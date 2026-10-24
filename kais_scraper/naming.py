"""Turning portal paths into local file and directory names.

The rules mirror the sed expressions in the original bash scripts, so archives
downloaded by those scripts keep the same file names and can be adopted.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePath, PurePosixPath

_PREFIXES = re.compile(r"област |община |с\. |гр\. ")
_EKATTE_PAREN = re.compile(r" \((\d+)")
_PARENS = re.compile(r"[()]+")
_UNSAFE = re.compile(r'[\\:*?"<>|\x00-\x1f]')
_EKATTE_DIR = re.compile(r"\d{5}(?: - район [А-Яа-я ]+)*")
_POLY = " (Poly"

OWNERSHIP_SUFFIXES = {"parcels": "ПИ", "buildings": "сгради", "units": "СОС"}


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def local_name(remote_path: str) -> str:
    """'област Бургас/община Айтос/гр. Айтос (00151)/x.zip' -> 'Бургас-Айтос-Айтос-00151-x.zip'."""
    s = nfc(remote_path).strip()
    s = _PREFIXES.sub("", s)
    s = _EKATTE_PAREN.sub(r"-\1", s, count=1)
    s = _PARENS.sub("", s)
    s = s.replace("/", "-")
    return _UNSAFE.sub("_", s)


def extract_dir_name(zip_name: str) -> str:
    """Directory an archive is unpacked into: everything up to the EKATTE code (and district)."""
    name = nfc(zip_name)
    match = _EKATTE_DIR.search(name)
    base = name[: match.end()].rstrip() if match else PurePosixPath(name).stem
    return base.replace(" - район ", "-")


def geojson_name(rel_path: PurePath) -> str:
    """'Бургас-00151/sub/Parcels (Polygon).shp' -> 'Бургас-00151-sub-Parcels.geojson'."""
    parts = [nfc(p) for p in rel_path.parts]
    stem = parts[-1]
    if stem.lower().endswith(".shp"):
        stem = stem[:-4]
    cut = stem.find(_POLY)
    if cut != -1:
        stem = stem[:cut]
    return "-".join(parts[:-1] + [stem]) + ".geojson"


def ownership_category(stem: str) -> str | None:
    s = nfc(stem)
    for category, suffix in OWNERSHIP_SUFFIXES.items():
        if s.endswith(suffix):
            return category
    return None


class NameRegistry:
    """Hands out local names and disambiguates the rare case of two remote paths colliding."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def name_for(self, remote: str, preferred: str | None = None) -> str:
        name = preferred or local_name(remote)
        owner = self._owners.get(name)
        if owner is not None and owner != remote:
            p = PurePosixPath(name)
            digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:8]
            name = f"{p.stem}~{digest}{p.suffix}"
        self._owners[name] = remote
        return name
