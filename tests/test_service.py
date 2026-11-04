import asyncio
from typing import Any

import fal_client
import httpx
import pytest
from fal_client import Completed, InProgress, Queued

from fal_telemetry.backends import LiveBackend, MockBackend
from fal_telemetry.budget import BudgetExhausted, RequestBudget
from fal_telemetry.config import Settings
from fal_telemetry.service import (
    GenerationError,
    GenerationService,
    classify_error,
    estimate_cost,
    split_timings,
)

IMAGE = {"url": "https://x/y.jpg", "width": 512, "height": 512}


def settings(**overrides: Any) -> Settings:
    defaults = {
        "fal_poll_interval_seconds": 0.001,
        "fal_request_timeout_seconds": 5,
        "_env_file": None,
    }
    return Settings(**{**defaults, **overrides})


class ScriptedHandle:
    request_id = "req-1"

    def __init__(self, statuses: list, hang: bool = False) -> None:
        self._statuses = iter(statuses)
        self._hang = hang
        self.cancelled = False

    async def status(self):
        if self._hang:
            await asyncio.sleep(60)
        return next(self._statuses)

    async def result(self):
        return {"images": [IMAGE]}

    async def cancel(self):
        self.cancelled = True


class ScriptedBackend:
    def __init__(self, handle=None, error: Exception | None = None) -> None:
        self.handle = handle
        self.error = error
        self.calls: list[dict] = []

    async def submit(self, endpoint, arguments, start_timeout):
        self.calls.append({"endpoint": endpoint, "start_timeout": start_timeout, **arguments})
        if self.error:
            raise self.error
        return self.handle


def done(**kwargs: Any) -> Completed:
    return Completed(logs=None, metrics=kwargs.pop("metrics", {"inference_time": 0.5}), **kwargs)


def http_error(status: int) -> fal_client.client.FalClientHTTPError:
    return fal_client.client.FalClientHTTPError("nope", status, {}, response=httpx.Response(status))


async def test_success_returns_images_and_cost():
    handle = ScriptedHandle([Queued(position=2), InProgress(logs=None), done()])
    backend = ScriptedBackend(handle)
    result = await GenerationService(settings(), backend, None).generate("a cat")
    assert result.request_id == "req-1"
    assert result.images == [IMAGE]
    assert result.inference_seconds == 0.5
    assert result.estimated_cost_usd == pytest.approx(0.003)


async def test_submit_fixes_cost_controls_and_sets_start_timeout():
    backend = ScriptedBackend(ScriptedHandle([done()]))
    await GenerationService(settings(), backend, None).generate("a cat")
    call = backend.calls[0]
    assert call["endpoint"] == "fal-ai/flux/schnell"
    assert call["num_images"] == 1
    assert call["image_size"] == "square"
    assert call["start_timeout"] == 60


async def test_completed_with_error_fails_without_cost():
    handle = ScriptedHandle([done(error="boom", error_type="internal")])
    with pytest.raises(GenerationError) as exc:
        await GenerationService(settings(), ScriptedBackend(handle), None).generate("a cat")
    assert exc.value.error_type == "model_error"
    assert exc.value.request_id == "req-1"


async def test_timeout_cancels_the_request():
    handle = ScriptedHandle([], hang=True)
    service = GenerationService(
        settings(fal_request_timeout_seconds=0.01), ScriptedBackend(handle), None
    )
    with pytest.raises(GenerationError) as exc:
        await service.generate("a cat")
    assert exc.value.error_type == "timeout"
    assert handle.cancelled


async def test_http_error_on_submit_is_classified():
    backend = ScriptedBackend(error=http_error(401))
    with pytest.raises(GenerationError) as exc:
        await GenerationService(settings(), backend, None).generate("a cat")
    assert exc.value.error_type == "auth"


async def test_unexpected_error_is_reported_as_unknown():
    backend = ScriptedBackend(error=ValueError("Unknown status: NEW"))
    with pytest.raises(GenerationError) as exc:
        await GenerationService(settings(), backend, None).generate("a cat")
    assert exc.value.error_type == "unknown"


async def test_exhausted_budget_never_reaches_fal(tmp_path):
    backend = ScriptedBackend(ScriptedHandle([done()]))
    budget = RequestBudget(tmp_path / "b.json", limit=0)
    with pytest.raises(BudgetExhausted):
        await GenerationService(settings(), backend, budget).generate("a cat")
    assert backend.calls == []


