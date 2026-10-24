"""Small shared helpers."""
from __future__ import annotations

import json
import logging
import os
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Sequence

from tqdm import tqdm

log = logging.getLogger(__name__)


def write_json_atomic(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def zip_ok(path: Path) -> bool:
    """Full CRC check, the equivalent of `zip -T`."""
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except Exception:  # BadZipFile, zlib.error, EOFError, OSError, ...
        return False


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_parallel(fn: Callable[..., Any], jobs: Sequence[tuple], *, workers: int,
                 desc: str, processes: bool) -> list[tuple[tuple, Exception]]:
    """Run fn(*job) for every job; returns the jobs that raised."""
    failures: list[tuple[tuple, Exception]] = []
    if not jobs:
        return failures
    workers = max(1, workers)
    executor_cls = ProcessPoolExecutor if processes and workers > 1 else ThreadPoolExecutor
    with executor_cls(max_workers=workers) as ex, tqdm(total=len(jobs), desc=desc, unit="file") as bar:
        futures = {ex.submit(fn, *job): job for job in jobs}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                failures.append((futures[fut], exc))
                log.warning("%s failed for %s: %s", desc, futures[fut][0], exc)
            bar.update()
    return failures
