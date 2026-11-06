"""Module split out of shoggoth.py; see package __init__."""
import os

from ..git import Git, WsHandler
from ..util import die

class CommandBase:
    def __init__(self, gitea, redmine, agent, shoggoth=None):
        self.gitea = gitea
        self.redmine = redmine
        self.agent = agent
        self.shoggoth = shoggoth

    @staticmethod
    def _stage_and_commit_ccws(repo_dir, modified_repos, commit_msg):
        """Stage each sub-repo's working tree and commit via the wshandler wrapper.

        Identical step sequence between `_commit_only_ccws` (review-comment
        fix) and `commit_push_and_create_mr_ccws` (task implementation):
        for every modified sub-repo `git add -A`, then one repo-wide
        `wshandler commit` that walks all sub-repos and creates a commit per
        repo that has staged changes. Failure at any step is fatal — the
        workflow script's caller is a webhook (Argo WorkflowEventBinding),
        so partial commits would leave the PR in an unrecoverable state.
        """
        for repo_name in modified_repos:
            repo_path = os.path.join(repo_dir, repo_name)
            add = Git(repo_path).add_all(check=False)
            if add.returncode != 0:
                die(f"failed to stage changes in {repo_name}: {add.stderr}")
        commit = WsHandler(repo_dir).commit(commit_msg, check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")

    def _commit_only_standalone(self, repo_dir, commit_msg):
        g = Git(repo_dir)
        status = g.status_porcelain(check=False)
        if status.returncode != 0:
            die(f"git status failed in {repo_dir}: {status.stderr}")
        if not status.stdout.strip():
            self._pending_modified_repos = []
            return False

        add = g.add_all(check=False)
        if add.returncode != 0:
            die(f"failed to stage changes in {repo_dir}: {add.stderr}")
        staged = g.diff_cached_name_only(check=False)
        if staged.returncode != 0:
            die(f"git diff --cached failed in {repo_dir}: {staged.stderr}")
        if not staged.stdout.strip():
            self._pending_modified_repos = []
            return False

        commit = g.commit(commit_msg, check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")
        self._pending_modified_repos = []
        return True

    def _commit_only_ccws(self, repo_dir, commit_msg):
        ws_handler = WsHandler(repo_dir)
        status = ws_handler.status(quiet=True)
        if not status.stdout.strip():
            self._pending_modified_repos = []
            return False

        modified_repos = ws_handler.parse_modified_repos(status.stdout)
        if not modified_repos:
            self._pending_modified_repos = []
            return False

        self._stage_and_commit_ccws(repo_dir, modified_repos, commit_msg)
        self._pending_modified_repos = modified_repos
        return True

    def _commit_only(self, commit_msg=None):
        repo_dir = self.shoggoth.repo_dir
        if commit_msg is None:
            commit_msg = f"Address review comments on PR#{self.gitea.get_pr_number()}"

        if self.shoggoth.type == "ccws":
            return self._commit_only_ccws(repo_dir, commit_msg)
        return self._commit_only_standalone(repo_dir, commit_msg)

    def _push_pending_commits(self):
        repo_dir = self.shoggoth.repo_dir
        pr_branch = self.shoggoth.working_branch
        if self.shoggoth.type == "ccws":
            modified_repos = getattr(self, "_pending_modified_repos", [])
            push = WsHandler(repo_dir).push(modified_repos, check=False)
            if push.returncode != 0:
                die(f"failed to push branches: {push.stderr}")
        else:
            push = Git(repo_dir).push("origin", pr_branch, check=False)
            if push.returncode != 0:
                die(f"failed to push branch {pr_branch}: {push.stderr}")
class GiteaCommand:
    """Base for maintenance commands that operate against a Gitea instance.

    Stores the shared client reference and the per-command ``errors`` counter
    used by ``die(... completed with N error(s))`` at the end of each
    command's ``execute()``. Subclasses call ``super().__init__(gitea)``
    before their own env-var resolution.
    """

    def __init__(self, gitea):
        self.gitea = gitea
        self.errors = 0

    def _resolve_orgs(self, projects):
        """Resolve the list of Gitea org names to act on.

        Uses the explicit list if not ``None`` (including the empty
        list — an explicit ``[]`` means "operator chose nothing"),
        otherwise enumerates every org the Gitea admin token can see.
        Returns ``None`` when the fallback enumeration finds nothing
        so the caller can exit early with a "No Gitea projects found"
        diagnostic. The explicit-empty-list case is the
        ``list(projects)`` short-circuit returning ``[]``, preserving
        the difference between "default fallback found nothing" and
        "operator explicitly passed no projects".
        """
        if projects is not None:
            return list(projects)
        orgs = self.gitea.list_orgs()
        if not orgs:
            return None
        return [o.get("username") for o in orgs if o.get("username")]
