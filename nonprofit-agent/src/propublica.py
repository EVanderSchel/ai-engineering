"""Client for the ProPublica Nonprofit Explorer API (https://projects.propublica.org/nonprofits/api).

Free and keyless, so be polite: at most about one request per second, a few retries with growing
pauses on network errors and 5xx/429 responses, and every successful response is cached in
data/cache/ and reused. Only the JSON API is used; ProPublica's website and download links are
bot-protected, and this project doesn't work around that.
"""

import hashlib
import json
import time

import requests

from paths import CACHE_DIR

API_BASE = "https://projects.propublica.org/nonprofits/api/v2"
HEADERS = {"User-Agent": "nonprofit-agent (learning project; github.com/EVanderSchel/ai-engineering)"}
MIN_SECONDS_BETWEEN_REQUESTS = 1.0

_last_request = 0.0


class NotFound(LookupError):
    """The API answered 404: no such organization."""


def _wait_turn() -> None:
    """Sleep until at least MIN_SECONDS_BETWEEN_REQUESTS has passed since the last request."""
    global _last_request
    wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _get(url: str, params: dict, *, attempts: int = 4) -> requests.Response:
    """GET with spacing and retries on network errors and 5xx/429 responses."""
    for attempt in range(1, attempts + 1):
        _wait_turn()
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException:
            if attempt == attempts:
                raise
        else:
            if response.status_code < 500 and response.status_code != 429:
                return response
            if attempt == attempts:
                response.raise_for_status()
        time.sleep(2**attempt)  # 2, 4, 8 s
    raise AssertionError("unreachable")


def cache_path(path: str, params: dict):
    """One file per distinct request: the endpoint plus its sorted parameters, hashed into a filename."""
    key = json.dumps([path, sorted(params.items())])
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"


def fetch(path: str, params: dict | None = None) -> dict:
    """The API's JSON for `path` (e.g. "/search.json"), from the cache if it was fetched before.
    Raises NotFound on 404; errors are never cached, so a later call tries again."""
    params = params or {}
    cached = cache_path(path, params)
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    response = _get(API_BASE + path, params)
    if response.status_code == 404:
        raise NotFound(path)
    response.raise_for_status()
    data = response.json()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data), encoding="utf-8")
    return data


def search(query: str, *, state: str | None = None, page: int = 0) -> dict:
    """One page (25 results, numbered from 0) of organizations matching `query`."""
    params = {"q": query, "page": page}
    if state:
        params["state[id]"] = state
    return fetch("/search.json", params)


def organization(ein: str) -> dict:
    """An organization's profile and its filings ("filings_with_data" carry financial figures;
    "filings_without_data" only a PDF link, which includes the newest filings not yet processed)."""
    return fetch(f"/organizations/{ein}.json")
