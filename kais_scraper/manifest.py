"""Per-document record of what was downloaded, keyed by the portal's own 'Modified' value."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import write_json_atomic


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.meta: dict[str, Any] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self._dirty = 0
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.meta = data.get("meta", {})
            self.documents = data.get("documents", {})

    def __contains__(self, remote: str) -> bool:
        return remote in self.documents

    def file_for(self, remote: str) -> str | None:
        entry = self.documents.get(remote)
        return entry.get("file") if entry else None

    def is_current(self, remote: str, modified: str, dest: Path) -> bool:
        entry = self.documents.get(remote)
        if not entry or entry.get("modified") != modified:
            return False
        try:
            return dest.stat().st_size == entry.get("size")
        except FileNotFoundError:
            return False

    def record(self, remote: str, modified: str, file: str, size: int) -> None:
        self.documents[remote] = {"modified": modified, "file": file, "size": size,
                                  "downloaded_at": _utc_now()}
        self._dirty += 1

    def maybe_save(self, every: int = 200) -> None:
        if self._dirty >= every:
            self.save()

    def save(self, *, completed: bool = False) -> None:
        self.meta["last_run"] = _utc_now()
        if completed:
            self.meta["last_complete_run"] = self.meta["last_run"]
        write_json_atomic(self.path, {"version": 1, "meta": self.meta, "documents": self.documents})
        self._dirty = 0
