"""Module split out of shoggoth.py; see package __init__."""
import json
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .http import HttpApiClient
from .util import log
from .state import params

class Github(HttpApiClient):
    """Unauthenticated GitHub REST client.

    Inherits the JSON REST plumbing from ``HttpApiClient``; only the
    rate-limit retry loop (specific to GitHub's 60-req/hour
    unauthenticated quota) and the unauthenticated accept header
    override ``get``. Future endpoints that don't need rate-limit
    handling (e.g. authenticated POSTs) can use the inherited
    ``post`` / ``put_json`` / ``delete`` directly.

    Per-repo branch/tag listing is done via ``git ls-remote`` (see
    ``GithubMirrorSync``), which does not count against the REST
    quota.
    """

    RATE_LIMIT_MAX_WAIT = 900

    def __init__(self):
        self.api_url = "https://api.github.com"
        self.api_delay = params.shoggoth_github_api_delay

    def get(self, path, params=None):
        # Override the base ``get`` because GitHub's quota policy
        # requires a retry-once-on-429/403 loop that needs the raw
        # ``HTTPError`` (to read ``Retry-After`` / ``X-RateLimit-Reset``
        # headers). The base ``get`` swallows HTTPError after
        # ``_http_warn`` so a 429 is indistinguishable from a 404 —
        # the retry loop has to live here with the urllib plumbing
        # inlined. Other methods (``post`` / ``put_json`` / etc.)
        # remain available from the base class without override.
        url = f"{self.api_url}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        for attempt in (1, 2):
            time.sleep(self.api_delay)
            log(f"GET {url}")
            req = Request(url, headers={"accept": "application/json"})
            try:
                with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode())
                    log(f"GET {url} -> {resp.status}")
                    return data
            except HTTPError as e:
                if attempt == 1 and e.code in (403, 429) and self._wait_for_quota(e, url):
                    continue
                return self._http_warn(f"HTTP GET {url}", e, body_chars=2000)
            except (URLError, OSError, json.JSONDecodeError) as e:
                return self._http_warn(f"HTTP GET {url}", e, body_chars=2000)
        return None

    def _wait_for_quota(self, error, url):
        headers = error.headers or {}
        retry_after = headers.get("Retry-After")
        reset = headers.get("X-RateLimit-Reset")
        try:
            if retry_after is not None:
                wait = float(retry_after)
            elif reset is not None:
                wait = max(0.0, int(reset) - time.time())
            else:
                return False
        except ValueError:
            return False
        if wait > self.RATE_LIMIT_MAX_WAIT:
            return False
        print(f"GitHub rate limit hit, waiting {wait:.0f}s before retrying {url}",
              file=sys.stderr, flush=True)
        time.sleep(wait)
        return True

    def list_repos(self, account, account_type):
        return self._paginate(lambda page, limit: self.get(
            f"{account_type}/{account}/repos",
            params={"page": page, "per_page": limit, "type": "public"}), limit=100)
