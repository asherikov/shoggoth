"""Module split out of shoggoth.py; see package __init__."""
import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .util import log
from .state import params

class HttpApiClient:
    """Base class for token-authenticated JSON REST clients.

    Subclasses must set ``self.api_url`` (the API base URL) and may set
    ``self.token`` in their own ``__init__``. The auth scheme is wired
    by overriding ``_auth_header`` to return the API-specific auth
    header dict (e.g. ``{"Authorization": "token ..."}`` for Gitea,
    ``{"X-Redmine-API-Key": "..."}`` for Redmine). Return an empty dict
    when no token is configured — the per-method URL construction and
    HTTP plumbing are inherited from this class.
    """

    # Default ``urlopen`` timeout for every request issued through
    # this client. Read by the static-method helpers (``http_get``,
    # ``http_post_json``, ``http_status``) so it must be a class
    # attribute, not an instance one. Bumped in derived classes
    # only when an endpoint genuinely needs more headroom (none
    # currently do).
    HTTP_TIMEOUT = 30

    @staticmethod
    def _http_warn(label, exc, body_chars=500):
        """Emit the canonical WARNING line for an HTTP failure.

        `label` is the operation prefix, e.g. ``"HTTP GET https://..."`` or
        ``"Redmine admin session POST https://..."``. ``body_chars`` caps the
        response-body fragment included with HTTPError messages (default 500;
        set to 2000 for endpoints whose bodies carry useful diagnostic data,
        e.g. GitHub API error envelopes).

        Always returns None so callers can ``return _http_warn(...)`` from
        their except blocks.
        """
        if isinstance(exc, HTTPError):
            try:
                body = exc.read().decode(errors="replace")[:body_chars]
            except Exception:
                body = ""
            print(f"WARNING: {label} failed: {exc.code} {exc.reason}: {body}",
                  file=sys.stderr)
        elif isinstance(exc, (URLError, OSError)):
            print(f"WARNING: {label} failed: {exc}", file=sys.stderr)
        elif isinstance(exc, json.JSONDecodeError):
            print(f"WARNING: {label} returned invalid JSON: {exc}",
                  file=sys.stderr)
        else:
            print(f"WARNING: {label} failed: {exc!r}", file=sys.stderr)
        return None

    @staticmethod
    def http_get(url, headers=None, params=None):
        if params:
            url = f"{url}?{urlencode(params)}"
        log(f"GET {url}")
        req = Request(url, headers=headers or {})
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                log(f"GET {url} -> {resp.status}")
                return data
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
            return HttpApiClient._http_warn(f"HTTP GET {url}", e)

    @staticmethod
    def _json_request(method, url, payload, headers=None, quiet=False):
        """Build a JSON Request for ``method`` (``POST``/``PATCH``/``PUT``/``DELETE``).

        Centralises the (data, headers, Request) construction that every JSON
        HTTP wrapper below repeats. ``payload`` may be ``None`` for endpoints
        that accept a JSON body or no body interchangeably; callers that need
        a real request without a body should use ``http_delete`` instead.
        When ``quiet`` is set the pre-send method log is suppressed (matches
        the original ``http_post_json(quiet=True)`` behaviour).
        """
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        data = json.dumps(payload).encode() if payload is not None else None
        if not quiet:
            log(f"{method} {url}")
        return Request(url, data=data, headers=hdrs, method=method)

    @staticmethod
    def http_post_json(url, payload, headers=None, quiet=False):
        req = HttpApiClient._json_request("POST", url, payload, headers, quiet=quiet)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                result = json.loads(resp.read().decode())
                if not quiet:
                    log(f"POST {url} -> {resp.status}")
                return result
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
            return HttpApiClient._http_warn(f"HTTP POST {url}", e)

    @staticmethod
    def http_post_json_with_status(url, payload, headers=None):
        req = HttpApiClient._json_request("POST", url, payload, headers)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                body = resp.read().decode(errors="replace")[:2000]
                log(f"POST {url} -> {resp.status}")
                try:
                    return resp.status, body, json.loads(body) if body else None
                except json.JSONDecodeError:
                    return resp.status, body, None
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:2000]
            log(f"POST {url} -> {e.code}")
            return e.code, body, None
        except (URLError, OSError) as e:
            return 0, str(e), None

    @staticmethod
    def http_status(url, headers=None):
        req = Request(url, headers=headers or {}, method="GET")
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                return resp.status
        except HTTPError as e:
            return e.code
        except (URLError, OSError):
            return 0

    @staticmethod
    def http_get_with_status(url, headers=None):
        """GET ``url`` and return ``(status_code, parsed_json_or_None)``.

        Returns ``(0, None)`` on network error, ``(status, None)``
        for non-JSON bodies, ``(status, body_dict)`` for JSON bodies.
        The body is best-effort — callers that only need the status
        code should use ``http_status`` instead. This is the
        counterpart used by the token-validation checks (e.g.
        ``SlaveToken._api_rejects_token``) where the operator
        wants to know *who* the stored token authenticates as,
        not just that the call succeeded.
        """
        req = Request(url, headers=headers or {}, method="GET")
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                raw = resp.read().decode(errors="replace")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, None
        except HTTPError as e:
            return e.code, None
        except (URLError, OSError):
            return 0, None

    def _auth_header(self):
        return {}

    def _headers(self):
        """Default headers for token-authenticated JSON requests.

        Includes Content-Type/accept because every public Gitea/Redmine
        endpoint expects application/json. Endpoints that need a
        different Accept (e.g. ``.diff``) build their own Request and
        pass headers via ``_auth_header`` directly.
        """
        hdrs = {"Content-Type": "application/json",
                "accept": "application/json"}
        hdrs.update(self._auth_header())
        return hdrs

    def _resolve(self, path, headers=None):
        """Return ``(url, hdrs)`` for ``path``: prepends ``api_url`` and
        falls back to ``self._headers()`` when ``headers`` is not given.

        Used by every public HTTP method on this class so the
        URL-composition / header-defaulting pair lives in one place.
        """
        url = f"{self.api_url}/{path}"
        hdrs = headers if headers is not None else self._headers()
        return url, hdrs

    def _send_json(self, method, path, payload, headers=None):
        """Send a JSON ``method`` and return the parsed response body.

        Returns ``{}`` on an empty body and re-raises on HTTP/URL/OS
        errors and on JSONDecodeError after logging the canonical
        ``HTTP <method> <url>`` warning via ``_http_warn``. Shared by
        ``patch`` and ``put_json`` which both want a parsed body (or
        raise) and were previously duplicated line-for-line apart
        from the verb.
        """
        url, hdrs = self._resolve(path, headers)
        req = self._json_request(method, url, payload, hdrs)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                body = resp.read().decode()
                log(f"{method} {url} -> {resp.status}")
                if not body:
                    return {}
                return json.loads(body)
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
            self._http_warn(f"HTTP {method} {url}", e)
            raise

    def _send_status(self, method, path, payload=None, headers=None):
        """Send a JSON ``method`` and return ``True`` on success.

        On HTTP/URL/OS error logs the canonical warning and returns the
        ``_http_warn`` return value (``None`` — falsy). Used by ``put``
        and ``delete``: both only need the success/failure signal and
        were duplicated apart from the verb. ``payload`` defaults to
        ``None`` so ``delete`` (no body) and ``put`` (optional body)
        share the same plumbing.
        """
        url, hdrs = self._resolve(path, headers)
        req = self._json_request(method, url, payload, hdrs)
        try:
            with urlopen(req, timeout=HttpApiClient.HTTP_TIMEOUT) as resp:
                resp.read()
                log(f"{method} {url} -> {resp.status}")
                return True
        except (HTTPError, URLError, OSError) as e:
            return self._http_warn(f"HTTP {method} {url}", e)

    def get(self, path, params=None, headers=None):
        url, hdrs = self._resolve(path, headers)
        return self.http_get(url, headers=hdrs, params=params)

    def post(self, path, payload, headers=None):
        url, hdrs = self._resolve(path, headers)
        return self.http_post_json(url, payload, headers=hdrs)

    def patch(self, path, payload, headers=None):
        return self._send_json("PATCH", path, payload, headers)

    def put_json(self, path, payload, headers=None):
        """PUT that returns the parsed response body (raises on HTTP error).

        Mirrors ``post``/``patch`` (which return bodies); the boolean
        variant ``put`` is the sibling for endpoints that only need the
        success signal.
        """
        return self._send_json("PUT", path, payload, headers)

    def put(self, path, payload=None, headers=None):
        """PUT without consuming a response body; returns True on success.

        Parallel to ``delete`` (also returns a boolean). Used by
        endpoints like ``PUT /teams/{id}/members/{user}`` where the
        success path only needs the status code, not a parsed body.
        """
        return self._send_status("PUT", path, payload, headers)

    def delete(self, path, payload=None, headers=None):
        return self._send_status("DELETE", path, payload, headers)

    def status(self, path, headers=None):
        url, hdrs = self._resolve(path, headers)
        return self.http_status(url, headers=hdrs)

    @staticmethod
    def _paginate(fetch_page, limit=50):
        """Drive a paginated JSON endpoint.

        ``fetch_page(page, limit)`` is called repeatedly with
        incrementing ``page`` until it returns None or an empty
        iterable. Iteration stops early when a page returns fewer than
        ``limit`` items (the API considered that the last page) so
        callers don't have to inspect the response to detect the
        terminal page.

        Marked staticmethod because it doesn't touch any per-instance
        state — it's a generic pagination loop shared by every
        subclass's list endpoint (``Gitea.list_orgs``, ``Gitea.list_branches``,
        ``Github.list_repos``, etc.). Static so subclasses can't
        accidentally inherit a hook into ``self``.

        Returns ``[]`` on transport failure (the first ``data is
        None`` collapses to an empty accumulator) — callers that
        must distinguish "API succeeded with empty list" from
        "API failed entirely" cannot use this helper directly; use
        ``get_list`` instead, which returns ``(items, error)``.
        """
        results = []
        page = 1
        while True:
            data = fetch_page(page, limit)
            if data is None or not data:
                break
            results.extend(data)
            if len(data) < limit:
                break
            page += 1
        return results

    def get_list(self, path, params=None, limit=50):
        """GET a paginated list endpoint and return ``(items, error)``.

        Distinguishes three outcomes the plain ``_paginate`` loop
        collapses:

        * API transport failure (``HTTPError`` / ``URLError`` /
          ``OSError`` / ``JSONDecodeError``) → ``(None, error_str)``
        * API responded but the body is not a list
          (an HTML error page, a redirect object, etc.) →
          ``(None, error_str)``
        * API succeeded with N items (N may be 0) →
          ``(items, None)``

        Callers branch on ``error is not None``: a transport failure
        is a hard error (the maintenance script must ``die()`` so the
        Argo step reports it); an empty list on a list endpoint is
        just a fact about the org / repo. ``_paginate`` collapses the
        two to ``[]`` and the old ``if not repos: continue`` paths
        silently skipped the org — the bug the
        ``SlaveAccess._add_to_mirrored_repos`` /
        ``_ensure_force_push_access`` defensive checks were meant
        to surface. ``list_org`` etc. remain on the class for
        callers that legitimately do not need to distinguish; new
        callers use ``get_list``.
        """
        page = 1
        items = []
        try:
            while True:
                p = {"page": page, "limit": limit}
                if params:
                    p.update(params)
                batch = self.get(path, params=p)
                if batch is None:
                    return None, f"GET {path} transport failure"
                if not isinstance(batch, list):
                    return None, (f"GET {path} returned non-list: "
                                  f"{type(batch).__name__}")
                items.extend(batch)
                if len(batch) < limit:
                    break
                page += 1
        except Exception as e:
            return None, f"GET {path} raised {type(e).__name__}: {e}"
        return items, None
