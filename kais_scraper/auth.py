"""Session setup for kais.cadastre.bg: either parse a 'Copy as cURL' command or bootstrap automatically."""
from __future__ import annotations

import html
import logging
import re
import shlex
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://kais.cadastre.bg"
COOKIE_DOMAIN = "kais.cadastre.bg"
OPENDATA_PAGE = f"{BASE_URL}/bg/OpenData"
READ_URL = f"{OPENDATA_PAGE}/Read"
DOWNLOAD_URL = f"{OPENDATA_PAGE}/Download"

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "bg,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": OPENDATA_PAGE,
    "X-Requested-With": "XMLHttpRequest",
}

# Headers from a copied curl command that httpx must manage itself (or that are per-request).
_SKIP_HEADERS = {"content-length", "content-type", "host", "connection", "accept-encoding", "cookie"}

_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "0": "\0"}

_TOKEN_PATTERNS = [
    re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"'),
    re.compile(r'value="([^"]+)"[^>]*name="__RequestVerificationToken"'),
    re.compile(r'<meta[^>]+name="(?:csrf-token|X-CSRF-TOKEN|RequestVerificationToken)"[^>]+content="([^"]+)"', re.I),
    re.compile(r"""['"]X-CSRF-TOKEN['"]\s*[:,]\s*['"]([^'"]+)['"]""", re.I),
]


class AuthError(RuntimeError):
    """The portal rejected the session (expired cookies or CSRF token)."""


@dataclass
class Credentials:
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)

    def apply(self, client: httpx.AsyncClient) -> None:
        client.headers.update(self.headers)
        for name, value in self.cookies.items():
            client.cookies.set(name, value, domain=COOKIE_DOMAIN)


def _unescape_ansi_c(body: str) -> str:
    def repl(m: re.Match[str]) -> str:
        esc = m.group(1)
        if esc[0] in "ux" and len(esc) > 1:
            return chr(int(esc[1:], 16))
        return _SIMPLE_ESCAPES.get(esc, "\\" + esc)
    return _ESCAPE.sub(repl, body)


def parse_cookie_header(value: str) -> dict[str, str]:
    cookies = {}
    for part in value.split(";"):
        name, sep, val = part.strip().partition("=")
        if sep and name:
            cookies[name] = val
    return cookies


def parse_curl(text: str) -> Credentials:
    """Extract headers and cookies from Chrome/Firefox 'Copy as cURL (bash)' output."""
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    # Chrome uses bash $'...' quoting when a value contains special characters.
    text = _ANSI_C.sub(lambda m: shlex.quote(_unescape_ansi_c(m.group(1))), text)
    tokens = shlex.split(text)

    creds = Credentials()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            name, _, value = tokens[i + 1].partition(":")
            name, value = name.strip(), value.strip()
            if name.lower() == "cookie":
                creds.cookies.update(parse_cookie_header(value))
            elif name.lower() not in _SKIP_HEADERS:
                creds.headers[name] = value
            i += 2
        elif tok in ("-b", "--cookie") and i + 1 < len(tokens):
            creds.cookies.update(parse_cookie_header(tokens[i + 1]))
            i += 2
        else:
            i += 1

    if not creds.cookies:
        raise AuthError("No cookies found in the curl command; copy it with 'Copy as cURL (bash)'.")
    if not any(k.lower() == "x-csrf-token" for k in creds.headers):
        log.warning("The curl command has no X-CSRF-TOKEN header; requests will probably be rejected.")
    return creds


def find_csrf_token(page: str) -> str | None:
    for pattern in _TOKEN_PATTERNS:
        m = pattern.search(page)
        if m:
            return html.unescape(m.group(1))
    return None


async def bootstrap(client: httpx.AsyncClient) -> Credentials:
    """Best effort: open the OpenData page, keep its cookies and pick up the CSRF token."""
    r = await client.get(OPENDATA_PAGE, headers={"Accept": "text/html,application/xhtml+xml"})
    if r.status_code >= 400:
        raise AuthError(f"Opening {OPENDATA_PAGE} returned HTTP {r.status_code}.")
    token = find_csrf_token(r.text)
    if token is None:
        raise AuthError("Could not find a CSRF token on the OpenData page.")
    return Credentials(headers={"X-CSRF-TOKEN": token})
