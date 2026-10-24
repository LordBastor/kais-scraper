"""Command line entry point: python -m kais_scraper <command> ..."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from tqdm.contrib.logging import logging_redirect_tqdm

import httpx

from .auth import AuthError
from .client import FatalError, describe_error
from .extract import run_extract
from .geo import geo_available, run_geojson
from .ownership import run_ownership
from .package import TARGETS, run_package
from .paths import Layout
from .scrape import ScrapeOptions, ScrapeResult, run_scrape
from .timewindow import in_window, now_in, parse_window
from .util import human_bytes

log = logging.getLogger("kais_scraper")

AUTH_HINT = (
    "Refresh the session: open https://kais.cadastre.bg/bg/OpenData with DevTools, "
    "right-click the second OpenData/Read request > Copy > Copy as cURL (bash), "
    "save it to a file and pass it with --curl-file."
)
CONNECT_HINT = (
    "Check that the portal is reachable from this machine: "
    "curl -sv -o /dev/null https://kais.cadastre.bg/bg/OpenData"
)
DEFAULT_WORKERS = os.cpu_count() or 4


def _add_scrape_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("scraping")
    g.add_argument("--curl-file", type=Path, help="file with a 'Copy as cURL (bash)' command (session cookies + CSRF token)")
    g.add_argument("--concurrency", type=int, default=8, help="parallel archive downloads (default: 8)")
    g.add_argument("--index-concurrency", type=int, default=4, help="parallel index requests (default: 4)")
    g.add_argument("--rate", type=float, default=1, help="max requests per second overall, 0 = unlimited (default: 1, what the portal's firewall tolerates)")
    g.add_argument("--blocked-delay", type=float, default=30, help="initial pause after the firewall answers 403; doubles while blocks continue (default: 30)")
    g.add_argument("--retries", type=int, default=5, help="attempts per request (default: 5)")
    g.add_argument("--timeout", type=float, default=120, help="per-request read timeout in seconds (default: 120)")
    g.add_argument("--refresh-index", action="store_true", help="refetch municipality and settlement lists instead of using the cache")
    g.add_argument("--adopt-existing", action="store_true", help="trust valid archives already in doc/ that the manifest doesn't know (migration from the bash scripts)")
    g.add_argument("--region", action="append", default=[], help="only process regions whose path contains this text (repeatable)")
    g.add_argument("--dry-run", action="store_true", help="fetch the index and report what would be downloaded")
    g.add_argument("--window", default="22:00-06:00", help="allowed local time window (default: 22:00-06:00)")
    g.add_argument("--timezone", default="Europe/Sofia", help="time zone for --window (default: Europe/Sofia)")
    g.add_argument("--ignore-window", action="store_true", help="run outside the night-time window")


def _add_geo_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("geojson")
    g.add_argument("--encoding", help="DBF attribute encoding if there is no .cpg file, e.g. cp1251")
    g.add_argument("--source-crs", help="CRS for shapefiles without a .prj, e.g. EPSG:7801")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path, default=Path("data"), help="working directory (default: ./data)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    workers = argparse.ArgumentParser(add_help=False)
    workers.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                         help=f"parallel workers (default: {DEFAULT_WORKERS})")

    parser = argparse.ArgumentParser(prog="kais_scraper", description="Bulgarian cadastre (KAIS) open data scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_scrape_args(sub.add_parser("scrape", parents=[common], help="download new and changed archives"))
    sub.add_parser("extract", parents=[common, workers], help="unpack archives into extracted/")
    sub.add_parser("ownership", parents=[common, workers], help="build ownership-*.tsv from the spreadsheets")
    _add_geo_args(sub.add_parser("geojson", parents=[common, workers], help="convert shapefiles to WGS84 GeoJSON"))
    pkg = sub.add_parser("package", parents=[common], help="build Bulgaria-kais-*.zip in dist/")
    pkg.add_argument("--only", nargs="+", choices=list(TARGETS), help="build only these archives")

    run_all = sub.add_parser("all", parents=[common, workers], help="scrape, extract, ownership, geojson, package")
    _add_scrape_args(run_all)
    _add_geo_args(run_all)
    run_all.add_argument("--skip-geojson", action="store_true")
    run_all.add_argument("--skip-package", action="store_true")
    return parser


def _check_window(args: argparse.Namespace) -> bool:
    if args.ignore_window:
        return True
    start, end = parse_window(args.window)
    now = now_in(args.timezone)
    if in_window(now.time(), start, end):
        return True
    log.error("It is %s in %s, outside the allowed window %s. The portal asks for night-time "
              "runs; pass --ignore-window to override.", now.strftime("%H:%M"), args.timezone, args.window)
    return False


def _report(res: ScrapeResult) -> None:
    log.info("Index: %d fetched, %d from cache, %d failed",
             res.index_fetched, res.index_cached, len(res.index_failed))
    log.info("Archives: %d listed, %d up to date, %d adopted, %d to download, %d downloaded (%s), %d failed",
             res.docs_listed, res.docs_current, res.docs_adopted, res.docs_pending,
             res.docs_downloaded, human_bytes(res.bytes_downloaded), len(res.docs_failed))
    for path in (res.index_failed + res.docs_failed)[:20]:
        log.warning("  failed: %s", path)
    remaining = len(res.index_failed) + len(res.docs_failed) - 20
    if remaining > 0:
        log.warning("  ... and %d more; rerun to retry them", remaining)


def cmd_scrape(args: argparse.Namespace) -> int:
    if not _check_window(args):
        return 4
    opts = ScrapeOptions(
        data_dir=args.data_dir,
        curl_file=args.curl_file,
        concurrency=args.concurrency,
        index_concurrency=args.index_concurrency,
        rate=args.rate or None,
        retries=args.retries,
        blocked_delay=args.blocked_delay,
        timeout=args.timeout,
        refresh_index=args.refresh_index,
        adopt_existing=args.adopt_existing,
        regions=args.region,
        dry_run=args.dry_run,
    )
    res = asyncio.run(run_scrape(opts))
    _report(res)
    return 0 if res.ok else 1


def cmd_extract(args: argparse.Namespace) -> int:
    return 1 if run_extract(Layout(args.data_dir), args.workers) else 0


def cmd_ownership(args: argparse.Namespace) -> int:
    return 1 if run_ownership(Layout(args.data_dir), args.workers) else 0


def cmd_geojson(args: argparse.Namespace) -> int:
    return 1 if run_geojson(Layout(args.data_dir), args.workers, args.encoding, args.source_crs) else 0


def cmd_package(args: argparse.Namespace) -> int:
    run_package(Layout(args.data_dir), args.only)
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    code = cmd_scrape(args)
    if code == 4:
        return code
    code = max(code, cmd_extract(args), cmd_ownership(args))
    if not args.skip_geojson:
        if geo_available():
            code = max(code, cmd_geojson(args))
        else:
            log.warning("Skipping GeoJSON: geopandas/pyogrio are not installed")
    if not args.skip_package:
        cmd_package(argparse.Namespace(data_dir=args.data_dir, only=None))
    return code


COMMANDS = {
    "scrape": cmd_scrape,
    "extract": cmd_extract,
    "ownership": cmd_ownership,
    "geojson": cmd_geojson,
    "package": cmd_package,
    "all": cmd_all,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with logging_redirect_tqdm():
        try:
            return COMMANDS[args.command](args)
        except AuthError as exc:
            log.error("%s\n%s", exc, AUTH_HINT)
            return 2
        except FatalError as exc:
            log.error("%s\n%s", exc, AUTH_HINT)
            return 3
        except httpx.TransportError as exc:
            log.error("Could not talk to the portal: %s\n%s", describe_error(exc), CONNECT_HINT)
            return 5
        except KeyboardInterrupt:
            log.warning("Interrupted. Finished downloads are recorded; rerun to continue.")
            return 130
