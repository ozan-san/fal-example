"""Runs one fal request through the queue and records what happened at each stage."""

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import fal_client
import httpx
from fal_client import Completed, InProgress, Queued
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from opentelemetry.metrics import Observation
from opentelemetry.trace import Status as SpanStatus
from opentelemetry.trace import StatusCode

from fal_telemetry.backends import Backend, RequestHandle
from fal_telemetry.budget import BudgetExhausted, RequestBudget
from fal_telemetry.config import Settings

log = logging.getLogger(__name__)
tracer = trace.get_tracer("fal_telemetry")
meter = metrics.get_meter("fal_telemetry")

_requests = meter.create_counter("fal.client.requests", description="Generation attempts")
_cost = meter.create_counter(
    "fal.client.cost", unit="usd", description="Estimated spend on successful requests"
)
_submit_duration = meter.create_histogram("fal.client.submit.duration", unit="s")
_queue_duration = meter.create_histogram(
    "fal.client.queue.duration", unit="s", description="Accepted until processing started"
)
_inference_duration = meter.create_histogram(
    "fal.client.inference.duration", unit="s", description="Processing time reported by fal"
)
_request_duration = meter.create_histogram(
    "fal.client.request.duration", unit="s", description="End to end, as seen by this client"
)
_queue_position = meter.create_histogram(
    "fal.client.queue.position", description="Position at first status check"
)
_local_wait_duration = meter.create_histogram(
    "fal.client.local_wait.duration",
    unit="s",
    description="Time held back by this app's own concurrency limit, before reaching fal",
)
_in_flight = meter.create_up_down_counter("fal.client.in_flight")

ERROR_TYPES = (
    "auth",
    "rate_limited",
    "client_error",
    "server_error",
    "model_error",
    "timeout",
    "network",
    "unknown",
)


