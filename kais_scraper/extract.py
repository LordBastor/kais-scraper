"""Unpack downloaded archives into extracted/<EKATTE dir>/ (the old doc1/ step)."""
from __future__ import annotations

import logging
import os
import shutil
import time
import zipfile
from pathlib import Path

from .naming import extract_dir_name, nfc
from .paths import Layout
from .util import run_parallel

log = logging.getLogger(__name__)
_UTF8_FLAG = 0x800


def _member_name(info: zipfile.ZipInfo) -> str:
    """Archives without the UTF-8 flag are usually UTF-8 anyway (the old `unzip -O utf8`)."""
    if info.flag_bits & _UTF8_FLAG:
        return info.filename
    raw = info.filename.encode("cp437")
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def _entry_mtime(info: zipfile.ZipInfo) -> float | None:
    try:
        return time.mktime(info.date_time + (0, 0, -1))
    except (OverflowError, ValueError):
        return None


def extract_one(zip_path: Path, out_root: Path) -> int:
    target = out_root / extract_dir_name(zip_path.name)
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    written = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = nfc(_member_name(info)).replace("\\", "/").lstrip("/")
            dest = (target / name).resolve()
            if not dest.is_relative_to(root):
                log.warning("Skipping unsafe entry %r in %s", name, zip_path.name)
                continue
            mtime = _entry_mtime(info)
            try:
                st = dest.stat()
                if st.st_size == info.file_size and mtime is not None and int(st.st_mtime) == int(mtime):
                    continue
            except FileNotFoundError:
                pass
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            with zf.open(info) as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            if mtime is not None:
                os.utime(tmp, (mtime, mtime))
            os.replace(tmp, dest)
            written += 1
    return written


def run_extract(layout: Layout, workers: int) -> int:
    layout.ensure()
    archives = sorted(layout.doc.glob("*.zip"))
    log.info("Extracting %d archives", len(archives))
    failures = run_parallel(extract_one, [(a, layout.extracted) for a in archives],
                            workers=workers, desc="extract", processes=False)
    return len(failures)
