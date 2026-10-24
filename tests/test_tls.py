import socket
import ssl
import subprocess
import threading
from pathlib import Path

from kais_scraper.tls import portal_ssl_context


def test_context_verifies_like_default():
    ctx = portal_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_offers_h2_even_when_asked_for_http11_only(tmp_path: Path):
    """A local TLS server that only accepts h2 must see it offered, as the portal's firewall wants."""
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                    "-keyout", str(key), "-out", str(cert), "-days", "1"], check=True, capture_output=True)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    server_ctx.set_alpn_protocols(["h2"])

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass

    threading.Thread(target=serve, daemon=True).start()
    ctx = portal_ssl_context()
    ctx.load_verify_locations(cert)
    ctx.set_alpn_protocols(["http/1.1"])  # what httpcore does
    with socket.create_connection(("127.0.0.1", port)) as raw, \
            ctx.wrap_socket(raw, server_hostname="localhost") as tls:
        assert tls.selected_alpn_protocol() == "h2"
    listener.close()
