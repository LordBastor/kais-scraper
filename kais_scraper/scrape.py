"""Walk region -> municipality -> settlement -> document list, then download changed archives."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine, Iterable

import httpx
from tqdm import tqdm

from .auth import DEFAULT_HEADERS, bootstrap, parse_curl
from .client import KaisClient, TransientError
from .manifest import Manifest
from .naming import NameRegistry, nfc
from .paths import Layout
from .tls import portal_ssl_context
from .util import write_json_atomic, zip_ok

log = logging.getLogger(__name__)

RECOVERABLE = (TransientError, httpx.HTTPError, OSError)
LEVEL_NAMES = {1: "municipalities", 2: "settlements", 3: "document lists"}


@dataclass
class ScrapeOptions:
    data_dir: Path
    curl_file: Path | None = None
    concurrency: int = 8
    index_concurrency: int = 4
    rate: float | None = 1.0
    retries: int = 5
    retry_base_delay: float = 2.0
    blocked_delay: float = 30.0
    timeout: float = 120.0
    max_consecutive_failures: int = 25
    refresh_index: bool = False
    adopt_existing: bool = False
    regions: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass
class ScrapeResult:
    index_fetched: int = 0
    index_cached: int = 0
    index_failed: list[str] = field(default_factory=list)
    docs_listed: int = 0
    docs_current: int = 0
    docs_adopted: int = 0
    docs_pending: int = 0
    docs_downloaded: int = 0
    bytes_downloaded: int = 0
    docs_failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.index_failed and not self.docs_failed


async def _run_all(coros: Iterable[Coroutine[Any, Any, None]]) -> None:
    """Run coroutines concurrently; a fatal error in one cancels the rest (Python 3.10 compatible)."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _child_items(listings: Iterable[list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for listing in listings:
        for item in listing:
            path = item.get("Path") if isinstance(item, dict) else None
            if path:
                items.setdefault(path, item)
    return items


async def _fetch_level(client: KaisClient, layout: Layout, paths: list[str], level: int,
                       use_cache: bool, res: ScrapeResult) -> list[list[dict[str, Any]]]:
    registry = NameRegistry()
    jobs = [(p, layout.index / f"{registry.name_for(p)}.{level}.json") for p in paths]
    listings: list[list[dict[str, Any]]] = []
    bar = tqdm(total=len(jobs), desc=LEVEL_NAMES[level], unit="req")

    async def one(path: str, file: Path) -> None:
        if use_cache and file.exists():
            try:
                listings.append(json.loads(file.read_text(encoding="utf-8")))
                res.index_cached += 1
                bar.update()
                return
            except ValueError:
                log.debug("Cached index %s is unreadable, refetching", file.name)
        try:
            data = await client.list_children(path)
        except RECOVERABLE as exc:
            res.index_failed.append(path)
            log.warning("Could not list %s: %s", path, exc)
        else:
            write_json_atomic(file, data)
            listings.append(data)
            res.index_fetched += 1
        bar.update()

    try:
        await _run_all(one(p, f) for p, f in jobs)
    finally:
        bar.close()
    return listings


async def _download_docs(client: KaisClient, layout: Layout, manifest: Manifest,
                         docs: dict[str, dict[str, Any]], opts: ScrapeOptions,
                         res: ScrapeResult) -> None:
    registry = NameRegistry()
    todo: list[tuple[str, str, Path]] = []
    for remote, item in docs.items():
        modified = str(item.get("Modified") or "")
        name = registry.name_for(remote, preferred=manifest.file_for(remote))
        dest = layout.doc / name
        if manifest.is_current(remote, modified, dest):
            res.docs_current += 1
            continue
        if opts.adopt_existing and remote not in manifest and dest.exists():
            if dest.suffix.lower() != ".zip" or await asyncio.to_thread(zip_ok, dest):
                manifest.record(remote, modified, name, dest.stat().st_size)
                res.docs_adopted += 1
                continue
        todo.append((remote, modified, dest))

    res.docs_pending = len(todo)
    if opts.dry_run:
        for remote, _, _ in todo[:50]:
            log.info("would download: %s", remote)
        if len(todo) > 50:
            log.info("... and %d more", len(todo) - 50)
        manifest.save()
        return

    bar = tqdm(total=len(todo), desc="archives", unit="file")

    async def one(remote: str, modified: str, dest: Path) -> None:
        try:
            size = await client.download(remote, dest)
        except RECOVERABLE as exc:
            res.docs_failed.append(remote)
            log.warning("Download failed for %s: %s", remote, exc)
        else:
            manifest.record(remote, modified, dest.name, size)
            res.docs_downloaded += 1
            res.bytes_downloaded += size
            manifest.maybe_save()
        bar.update()

    try:
        await _run_all(one(*job) for job in todo)
    finally:
        bar.close()
        manifest.save(completed=res.ok)


async def run_scrape(opts: ScrapeOptions,
                     transport: httpx.AsyncBaseTransport | None = None) -> ScrapeResult:
    layout = Layout(opts.data_dir)
    layout.ensure()
    res = ScrapeResult()
    manifest = Manifest(layout.manifest)

    pool = opts.concurrency + opts.index_concurrency
    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=httpx.Timeout(opts.timeout, connect=30.0),
        limits=httpx.Limits(max_connections=pool, max_keepalive_connections=pool),
        follow_redirects=True,
        # The portal's firewall resets handshakes that offer only http/1.1; see tls.py.
        verify=portal_ssl_context(),
        http2=True,
        transport=transport,
    ) as http:
        if opts.curl_file:
            creds = parse_curl(opts.curl_file.read_text(encoding="utf-8"))
        else:
            log.info("No --curl-file given; trying to open a session automatically")
            creds = await bootstrap(http)
        creds.apply(http)

        client = KaisClient(
            http,
            concurrency=opts.concurrency,
            index_concurrency=opts.index_concurrency,
            rate=opts.rate,
            retries=opts.retries,
            retry_base_delay=opts.retry_base_delay,
            max_consecutive_failures=opts.max_consecutive_failures,
            blocked_delay=opts.blocked_delay,
        )

        # Always fetched: one request, and it validates the session before anything else.
        regions = await client.list_children(None)
        write_json_atomic(layout.index / "regions.0.json", regions)
        res.index_fetched += 1

        region_paths = list(_child_items([regions]))
        if opts.regions:
            wanted = [nfc(r).casefold() for r in opts.regions]
            region_paths = [p for p in region_paths if any(w in nfc(p).casefold() for w in wanted)]
            if not region_paths:
                raise ValueError(f"No region matches {opts.regions}")
        log.info("Regions to process: %d", len(region_paths))

        cache = not opts.refresh_index
        municipalities = await _fetch_level(client, layout, region_paths, 1, cache, res)
        settlements = await _fetch_level(client, layout, list(_child_items(municipalities)), 2, cache, res)
        # Document lists carry the Modified dates, so they are refetched on every run.
        doc_lists = await _fetch_level(client, layout, list(_child_items(settlements)), 3, False, res)

        docs = _child_items(doc_lists)
        res.docs_listed = len(docs)
        log.info("Archives listed on the portal: %d", len(docs))
        await _download_docs(client, layout, manifest, docs, opts, res)

    return res
