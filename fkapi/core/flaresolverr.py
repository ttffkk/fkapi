"""FlareSolverr-backed fetching.

footballkitarchive.com sits behind Cloudflare's "challenge" bot mitigation, so a
plain ``requests`` GET is answered with a 403 challenge page regardless of the
User-Agent or (HTTP) proxy used — solving it needs a real browser. When
``FLARESOLVERR_URL`` is set we route the "use a proxy" code path through a
FlareSolverr instance (https://github.com/FlareSolverr/FlareSolverr), which
drives a headless browser to solve the challenge and returns the real HTML.

The public helper returns a normal ``requests.Response`` so the rest of the
scraper (which reads ``.status_code`` / ``.text`` / ``.json()``) is unchanged.
"""

from __future__ import annotations

import os

import requests

# e.g. "http://flaresolverr:8191/v1". Unset => FlareSolverr disabled (callers
# fall back to the plain-proxy path, preserving upstream behaviour).
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL")

# Browser solve budget (seconds). Cloudflare challenges routinely take >15s, so
# this is deliberately well above the plain-request DEFAULT_TIMEOUT.
FLARESOLVERR_TIMEOUT = float(os.getenv("FLARESOLVERR_TIMEOUT", "60"))


def flaresolverr_enabled() -> bool:
    return bool(FLARESOLVERR_URL)


def fetch_via_flaresolverr(url: str, *, timeout: float | None = None) -> requests.Response:
    """Fetch ``url`` through FlareSolverr and adapt the result to a Response.

    Raises ``requests.exceptions.RequestException`` if FlareSolverr itself is
    unreachable or returns a malformed payload — the scrapers already treat that
    as a retryable network error.
    """
    budget = timeout if timeout is not None else FLARESOLVERR_TIMEOUT
    payload = {"cmd": "request.get", "url": url, "maxTimeout": int(budget * 1000)}

    # Give the HTTP call headroom over the browser's own solve budget.
    fs_resp = requests.post(FLARESOLVERR_URL, json=payload, timeout=budget + 15)
    fs_resp.raise_for_status()

    try:
        data = fs_resp.json()
    except ValueError as exc:  # noqa: TRY003 - surface as retryable network error
        raise requests.exceptions.RequestException(
            f"FlareSolverr returned non-JSON payload for {url}"
        ) from exc

    solution = data.get("solution") or {}
    resp = requests.Response()
    # data["status"] is FlareSolverr's own ok/error; solution["status"] is the
    # upstream HTTP status. Prefer the upstream status; fall back to 502 when
    # FlareSolverr failed to solve so callers treat it as a failure.
    if data.get("status") != "ok":
        resp.status_code = int(solution.get("status") or 502)
    else:
        resp.status_code = int(solution.get("status") or 200)
    resp._content = (solution.get("response") or "").encode("utf-8")
    resp.encoding = "utf-8"
    resp.url = solution.get("url") or url
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp
