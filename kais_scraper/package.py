"""Build the distributable Bulgaria-kais-*.zip archives."""
from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path
from typing import Callable

from tqdm import tqdm

from .paths import Layout

log = logging.getLogger(__name__)

Files = list[tuple[Path, str]]


def _tree(root: Path) -> Files:
    return [(p, p.relative_to(root).as_posix()) for p in sorted(root.rglob("*"))
            if p.is_file() and not p.name.endswith(".part")]


TARGETS: dict[str, tuple[str, Callable[[Layout], Files]]] = {
    "index": ("Bulgaria-kais-index.zip", lambda L: _tree(L.index)),
    "docs": ("Bulgaria-kais-docs.zip", lambda L: _tree(L.extracted)),
    "ownership": ("Bulgaria-kais-ownership.zip",
                  lambda L: [(p, p.name) for p in sorted(L.ownership.glob("ownership-*.tsv"))]),
    "geojson": ("Bulgaria-kais-geojson.zip", lambda L: _tree(L.geojson)),
}


def _zip(files: Files, dest: Path, desc: str) -> None:
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf, \
            tqdm(total=len(files), desc=desc, unit="file") as bar:
        for path, arcname in files:
            zf.write(path, arcname)
            bar.update()
    os.replace(tmp, dest)


def run_package(layout: Layout, only: list[str] | None = None) -> None:
    layout.ensure()
    for key in only or list(TARGETS):
        name, collect = TARGETS[key]
        files = collect(layout)
        if not files:
            log.info("Nothing to package for %s", key)
            continue
        _zip(files, layout.dist / name, key)
        log.info("Wrote %s (%d files)", name, len(files))
