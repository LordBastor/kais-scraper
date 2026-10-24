import pytest

from kais_scraper.auth import AuthError, find_csrf_token, parse_curl

CURL = r"""curl 'https://kais.cadastre.bg/bg/OpenData/Read' \
  -H 'Accept: */*' \
  -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
  -H $'Cookie: csrf=abc; cookiesgdpr=true; k-cookie=\u0021xyz==' \
  -H 'X-CSRF-TOKEN: tok' \
  -H 'User-Agent: TestAgent' \
  --data-raw 'path=x&target=x'
"""


def test_parse_curl():
    creds = parse_curl(CURL)
    assert creds.cookies == {"csrf": "abc", "cookiesgdpr": "true", "k-cookie": "!xyz=="}
    assert creds.headers["X-CSRF-TOKEN"] == "tok"
    assert creds.headers["User-Agent"] == "TestAgent"
    assert "Content-Type" not in creds.headers


def test_parse_curl_without_cookies():
    with pytest.raises(AuthError):
        parse_curl("curl 'https://example.com' -H 'X-CSRF-TOKEN: t'")


def test_find_csrf_token():
    page = '<form><input name="__RequestVerificationToken" type="hidden" value="CfDJ8&amp;x" /></form>'
    assert find_csrf_token(page) == "CfDJ8&x"
    assert find_csrf_token("<html></html>") is None