async def test_failed_submit_still_uses_budget(tmp_path):
    budget = RequestBudget(tmp_path / "b.json", limit=1)
    backend = ScriptedBackend(error=http_error(500))
    with pytest.raises(GenerationError):
        await GenerationService(settings(), backend, budget).generate("a cat")
    assert budget.remaining == 0


async def test_budget_survives_restart(tmp_path):
    path = tmp_path / "nested" / "b.json"
    first = RequestBudget(path, limit=2)
    await first.reserve()
    second = RequestBudget(path, limit=2)
    assert second.remaining == 1
    await second.reserve()
    with pytest.raises(BudgetExhausted):
        await RequestBudget(path, limit=2).reserve()


async def test_concurrent_reservations_cannot_exceed_cap(tmp_path):
    budget = RequestBudget(tmp_path / "b.json", limit=3)
    results = await asyncio.gather(*(budget.reserve() for _ in range(10)), return_exceptions=True)
    assert sum(r is None for r in results) == 3


async def test_concurrency_limit_is_respected():
    active = peak = 0

    class Slow(ScriptedBackend):
        async def submit(self, endpoint, arguments, start_timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ScriptedHandle([done()])

    service = GenerationService(settings(fal_max_concurrency=2), Slow(), None)
    await asyncio.gather(*(service.generate("x") for _ in range(6)))
    assert peak == 2


async def test_mock_backend_completes_a_request():
    handle = await MockBackend(seed=1).submit("e", {}, start_timeout=1)
    assert handle.request_id.startswith("mock-")
    assert isinstance(await handle.status(), Queued | InProgress | Completed)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError(), "timeout"),
        (http_error(403), "auth"),
        (http_error(429), "rate_limited"),
        (http_error(422), "client_error"),
        (http_error(500), "server_error"),
        (http_error(504), "timeout"),
        (httpx.ConnectError("x"), "network"),
        (ValueError(), "unknown"),
    ],
)
def test_classify_error(exc, expected):
    assert classify_error(exc) == expected


def test_cost_rounds_up_to_whole_megapixels():
    assert estimate_cost([IMAGE], 0.003) == pytest.approx(0.003)
    big = {"width": 1500, "height": 1000}
    assert estimate_cost([big], 0.003) == pytest.approx(0.006)
    assert estimate_cost([], 0.003) == 0


def test_timings_use_observed_start_when_available():
    assert split_timings(10.0, 12.0, 13.0, 0.5) == (12.0, 13.0)


def test_timings_infer_start_when_polling_missed_it():
    assert split_timings(10.0, None, 13.0, 0.5) == (12.5, 13.0)


def test_inferred_queue_end_never_precedes_submit():
    assert split_timings(10.0, None, 10.1, 5.0) == (10.0, 10.1)


def test_live_mode_requires_key():
    with pytest.raises(ValueError, match="FAL_KEY"):
        settings(fal_mode="live", fal_key=None)


def test_key_is_hidden_from_repr():
    assert "secret-value" not in repr(settings(fal_mode="live", fal_key="secret-value"))


async def test_live_backend_speaks_fal_queue_protocol(monkeypatch):
    """Runs the real fal-client over a fake transport, checking the wire format without spend."""
    seen: list[httpx.Request] = []
    base = "https://queue.fal.run/fal-ai/flux/requests/abc"
    statuses = iter(
        [
            {"status": "IN_QUEUE", "queue_position": 3, "logs": None},
            {"status": "IN_PROGRESS", "logs": None},
            {"status": "COMPLETED", "logs": None, "metrics": {"inference_time": 0.7}},
            {"status": "COMPLETED", "logs": None, "metrics": {"inference_time": 0.7}},
        ]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "request_id": "abc",
                    "response_url": base,
                    "status_url": f"{base}/status",
                    "cancel_url": f"{base}/cancel",
                },
            )
        if path.endswith("/status"):
            return httpx.Response(200, json=next(statuses))
        return httpx.Response(200, json={"images": [IMAGE]})

    monkeypatch.setattr(
        fal_client.client, "AsyncBackupDomainTransport", lambda: httpx.MockTransport(respond)
    )
    backend = LiveBackend("test-key")
    result = await GenerationService(settings(), backend, None).generate("a cat")

    assert result.request_id == "abc"
    assert result.inference_seconds == 0.7
    assert result.queue_seconds > 0
    submit = seen[0]
    assert str(submit.url) == "https://queue.fal.run/fal-ai/flux/schnell"
    assert submit.headers["Authorization"] == "Key test-key"
    # fal-client only skips its retry of a timed-out submit when this header is set.
    assert float(submit.headers["X-Fal-Request-Timeout"]) == 60
