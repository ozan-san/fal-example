"""Lifetime cap on live fal submissions, stored on disk so restarts cannot reset it."""

import asyncio
import json
import os
from pathlib import Path


class BudgetExhausted(Exception):
    pass


class RequestBudget:
    def __init__(self, path: Path, limit: int) -> None:
        self._path = path
        self._limit = limit
        self._lock = asyncio.Lock()
        self._used = self._load()

    @property
    def remaining(self) -> int:
        return max(0, self._limit - self._used)

    async def reserve(self) -> None:
        """Claim one request. Saved before the submit, so a crash can't under-count."""
        async with self._lock:
            if self._used >= self._limit:
                raise BudgetExhausted(f"request cap of {self._limit} reached")
            self._save(self._used + 1)
            self._used += 1

    def _load(self) -> int:
        try:
            return int(json.loads(self._path.read_text())["used"])
        except FileNotFoundError:
            return 0

    def _save(self, used: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"used": used}))
        os.replace(tmp, self._path)
