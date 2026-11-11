"""Check HTTPS certificates against the operating system's own trusted list.

Python's OpenSSL looks for certificates where the Python it came with was built. In the
downloadable app that's a folder on the machine that built it, missing on most others
(every Mac, and Linux other than Debian or Ubuntu), so every HTTPS server was refused.
`truststore` asks macOS, Windows or Linux instead, which also honours certificates an
organisation has added to its computers. Every HTTPS connection passes this context
explicitly: aiohttp makes its default one when first imported, so patching `ssl` later
would come too late.
"""

from __future__ import annotations

import logging
import ssl

log = logging.getLogger(__name__)
_context: ssl.SSLContext | None = None


def context() -> ssl.SSLContext:
    global _context
    if _context is None:
        try:
            import truststore

            _context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception as e:  # noqa: BLE001 - fall back to Python's own list rather than fail
            log.warning("couldn't use the system's certificates (%s); using Python's", e)
            _context = ssl.create_default_context()
    return _context


def connector():
    """An aiohttp connector that checks certificates with `context()` (call inside the event loop)."""
    import aiohttp

    return aiohttp.TCPConnector(ssl=context())
