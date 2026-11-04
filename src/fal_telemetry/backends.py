"""Backends expose fal's queue lifecycle: submit, then status, then result or cancel."""

import asyncio
import random
import time
from typing import Any, Protocol

import fal_client
from fal_client import Completed, InProgress, Queued, Status


class RequestHandle(Protocol):
    request_id: str

    async def status(self) -> Status: ...
    async def result(self) -> dict[str, Any]: ...
    async def cancel(self) -> None: ...


class Backend(Protocol):
    async def submit(
        self, endpoint: str, arguments: dict[str, Any], start_timeout: int
    ) -> RequestHandle: ...


class _LiveHandle:
    def __init__(self, handle: fal_client.AsyncRequestHandle) -> None:
        self._handle = handle
        self.request_id = handle.request_id

    async def status(self) -> Status:
        return await self._handle.status()

    async def result(self) -> dict[str, Any]:
        return await self._handle.get()

    async def cancel(self) -> None:
        await self._handle.cancel()


class LiveBackend:
    def __init__(self, key: str) -> None:
        self._client = fal_client.AsyncClient(key=key)

    async def submit(
        self, endpoint: str, arguments: dict[str, Any], start_timeout: int
    ) -> RequestHandle:
        handle = await self._client.submit(endpoint, arguments, start_timeout=start_timeout)
        return _LiveHandle(handle)


class _MockHandle:
    def __init__(self, rng: random.Random) -> None:
        self.request_id = f"mock-{rng.getrandbits(64):016x}"
        self._submitted_at = time.monotonic()
        # Occasional long queue waits mimic cold starts.
        self._queue = rng.uniform(4.0, 12.0) if rng.random() < 0.05 else rng.uniform(0.1, 1.5)
        self._inference = rng.lognormvariate(-0.4, 0.3)
        self._error = rng.random() < 0.02

    async def status(self) -> Status:
        elapsed = time.monotonic() - self._submitted_at
        if elapsed < self._queue:
            return Queued(position=int(self._queue - elapsed))
        if elapsed < self._queue + self._inference:
            return InProgress(logs=None)
        return Completed(
            logs=None,
            metrics={"inference_time": self._inference},
            error="simulated failure" if self._error else None,
            error_type="mock_error" if self._error else None,
        )

    async def result(self) -> dict[str, Any]:
        return {
            "images": [
                {
                    "url": "https://example.invalid/mock.jpg",
                    "width": 512,
                    "height": 512,
                    "content_type": "image/jpeg",
                }
            ],
            "seed": 0,
            "has_nsfw_concepts": [False],
        }

    async def cancel(self) -> None:
        return None


class MockBackend:
    """Simulates fal's timing and failures, so telemetry can be exercised for free."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    async def submit(
        self, endpoint: str, arguments: dict[str, Any], start_timeout: int
    ) -> RequestHandle:
        await asyncio.sleep(self._rng.uniform(0.02, 0.1))
        return _MockHandle(self._rng)
