"""HTTP calls against the portal with bounded concurrency, rate limiting and retries."""
from __future__ import annotations

import asyncio
import logging
import re
import os
import random
import time
from pathlib import Path
from urllib.parse import urlencode
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from .auth import DOWNLOAD_URL, READ_URL, AuthError
from .util import zip_ok

log = logging.getLogger(__name__)
T = TypeVar("T")
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"  # as the browser sends it


class TransientError(RuntimeError):
    """Worth retrying: 5xx, 429, HTML instead of data, corrupt archive, empty body."""

    def __init__(self, message: str, *, min_delay: float = 0.0) -> None:
        super().__init__(message)
        self.min_delay = min_delay


class BlockedError(TransientError):
    """HTTP 403 mid-run: the portal's firewall throttling us. Back off harder before retrying."""


class FatalError(RuntimeError):
    """Too many consecutive failures; the server is down or the session died."""


def describe_error(exc: BaseException) -> str:
    """httpx often wraps the useful error (e.g. the SSL reason) a few levels down with an empty message."""
    parts = []
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).strip()
        parts.append(f"{type(cur).__name__}: {msg}" if msg else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


class RateLimiter:
    """Spaces out request starts, and adapts when the portal's firewall pushes back.

    A firewall 403 pauses *all* requests (not just the one that failed) and halves the
    rate; each further block while still throttled doubles the pause. After a run of
    successes the rate creeps back up towards the configured one.
    """

    MIN_RATE = 0.2          # never slower than one request every 5 s
    RECOVER_EVERY = 50      # successes before speeding up again
    MAX_PAUSE = 600.0

    def __init__(self, per_second: float | None, *, cooldown: float = 30.0) -> None:
        self._base = 1.0 / per_second if per_second else 0.0
        self._interval = self._base
        self._cooldown = cooldown
        self._pause = cooldown
        self._next = 0.0
        self._paused_until = 0.0
        self._streak = 0
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float | None:
        return 1.0 / self._interval if self._interval else None

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            target = max(self._next, self._paused_until)
            if target > now:
                await asyncio.sleep(target - now)
                now = target
            self._next = now + self._interval

    def blocked(self) -> None:
        now = time.monotonic()
        if now < self._paused_until:
            return  # a request that was already in flight; this block is already handled
        self._streak = 0
        self._interval = min(max(self._interval * 2, 0.25), 1.0 / self.MIN_RATE)
        self._paused_until = now + self._pause
        log.warning("The portal's firewall is throttling us: pausing all requests for %.0fs, "
                    "then continuing at %.2f req/s", self._pause, self.rate or 0)
        self._pause = min(max(self._pause * 2, self._cooldown), self.MAX_PAUSE)

    def succeeded(self) -> None:
        if time.monotonic() >= self._paused_until:
            self._pause = self._cooldown
        if self._interval <= self._base:
            return
        self._streak += 1
        if self._streak >= self.RECOVER_EVERY:
            self._streak = 0
            self._interval = max(self._base, self._interval * 0.8)
            log.info("No blocks for a while; speeding up to %.2f req/s", self.rate or 0)


_SUPPORT_ID = re.compile(r"support ID(?: is)?:?\s*([0-9]+)", re.I)


def _retry_after(r: httpx.Response) -> float:
    try:
        return float(r.headers.get("retry-after", 0))
    except ValueError:
        return 0.0


def _firewall_note(r: httpx.Response) -> str:
    try:
        text = r.text
    except httpx.ResponseNotRead:
        return ""
    m = _SUPPORT_ID.search(text)
    if m:
        return f" (rejected by the portal's firewall, support ID {m.group(1)})"
    if "Request Rejected" in text:
        return " (rejected by the portal's firewall)"
    return ""


def check_response(r: httpx.Response, what: str, *, first_request: bool = False,
                   blocked_delay: float = 30.0) -> None:
    """first_request: a rejection there means the session itself is bad, so fail fast."""
    if r.status_code == 401 or (first_request and r.status_code in (400, 403)):
        raise AuthError(f"HTTP {r.status_code} for {what}{_firewall_note(r)}: the session cookies "
                        "or CSRF token were rejected or have expired.")
    if r.status_code == 403:
        raise BlockedError(f"HTTP 403 for {what}{_firewall_note(r)}", min_delay=_retry_after(r))
    if r.status_code == 429 or r.status_code >= 500:
        raise TransientError(f"HTTP {r.status_code} for {what}", min_delay=_retry_after(r))
    r.raise_for_status()


class KaisClient:
    def __init__(self, http: httpx.AsyncClient, *, concurrency: int, index_concurrency: int,
                 rate: float | None, retries: int, retry_base_delay: float,
                 max_consecutive_failures: int, blocked_delay: float = 30.0) -> None:
        self.http = http
        self.download_sem = asyncio.Semaphore(concurrency)
        self.index_sem = asyncio.Semaphore(index_concurrency)
        self.limiter = RateLimiter(rate, cooldown=blocked_delay)
        self.retries = max(1, retries)
        self.base_delay = retry_base_delay
        self.max_consecutive = max_consecutive_failures
        self.blocked_delay = blocked_delay
        self._consecutive = 0

    async def _call(self, sem: asyncio.Semaphore, what: str, fn: Callable[[], Awaitable[T]]) -> T:
        delay = self.base_delay
        for attempt in range(1, self.retries + 1):
            try:
                async with sem:
                    await self.limiter.wait()
                    result = await fn()
            except AuthError:
                raise
            except (TransientError, httpx.TransportError) as exc:
                if isinstance(exc, BlockedError):
                    self.limiter.blocked()
                if attempt < self.retries:
                    level = logging.DEBUG if isinstance(exc, BlockedError) else logging.WARNING
                    log.log(level, "Attempt %d/%d for %s failed: %s", attempt, self.retries, what, describe_error(exc))
                    wait = max(delay, getattr(exc, "min_delay", 0.0))
                    await asyncio.sleep(wait * (1 + random.random() / 2))
                    delay = min(delay * 2, 60.0)
                    continue
                self._record_failure(what, exc)
                raise
            except Exception as exc:  # not retryable, e.g. 404
                self._record_failure(what, exc)
                raise
            else:
                self._consecutive = 0
                self.limiter.succeeded()
                return result
        raise AssertionError("unreachable")

    def _record_failure(self, what: str, exc: Exception) -> None:
        self._consecutive += 1
        if self.max_consecutive and self._consecutive >= self.max_consecutive:
            raise FatalError(f"{self._consecutive} requests in a row failed (last: {what}: {exc}). "
                             "The portal may be down or the session may have expired.") from exc

    async def list_children(self, target: str | None) -> list[dict[str, Any]]:
        """target=None lists the regions (the portal expects an empty POST for that)."""
        what = target or "<regions>"

        async def fetch() -> list[dict[str, Any]]:
            if target is None:
                r = await self.http.post(READ_URL, content=b"")
            else:
                r = await self.http.post(READ_URL, content=urlencode({"target": target}).encode(),
                                         headers={"Content-Type": FORM_CONTENT_TYPE})
            check_response(r, what, first_request=target is None, blocked_delay=self.blocked_delay)
            try:
                data = r.json()
            except ValueError as exc:
                raise TransientError(f"non-JSON response for {what}") from exc
            if not isinstance(data, list):
                raise TransientError(f"unexpected JSON for {what}: {type(data).__name__}")
            return data

        return await self._call(self.index_sem, what, fetch)

    async def download(self, path: str, dest: Path) -> int:
        """Stream one archive to dest via a .part file; returns its size."""
        tmp = dest.with_name(dest.name + ".part")

        async def fetch() -> int:
            size = 0
            async with self.http.stream("GET", DOWNLOAD_URL, params={"path": path}) as r:
                if r.status_code >= 400:
                    await r.aread()
                check_response(r, path, blocked_delay=self.blocked_delay)
                if "text/html" in r.headers.get("content-type", ""):
                    raise TransientError(f"got an HTML page instead of a file for {path}")
                with tmp.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 16):
                        fh.write(chunk)
                        size += len(chunk)
            if size == 0:
                raise TransientError(f"empty response for {path}")
            if dest.suffix.lower() == ".zip" and not await asyncio.to_thread(zip_ok, tmp):
                raise TransientError(f"corrupt zip for {path}")
            os.replace(tmp, dest)
            return size

        try:
            return await self._call(self.download_sem, path, fetch)
        finally:
            tmp.unlink(missing_ok=True)
