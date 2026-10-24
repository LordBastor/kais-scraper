import asyncio
import io
import json
import zipfile
from urllib.parse import parse_qs

import httpx
import pytest

from kais_scraper.auth import AuthError
from kais_scraper.scrape import ScrapeOptions, run_scrape
from test_auth import CURL

DOC = "област Бургас/община Айтос/гр. Айтос (00151)/00151.zip"


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("00151 ПИ.txt", "hello")
    return buf.getvalue()


class FakePortal:
    def __init__(self):
        self.modified = "2026-01-01T00:00:00"
        self.fail_downloads = 0
        self.reject_auth = False
        self.read_calls = []
        self.block_lists = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-CSRF-TOKEN"] == "tok"
        assert "csrf=abc" in request.headers.get("cookie", "")
        if request.url.path == "/bg/OpenData/Read":
            if self.reject_auth:
                return httpx.Response(400)
            target = parse_qs(request.content.decode()).get("target", [None])[0]
            self.read_calls.append(target)
            if target and "(00151)" in target and self.block_lists:
                self.block_lists -= 1
                return httpx.Response(403, html="<html>Request Rejected. Your support ID is: 123456</html>")
            tree = {
                None: [{"Path": "област Бургас"}],
                "област Бургас": [{"Path": "област Бургас/община Айтос"}],
                "област Бургас/община Айтос": [{"Path": "област Бургас/община Айтос/гр. Айтос (00151)"}],
                "област Бургас/община Айтос/гр. Айтос (00151)": [{"Path": DOC, "Modified": self.modified}],
            }
            return httpx.Response(200, json=tree[target])
        if request.url.path == "/bg/OpenData/Download":
            assert request.method == "GET"
            assert request.url.params["path"] == DOC
            if self.fail_downloads:
                self.fail_downloads -= 1
                return httpx.Response(200, html="<html>try again</html>")
            return httpx.Response(200, content=_zip_bytes(), headers={"content-type": "application/zip"})
        return httpx.Response(404)


@pytest.fixture
def setup(tmp_path):
    portal = FakePortal()
    curl = tmp_path / "session.curl"
    curl.write_text(CURL, encoding="utf-8")
    opts = ScrapeOptions(data_dir=tmp_path / "data", curl_file=curl, retry_base_delay=0, blocked_delay=0, rate=None)

    def run():
        return asyncio.run(run_scrape(opts, transport=httpx.MockTransport(portal.handler)))

    return portal, opts, run


def test_incremental_scrape(setup):
    portal, opts, run = setup
    doc_file = opts.data_dir / "doc" / "Бургас-Айтос-Айтос-00151-00151.zip"

    portal.fail_downloads = 1  # HTML error page first, then the real file
    first = run()
    assert first.ok and first.docs_downloaded == 1
    assert doc_file.exists()
    manifest = json.loads((opts.data_dir / "state" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][DOC]["modified"] == portal.modified

    portal.read_calls.clear()
    second = run()
    assert second.docs_downloaded == 0 and second.docs_current == 1
    assert second.index_cached == 2  # municipalities and settlements come from cache
    assert "област Бургас/община Айтос/гр. Айтос (00151)" in portal.read_calls  # document list refetched

    portal.modified = "2026-02-01T00:00:00"
    third = run()
    assert third.docs_downloaded == 1


def test_adopt_existing(setup):
    portal, opts, run = setup
    doc_dir = opts.data_dir / "doc"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Бургас-Айтос-Айтос-00151-00151.zip").write_bytes(_zip_bytes())
    opts.adopt_existing = True
    res = run()
    assert res.docs_adopted == 1 and res.docs_downloaded == 0


def test_rejected_session(setup):
    portal, _, run = setup
    portal.reject_auth = True
    with pytest.raises(AuthError):
        run()


def test_firewall_403_mid_run_is_retried(setup):
    portal, _, run = setup
    portal.block_lists = 2
    res = run()
    assert res.ok and res.docs_downloaded == 1


def test_global_pause_on_firewall_block(tmp_path):
    """After a block, every request (not just the failed one) must go through the pause and slow-down."""
    import asyncio as aio
    from kais_scraper.client import RateLimiter

    async def scenario():
        lim = RateLimiter(100.0, cooldown=0.2)
        lim.blocked()
        lim.blocked()  # in-flight duplicate: must not double the pause again
        assert lim.rate == 4.0  # halved, and never faster than 4 req/s right after a block
        t0 = aio.get_running_loop().time()
        await aio.gather(*(lim.wait() for _ in range(3)))
        elapsed = aio.get_running_loop().time() - t0
        assert elapsed >= 0.2  # all three waited for the pause
        for _ in range(RateLimiter.RECOVER_EVERY):
            lim.succeeded()
        assert lim.rate > 4.0

    aio.run(scenario())
