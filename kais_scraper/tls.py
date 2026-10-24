"""TLS settings the portal's firewall accepts.

The firewall in front of kais.cadastre.bg resets any handshake whose ALPN list is
only ["http/1.1"], which is exactly what httpx/httpcore send. Handshakes offering
["h2", "http/1.1"] (like curl) or no ALPN at all go through. This context always
offers h2 first; the client runs with http2=True so either negotiated protocol works.
"""
from __future__ import annotations

import ssl


class CurlLikeContext(ssl.SSLContext):
    def set_alpn_protocols(self, alpn_protocols):  # called by httpcore per connection
        protocols = list(dict.fromkeys(["h2", *alpn_protocols, "http/1.1"]))
        super().set_alpn_protocols(protocols)


def portal_ssl_context() -> ssl.SSLContext:
    """Same verification as ssl.create_default_context(): system trust store, hostname checks."""
    ctx = CurlLikeContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_default_certs()
    ctx.set_alpn_protocols([])
    return ctx
