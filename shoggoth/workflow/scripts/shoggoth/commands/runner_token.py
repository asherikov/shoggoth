"""Module split out of shoggoth.py; see package __init__."""
import sys

from ..k8s import K8s
from ..util import die
from .base import GiteaCommand
from ..state import params

class RunnerToken(GiteaCommand):
    K8S_SECRET_NAME = "gitea-runner-token"

    def __init__(self, gitea):
        super().__init__(gitea)
        self.org = params.shoggoth_github_org
        self.namespace = params.shoggoth_namespace

    def _fetch_registration_token(self):
        if not self.org:
            print("ERROR: SHOGGOTH_GITHUB_ORG is required", file=sys.stderr)
            return None
        return self.gitea.post(f"orgs/{self.org}/actions/runners/registration-token", {})

    def execute(self):
        if not self.namespace:
            print("ERROR: SHOGGOTH_NAMESPACE is required for K8s Secret write", file=sys.stderr)
            self.errors += 1
        else:
            print(f"Fetching runner registration token for org '{self.org}'")
            result = self._fetch_registration_token()
            if result is None or not result.get("token"):
                print(f"ERROR: Failed to fetch runner registration token", file=sys.stderr)
                self.errors += 1
            else:
                token_value = result["token"]
                print(f"Storing token in K8s Secret '{self.K8S_SECRET_NAME}' in namespace '{self.namespace}'")
                if not K8s(self.namespace).upsert_secret(self.K8S_SECRET_NAME, {"token": token_value}):
                    print(f"ERROR: Failed to write K8s Secret '{self.K8S_SECRET_NAME}'", file=sys.stderr)
                    self.errors += 1
        if self.errors:
            die(f"runner-token completed with {self.errors} error(s)")
        print(f"runner-token: token fetched and stored successfully")
