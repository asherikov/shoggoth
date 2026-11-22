"""Module split out of shoggoth.py; see package __init__."""
import os
import sys
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

# The manifest GET drives zot's on-demand sync, which fetches the
# whole image before answering, so a large image can hold the request
# far longer than a JSON API call. The DELETE only drops the tag
# reference and answers immediately.
PULL_TIMEOUT = 600
DELETE_TIMEOUT = 60

# 404 is the "tag not cached" case and is idempotent, matching
# ansible/drop_image_tag.yml; any other code is a failure.
DELETE_ACCEPTED = {200, 202, 204, 404}

class ContainerPull:
    def __init__(self):
        self.action = os.environ.get("SHOGGOTH_REGISTRY_ACTION", "")
        self.images = os.environ.get("SHOGGOTH_REGISTRY_IMAGES", "")
        self.registry_url = f"http://registry:{params.shoggoth_registry_port}"
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
            status = self._request("DELETE", path, DELETE_TIMEOUT)
            if status in DELETE_ACCEPTED:
                print(f"evicted {ref} (HTTP {status})")
            else:
                self._fail(ref, f"DELETE returned HTTP {status}")
        else:
            status = self._request("GET", path, PULL_TIMEOUT)
            if status == 200:
                print(f"pulled {ref} (HTTP {status})")
            else:
                self._fail(ref, f"GET returned HTTP {status}")

    def _fail(self, ref, reason):
        self.failures.append((ref, reason))
        print(f"ERROR: {ref}: {reason}", file=sys.stderr)

    @staticmethod
    def _manifest_path(ref):
        # The digest form is checked first: a digest's own colon must not
        # be mistaken for the repo:tag separator.
        if "@" in ref:
            name, _, digest = ref.partition("@")
            return f"/v2/{name}/manifests/{digest}"
        name, sep, tag = ref.rpartition(":")
        if not sep:
            name, tag = ref, "latest"
        return f"/v2/{name}/manifests/{tag}"

    def _request(self, method, path, timeout):
        url = f"{self.registry_url}{path}"
        req = Request(url, method=method,
                      headers={"Accept": MANIFEST_ACCEPT})
        try:
            with urlopen(req, timeout=timeout) as resp:
                resp.read()
                return resp.status
        except HTTPError as e:
            return e.code
        except (URLError, OSError) as e:
            print(f"ERROR: {method} {url} failed: {e}", file=sys.stderr)
            return 0
