"""Module split out of shoggoth.py; see package __init__."""
import json
import os
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..state import params
from ..util import die

MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json"
)

INDEX_MEDIA_TYPES = frozenset({
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
})

# A manifest GET drives zot's on-demand sync, which runs on a context
# detached from the request with its own budget (3h by default), so it
# commits even after the client that started it disconnects: a client
# timeout is not a sync failure, and the image may still land in the
# cache. A single-arch image manifest is answered only once the whole
# image (config + layers) has been copied; an index/list is answered as
# soon as its root manifest is cached, with the per-arch children left
# uncached (see _pull_manifest). The DELETE only drops the tag
# reference and answers immediately.
PULL_TIMEOUT = 600
DELETE_TIMEOUT = 60

# Total time one manifest request -- the tag itself, or one index child
# -- may take: the first PULL_TIMEOUT attempt plus the wait for zot's
# detached sync to commit. The default covers zot's 3h sync budget plus
# the initial attempt; the workflow's activeDeadlineSeconds remains the
# outer bound. Override with SHOGGOTH_REGISTRY_PULL_WAIT (seconds).
PULL_WAIT = PULL_TIMEOUT + 3 * 3600
POLL_INTERVAL = 15

# 404 is the "tag not cached" case and is idempotent, matching
# ansible/drop_image_tag.yml; any other code is a failure.
DELETE_ACCEPTED = {200, 202, 204, 404}


def _env_seconds(name, default):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        die(f"container-pull: {name} must be an integer number of "
            f"seconds, got {raw!r}")
    if value < PULL_TIMEOUT:
        die(f"container-pull: {name} must be at least {PULL_TIMEOUT} "
            f"(the first attempt's timeout), got {value}")
    return value


class ContainerPull:
    def __init__(self):
        self.action = os.environ.get("SHOGGOTH_REGISTRY_ACTION", "")
        self.images = os.environ.get("SHOGGOTH_REGISTRY_IMAGES", "")
        self.registry_url = f"http://registry:{params.shoggoth_registry_port}"
        self.pull_wait = _env_seconds("SHOGGOTH_REGISTRY_PULL_WAIT", PULL_WAIT)
        self.failures = []

    def execute(self):
        if self.action not in ("evict", "repull"):
            die("container-pull: SHOGGOTH_REGISTRY_ACTION must be "
                f"'evict' or 'repull', got {self.action!r}")
        refs = [ref.strip() for ref in self.images.split(",") if ref.strip()]
        if not refs:
            die("container-pull: SHOGGOTH_REGISTRY_IMAGES is empty; pass "
                "a comma-separated list of repo:tag refs")
        for ref in refs:
            self._refresh(ref)
        if self.failures:
            die("container-pull: "
                f"{len(self.failures)} of {len(refs)} ref(s) failed:\n"
                + "\n".join(f"  - {ref}: {reason}"
                            for ref, reason in self.failures))

    def _refresh(self, ref):
        path = self._manifest_path(ref)
        if self.action == "evict":
            status, _, _, _ = self._request("DELETE", path, DELETE_TIMEOUT)
            if status in DELETE_ACCEPTED:
                if status == 404:
                    print(f"not cached {ref} (HTTP 404)")
                else:
                    print(f"evicted {ref} (HTTP {status})")
            else:
                self._fail(ref, f"DELETE returned HTTP {status}")
        else:
            self._repull(ref)

    def _repull(self, ref):
        name, reference = self._split_ref(ref)
        self._pull_manifest(ref, name, reference, set(), ref)

    def _pull_manifest(self, ref, name, reference, seen, label):
        """GET one manifest and, for an index, every child manifest.

        zot's on-demand sync answers a GET for a single-arch image only
        after copying the whole image, but an index/list as soon as its
        root is cached -- the per-arch children are fetched separately.
        GETting each child by digest drives a full sync of that image
        (config + layers), so success here means the image is really in
        the cache and not just its manifest list.
        """
        path = f"/v2/{name}/manifests/{reference}"
        status, body, media_type, timed_out = self._get_manifest(path, label)
        if status != 200:
            self._fail(label, self._reason(status, timed_out))
            return
        print(f"pulled {label} (HTTP 200)")
        for digest in self._index_children(media_type, body):
            if digest in seen:
                continue
            seen.add(digest)
            self._pull_manifest(ref, name, digest, seen,
                                f"{ref} child {digest}")

    def _get_manifest(self, path, label):
        """GET one manifest, waiting out zot's detached sync on timeout.

        The sync outlives the client that started it, so a timeout is
        followed by re-requests until the manifest resolves or the
        budget expires: a request that arrives while the sync is in
        flight joins it and is answered when it commits. Any completed
        response -- including an error status -- is final.
        """
        deadline = time.monotonic() + self.pull_wait
        waiting = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0, b"", "", True
            status, body, media_type, timed_out = self._request(
                "GET", path, min(PULL_TIMEOUT, remaining))
            if status != 0 or not timed_out:
                return status, body, media_type, timed_out
            if not waiting:
                waiting = True
                print(f"waiting for {label}: zot's on-demand sync "
                      f"continues in the background, re-checking every "
                      f"{POLL_INTERVAL}s")
            time.sleep(POLL_INTERVAL)

    def _reason(self, status, timed_out):
        if timed_out:
            return f"GET timed out after {self.pull_wait}s"
        if status == 0:
            return "GET returned HTTP 0"
        return f"GET returned HTTP {status}"

    @staticmethod
    def _index_children(media_type, body):
        """Child manifest digests of an index/list; empty for an image."""
        if media_type.split(";")[0].strip() not in INDEX_MEDIA_TYPES:
            return []
        try:
            manifests = json.loads(body).get("manifests", [])
        except ValueError:
            return []
        return [m["digest"] for m in manifests if "digest" in m]

    def _fail(self, ref, reason):
        self.failures.append((ref, reason))
        print(f"ERROR: {ref}: {reason}", file=sys.stderr)

    @staticmethod
    def _split_ref(ref):
        # The digest form is checked first: a digest's own colon must not
        # be mistaken for the repo:tag separator.
        if "@" in ref:
            name, _, digest = ref.partition("@")
            return name, digest
        name, sep, tag = ref.rpartition(":")
        if not sep:
            name, tag = ref, "latest"
        return name, tag

    @staticmethod
    def _manifest_path(ref):
        name, reference = ContainerPull._split_ref(ref)
        return f"/v2/{name}/manifests/{reference}"

    def _request(self, method, path, timeout):
        url = f"{self.registry_url}{path}"
        req = Request(url, method=method,
                      headers={"Accept": MANIFEST_ACCEPT})
        try:
            with urlopen(req, timeout=timeout) as resp:
                return (resp.status, resp.read(),
                        resp.headers.get("Content-Type", ""), False)
        except HTTPError as e:
            return e.code, b"", "", False
        except (URLError, OSError) as e:
            print(f"ERROR: {method} {url} failed: {e}", file=sys.stderr)
            # A read timeout surfaces as a URLError wrapping
            # socket.timeout; anything else (refused, reset, DNS) is a
            # plain connectivity failure that waiting will not fix.
            reason = e.reason if isinstance(e, URLError) else e
            timed_out = isinstance(reason, (socket.timeout, TimeoutError))
            return 0, b"", "", timed_out
