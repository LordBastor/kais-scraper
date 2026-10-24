"""The portal asks for night-time runs; this enforces it."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


def parse_window(spec: str) -> tuple[time, time]:
    try:
        start, end = (time.fromisoformat(part.strip()) for part in spec.split("-"))
    except ValueError as exc:
        raise ValueError(f"invalid time window {spec!r}, expected e.g. 22:00-06:00") from exc
    return start, end


def in_window(now: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # window crosses midnight


def now_in(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))
