# kais-scraper

This tool keeps an incremental local mirror of the [Bulgarian cadastre open data portal](https://kais.cadastre.bg/bg/OpenData). It downloads the published archives and turns them into three outputs:

- ownership tables (TSV)
- WGS84 GeoJSON
- the distributable `Bulgaria-kais-*.zip` bundles

It is a Python port based on the great work by [Boyan Yorukov](https://gist.github.com/yurukov) of the earlier `scrape.sh` / `scrape-sub.sh` / `unpack.sh` scripts which can be found in the following [gist](https://gist.github.com/yurukov/1273e8eca40ebfce695bd2da144ef856). It produces the same outputs, fixes several of their bugs and avoids starting thousands of processes.

## Quick start

You need Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Save a browser session (see "Getting a session" below) to session.curl
# 2. Try a single region first, without downloading anything:
python -m kais_scraper scrape --curl-file session.curl --region Бургас --dry-run --ignore-window

# 3. Full run, at night:
python -m kais_scraper all --curl-file session.curl
```

All files go under `./data` unless you pass `--data-dir`.

## Please run it at night

The portal operators ask for scraping to happen when load is low. By default `scrape` refuses to start outside 22:00–06:00 Europe/Sofia time. You can change the window with `--window`/`--timezone`, or bypass it with `--ignore-window` for small test runs. For the same reason the defaults are deliberately gentle: 8 parallel downloads (the old script used 40), 4 parallel index requests, and at most 1 request per second overall, which is what the portal's firewall tolerated in testing (4 req/s got blocked within about 60 requests). The portal's firewall answers too many requests with HTTP 403. When that happens, the tool pauses all requests (30 seconds at first, doubling while the blocks continue, up to 10 minutes) and halves its request rate. After a run of successful requests it gradually speeds back up towards `--rate`. Raising `--rate` above the default makes these pauses more likely.

At 1 request per second, the first full download of the country (roughly 5,000 document lists and 20,000+ archives) takes about 7–8 hours, so it may span more than one night. That's fine: interrupted runs resume where they stopped, and you can also do a few regions per night with `--region`. Later runs only fetch the document lists and changed archives.

For a scheduled nightly run, a crontab entry looks like this:

```cron
30 23 * * * cd /opt/kais-scraper && .venv/bin/python -m kais_scraper all --curl-file session.curl >> data/run.log 2>&1
```

## Getting a session

The portal needs session cookies and a CSRF token.

1. Open https://kais.cadastre.bg/bg/OpenData in Chrome or Firefox and open DevTools → Network → Fetch/XHR.
2. Click into any region so that an `OpenData/Read` request with a payload appears.
3. Right-click that request and choose Copy → **Copy as cURL (bash)**. Avoid the cmd/PowerShell variants.
4. Paste it into `session.curl`. Only the cookies and headers are used, so the URL and payload don't matter.

If you don't pass `--curl-file`, the tool tries to open a session automatically by loading the page and reading the token from it. That only works if the portal exposes the token in its HTML and its bot protection doesn't block non-browser clients, so treat it as best effort.

`session.curl` holds live credentials. It is listed in `.gitignore` and should not be committed or shared.

A rejected first request (the region list) means the session is bad: the tool stops with exit code 2. A 403 later in the run usually means the firewall is throttling you, so the tool backs off and retries. If failures keep coming, it stops with exit code 3. Downloads that already finished are kept, so after refreshing `session.curl` a rerun picks up where it stopped.

## Commands

| Command | What it does | Replaces |
|---|---|---|
| `scrape` | Lists every region, municipality and settlement, then downloads new or changed archives into `data/doc/` | `scrape.sh` |
| `extract` | Unpacks archives into `data/extracted/<settlement-EKATTE>/` | `doc1/` step of `unpack.sh` |
| `ownership` | Converts `*ПИ.xlsx`, `*сгради.xlsx` and `*СОС.xlsx` to TSV and concatenates them into `data/ownership/ownership-{parcels,buildings,units}.tsv` | `ssconvert` + `mlr` step |
| `geojson` | Reprojects every shapefile to WGS84 GeoJSON (6-decimal precision) in `data/geojson/` | `mapshaper` step |
| `package` | Builds `Bulgaria-kais-{index,docs,ownership,geojson}.zip` in `data/dist/` | `zip -9r` steps |
| `all` | Runs all of the above in order | |

Run `python -m kais_scraper <command> --help` to see every option. These are the ones you are most likely to use:

| Option | Default | Notes |
|---|---|---|
| `--curl-file PATH` | none | Session from "Copy as cURL (bash)" |
| `--concurrency N` | 8 | Parallel archive downloads |
| `--index-concurrency N` | 4 | Parallel index requests |
| `--rate R` | 1 | Overall requests per second, `0` = unlimited |
| `--blocked-delay S` | 30 | Initial pause after a firewall 403 (doubles while blocks continue) |
| `--region TEXT` | all | Only process regions whose path contains TEXT (repeatable) |
| `--refresh-index` | off | Refetch municipality and settlement lists (see below) |
| `--adopt-existing` | off | Accept valid archives already in `doc/` without redownloading |
| `--dry-run` | off | Fetch the index and list what would be downloaded |
| `--workers N` | CPU count | Parallelism for extract, ownership and geojson |
| `--encoding` | auto | DBF encoding when a shapefile has no `.cpg`, e.g. `cp1251` |
| `--source-crs` | from `.prj` | CRS for shapefiles without a `.prj` |

Exit codes: `0` means success, `1` means some items failed (rerun to retry them), `2` means the session was rejected, `3` means too many consecutive failures, `4` means the run was outside the time window, and `5` means the portal could not be reached.

## How incremental updates work

Each run fetches the region list and every settlement's document list fresh. The document lists carry the `Modified` date for each archive. `data/state/manifest.json` records, for every archive, the `Modified` value and the file size at download time. An archive is downloaded again only when its `Modified` value changes, when the local file is missing, or when the file size no longer matches.

Municipality and settlement lists rarely change, so they are cached in `data/index/`. Run with `--refresh-index` every so often, for example monthly, to pick up new settlements.

All downloads are written to a `.part` file first. Zips get a full CRC check before being renamed into place, so an interrupted run never leaves a truncated file that looks valid.

## Migrating from the bash scripts

Archive file names follow the same rules as before. To avoid downloading roughly 6 GB again:

```bash
mkdir -p data && cp -r /path/to/old/doc data/doc
python -m kais_scraper scrape --curl-file session.curl --adopt-existing
```

Valid archives already present are recorded in the manifest with their current `Modified` date. Only archives that are missing or corrupt get downloaded. The old `index/` folder and the `LASTSCRAPE` file are not needed.

## Fixes compared with the bash version

- **Changed archives are now detected.** The old scripts cached the document lists forever, so their `Modified` dates went stale and changed archives were never seen. Document lists are now refetched on every run, and changes are tracked per file instead of by one global timestamp.
- **Error pages are no longer saved as data.** Before, `curl -s` saved HTTP error bodies, and a non-empty HTML error saved as `.json` was never retried. Responses are now checked for status code, content type and valid JSON or zip, with exponential-backoff retries.
- **Paths containing `'` no longer break requests.** They used to break the `eval`'d curl strings. Parameters are now encoded by the HTTP client.
- **Interrupted runs are no longer marked complete.** Before, `LASTSCRAPE` was written even when downloads had failed. Now only successful downloads are recorded.
- **The ownership header is generated correctly.** `unpack.sh` had a typo (`mlr … pqut`) that broke the header. Headers are now checked per category, with a warning if files disagree.
- **Concurrency is exact.** The `ps | grep` polling loop, which counted its own `grep` and slept in 3-second steps, is replaced by a semaphore.
- **Connections are reused.** A single keep-alive connection pool replaces one TLS handshake per file.
- **Unpacking is safer and incremental.** Zip entries that would escape the target directory are skipped. Unchanged files are not re-extracted, and spreadsheets and shapefiles are only reconverted when their source changes.

## Data layout

```
data/
  index/       cached portal listings (*.0-3.json)
  doc/         downloaded archives
  state/       manifest.json
  extracted/   unpacked archives, one directory per settlement
  ownership/   parts/<category>/*.tsv and combined ownership-*.tsv
  geojson/     *.geojson
  dist/        Bulgaria-kais-*.zip
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests include an end-to-end scrape against a mocked portal (`tests/test_scrape.py`) that covers retries, incremental updates and session rejection. They don't need network access.

Project structure:

```
kais_scraper/
  cli.py         argument parsing and commands
  auth.py        curl parsing and session bootstrap
  client.py      HTTP calls, retries, rate limiting, failure breaker
  scrape.py      index walk and download orchestration
  manifest.py    per-archive download state
  naming.py      portal path to local file name rules
  extract.py     archive unpacking
  ownership.py   xlsx to TSV conversion
  geo.py         shapefile to GeoJSON conversion
  package.py     distributable zips
```

## Known assumptions

The portal's API is undocumented. This tool assumes the same request shapes the bash scripts used:

- `POST /bg/OpenData/Read` with an empty body lists the regions.
- `POST /bg/OpenData/Read` with `target=<path>` lists the children of that path.
- `GET /bg/OpenData/Download?path=<path>` returns the archive.

If the portal changes, `client.py` is the only place that needs updating.