class GenerationError(Exception):
    def __init__(self, error_type: str, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.request_id = request_id


@dataclass
class Generation:
    request_id: str
    images: list[dict[str, Any]]
    queue_seconds: float
    inference_seconds: float | None
    total_seconds: float
    estimated_cost_usd: float


def classify_error(exc: Exception) -> str:
    """Map an exception to a small fixed set of values that is safe as a metric label."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, fal_client.client.FalClientHTTPError):
        code = exc.status_code
        if code in (401, 403):
            return "auth"
        if code == 429:
            return "rate_limited"
        if code == 504:
            return "timeout"
        return "server_error" if code >= 500 else "client_error"
    if isinstance(exc, httpx.TransportError):
        return "network"
    return "unknown"


def estimate_cost(images: list[dict[str, Any]], per_megapixel: float) -> float:
    """Round each image up to a whole megapixel, matching fal's listed billing for FLUX."""
    megapixels = sum(math.ceil(i["width"] * i["height"] / 1_000_000) for i in images)
    return megapixels * per_megapixel


def split_timings(
    submitted_at: float,
    first_running_at: float | None,
    completed_at: float,
    inference_seconds: float | None,
) -> tuple[float, float]:
    """Return (queue end, processing end) on the same clock as the inputs.

    Polling can miss IN_PROGRESS on fast requests. In that case the queue's end is
    worked out by subtracting fal's reported inference time from the completion time.
    """
    if first_running_at is not None:
        queue_end = first_running_at
    elif inference_seconds is not None:
        queue_end = max(submitted_at, completed_at - inference_seconds)
    else:
        queue_end = completed_at
    return queue_end, completed_at


class GenerationService:
    def __init__(self, settings: Settings, backend: Backend, budget: RequestBudget | None) -> None:
        self._settings = settings
        self._backend = backend
        self._budget = budget
        self._limit = asyncio.Semaphore(settings.fal_max_concurrency)
        self._attrs = {"fal.endpoint": settings.fal_endpoint, "fal.mode": settings.fal_mode}
        # Start every series at 0. Prometheus can't see the jump in a series that first
        # appears at 1, so the first failure of each type would otherwise be lost.
        for outcome, error_type in (
            ("success", "none"),
            ("rejected", "budget_exhausted"),
            *(("failure", e) for e in ERROR_TYPES),
        ):
            _requests.add(0, {**self._attrs, "outcome": outcome, "error_type": error_type})
        if budget is not None:
            meter.create_observable_gauge(
                "fal.client.budget.remaining",
                callbacks=[lambda _: [Observation(budget.remaining, self._attrs)]],
                description="Live requests left before the hard cap",
            )

    async def generate(self, prompt: str) -> Generation:
        started = time.perf_counter()
        _in_flight.add(1, self._attrs)
        try:
            with tracer.start_as_current_span("fal.generate", attributes=self._attrs) as span:
                # Length only: prompts are user content and don't belong in telemetry.
                span.set_attribute("fal.prompt.length", len(prompt))
                try:
                    async with self._limit:
                        local_wait = time.perf_counter() - started
                        _local_wait_duration.record(local_wait, self._attrs)
                        span.set_attribute("fal.local_wait_seconds", local_wait)
                        result = await self._run(prompt, span)
                except BudgetExhausted:
                    span.set_status(SpanStatus(StatusCode.ERROR, "budget_exhausted"))
                    self._finish(started, "rejected", "budget_exhausted")
                    log.warning("Request rejected: budget exhausted")
                    raise
                except GenerationError as exc:
                    span.set_status(SpanStatus(StatusCode.ERROR, exc.error_type))
                    span.set_attribute("error.type", exc.error_type)
                    self._finish(started, "failure", exc.error_type)
                    log.error(
                        "fal request failed",
                        extra={"fal.error_type": exc.error_type, "fal.request_id": exc.request_id},
                    )
                    raise
                self._finish(started, "success", "none")
                result.total_seconds = time.perf_counter() - started
                log.info(
                    "fal request succeeded",
                    extra={
                        "fal.request_id": result.request_id,
                        "fal.queue_seconds": round(result.queue_seconds, 3),
                        "fal.total_seconds": round(result.total_seconds, 3),
                    },
                )
                return result
        finally:
            _in_flight.add(-1, self._attrs)

    def _finish(self, started: float, outcome: str, error_type: str) -> None:
        attrs = {**self._attrs, "outcome": outcome, "error_type": error_type}
        _requests.add(1, attrs)
        if outcome != "rejected":
            _request_duration.record(time.perf_counter() - started, attrs)

    async def _run(self, prompt: str, span: trace.Span) -> Generation:
        s = self._settings
        if self._budget is not None:
            await self._budget.reserve()

        handle: RequestHandle | None = None
        try:
            async with asyncio.timeout(s.fal_request_timeout_seconds):
                handle = await self._submit(prompt)
                span.set_attribute("fal.request_id", handle.request_id)
                return await self._wait_for_result(handle, span)
        except GenerationError:
            raise
        except Exception as exc:  # Anything unexpected must still reach the failure metrics.
            error_type = classify_error(exc)
            span.record_exception(exc)
            if isinstance(exc, TimeoutError) and handle is not None:
                await self._cancel(handle)
            request_id = handle.request_id if handle else None
            raise GenerationError(error_type, str(exc), request_id) from exc

    async def _submit(self, prompt: str) -> RequestHandle:
        s = self._settings
        arguments = {
            "prompt": prompt,
            "image_size": s.fal_image_size,
            "num_inference_steps": s.fal_num_inference_steps,
            "num_images": 1,
        }
        started = time.perf_counter()
        with tracer.start_as_current_span("fal.submit"):
            handle = await self._backend.submit(
                s.fal_endpoint, arguments, start_timeout=s.fal_start_timeout_seconds
            )
        _submit_duration.record(time.perf_counter() - started, self._attrs)
        return handle

    async def _wait_for_result(self, handle: RequestHandle, span: trace.Span) -> Generation:
        s = self._settings
        submitted_ns = time.time_ns()
        submitted_at = time.perf_counter()
        first_running_at: float | None = None
        recorded_position = False
        polls = 0

        # One HTTP span per poll would bury the phases that matter in each trace.
        with suppress_http_instrumentation():
            while True:
                status = await handle.status()
                polls += 1
                if isinstance(status, Queued) and not recorded_position:
                    _queue_position.record(status.position, self._attrs)
                    span.set_attribute("fal.queue.initial_position", status.position)
                    recorded_position = True
                elif isinstance(status, InProgress) and first_running_at is None:
                    first_running_at = time.perf_counter()
                elif isinstance(status, Completed):
                    break
                await asyncio.sleep(s.fal_poll_interval_seconds)
        completed_at = time.perf_counter()
        span.set_attribute("fal.status_polls", polls)

        inference = (status.metrics or {}).get("inference_time")
        queue_end, _ = split_timings(submitted_at, first_running_at, completed_at, inference)
        queue_seconds = queue_end - submitted_at
        self._record_phases(submitted_ns, queue_seconds, completed_at - queue_end, inference)
        _queue_duration.record(queue_seconds, self._attrs)
        if inference is not None:
            _inference_duration.record(inference, self._attrs)

        if status.error:
            error_type = "timeout" if status.error_type == "request_timeout" else "model_error"
            raise GenerationError(error_type, status.error, handle.request_id)

        with tracer.start_as_current_span("fal.fetch_result"):
            result = await handle.result()

        images = result.get("images", [])
        cost = estimate_cost(images, s.fal_cost_per_megapixel_usd)
        _cost.add(cost, self._attrs)
        span.set_attribute("fal.estimated_cost_usd", cost)
        return Generation(
            request_id=handle.request_id,
            images=images,
            queue_seconds=queue_seconds,
            inference_seconds=inference,
            total_seconds=0.0,
            estimated_cost_usd=cost,
        )

    def _record_phases(
        self,
        start_ns: int,
        queue_seconds: float,
        processing_seconds: float,
        inference: float | None,
    ) -> None:
        """Emit phase spans afterwards, once polling has shown where the boundaries were."""
        queue_end_ns = start_ns + int(queue_seconds * 1e9)
        processing_end_ns = queue_end_ns + int(processing_seconds * 1e9)
        tracer.start_span("fal.queue_wait", start_time=start_ns).end(end_time=queue_end_ns)
        running = tracer.start_span("fal.inference", start_time=queue_end_ns)
        if inference is not None:
            running.set_attribute("fal.inference_time_reported", inference)
        running.end(end_time=processing_end_ns)

    async def _cancel(self, handle: RequestHandle) -> None:
        """Best effort, so an abandoned request is less likely to keep costing money."""
        try:
            async with asyncio.timeout(5):
                await handle.cancel()
            log.warning("Cancelled timed-out request", extra={"fal.request_id": handle.request_id})
        except Exception:
            log.warning(
                "Could not cancel request",
                exc_info=True,
                extra={"fal.request_id": handle.request_id},
            )
