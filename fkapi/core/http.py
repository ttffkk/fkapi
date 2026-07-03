from __future__ import annotations

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.flaresolverr import fetch_via_flaresolverr, flaresolverr_enabled
from fkapi.proxy import get_proxy

# Default HTTP headers for scraper-like requests
HTTP_DEFAULT_HEADERS = {
    "User-Agent": os.getenv(
        "SCRAPER_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36",
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))


def _retry_strategy() -> Retry:
    return Retry(
        total=int(os.getenv("HTTP_MAX_RETRIES", "3")),
        backoff_factor=float(os.getenv("HTTP_BACKOFF_FACTOR", "0.5")),
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )


def get_session(headers: dict | None = None) -> requests.Session:
    """Return a configured requests.Session with retries and default headers."""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=_retry_strategy())
    session.mount("https://", adapter)

    merged_headers = dict(HTTP_DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)
    session.headers.update(merged_headers)
    return session


SCRAPER_REUSE_SESSION = os.getenv("SCRAPER_REUSE_SESSION", "false").lower() in ("1", "true", "yes")
_SHARED_SESSION = get_session() if SCRAPER_REUSE_SESSION else None


def get_scraper_session() -> requests.Session | None:
    """Return a shared scraper session if reuse is enabled; otherwise None."""
    return _SHARED_SESSION if SCRAPER_REUSE_SESSION else None


def http_get(
    url: str,
    *,
    use_proxy: bool = False,
    timeout: float | None = None,
    session: requests.Session | None = None,
    headers: dict | None = None,
    proxies: dict | None = None,
) -> requests.Response:
    """Helper to GET a URL with sane defaults.

    - Adds default headers
    - Applies retries/backoff via a mounted adapter
    - Routes through FlareSolverr when enabled and use_proxy is requested
      (footballkitarchive.com is behind a Cloudflare browser challenge)
    - Falls back to an optional HTTP proxy via env-configured get_proxy()
    - Enforces a default timeout
    """
    # The scrapers signal "I got blocked, escalate" by retrying with
    # use_proxy=True. When FlareSolverr is configured, that escalation means
    # "solve the Cloudflare challenge in a browser" rather than swap HTTP proxy.
    if proxies is None and use_proxy and flaresolverr_enabled():
        return fetch_via_flaresolverr(url, timeout=timeout)

    sess = session or get_scraper_session()
    if sess is None:
        sess = get_session(headers=headers)
    elif headers:
        # Allow per-request headers without mutating shared session defaults
        request_headers = dict(sess.headers)
        request_headers.update(headers)
        headers = request_headers

    if proxies is None and use_proxy:
        proxies = get_proxy()

    return sess.get(url, timeout=timeout or DEFAULT_TIMEOUT, proxies=proxies, headers=headers)
