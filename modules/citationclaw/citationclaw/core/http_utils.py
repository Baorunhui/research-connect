"""Shared HTTP client factory with proxy auto-detection.

Detects system HTTP proxy and configures httpx accordingly.
SOCKS5 proxies (common in China) are skipped since httpx doesn't
support them natively; only HTTP/HTTPS proxies are used.
"""
import os
import ssl
import httpx
from typing import Optional


def _detect_http_proxy() -> Optional[str]:
    """Detect HTTP proxy from environment, skip SOCKS proxies."""
    for var in ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"]:
        proxy = os.environ.get(var, "")
        if proxy and proxy.startswith("http"):
            return proxy
    return None


def _detect_ca_bundle() -> Optional[str]:
    """Return custom CA bundle path if set via any of the common env vars."""
    for var in ["CA_BUNDLE_PATH", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"]:
        val = os.environ.get(var, "").strip().strip('"').strip("'")
        if val and os.path.exists(val):
            return val
    return None


def make_ssl_context() -> ssl.SSLContext:
    """Build an SSL context capped at TLS 1.2.

    Some hosts (e.g. Semantic Scholar via CloudFront) fail the TLS 1.3
    handshake with newer OpenSSL builds (3.5.x). Capping at TLS 1.2 restores
    compatibility while keeping certificate verification intact.
    """
    ctx = ssl.create_default_context()
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def make_async_client(timeout: float = 30.0, use_proxy: bool = True) -> httpx.AsyncClient:
    """Create an httpx AsyncClient with auto-detected proxy and CA bundle.

    Uses HTTP proxy if available, otherwise direct connection.
    Set use_proxy=False to force direct connection (e.g., for APIs
    that are directly reachable but fail through proxy).
    Respects CA_BUNDLE_PATH / SSL_CERT_FILE for corporate TLS interception.
    """
    proxy = _detect_http_proxy() if use_proxy else None
    ca_bundle = _detect_ca_bundle()
    verify = ca_bundle if ca_bundle else make_ssl_context()
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        verify=verify,
        headers={"User-Agent": "CitationClaw/2.0 (academic research tool)"},
    )
