"""On-disk layout of the data directory."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def index(self) -> Path:
        return self.root / "index"

    @property
    def doc(self) -> Path:
        return self.root / "doc"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def extracted(self) -> Path:
        return self.root / "extracted"

    @property
    def ownership(self) -> Path:
        return self.root / "ownership"

    @property
    def geojson(self) -> Path:
        return self.root / "geojson"

    @property
    def dist(self) -> Path:
        return self.root / "dist"

    @property
    def manifest(self) -> Path:
        return self.state / "manifest.json"

    def ensure(self) -> None:
        for d in (self.index, self.doc, self.state, self.extracted,
                  self.ownership, self.geojson, self.dist):
            d.mkdir(parents=True, exist_ok=True)
