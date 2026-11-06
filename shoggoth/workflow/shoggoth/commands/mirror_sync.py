"""Module split out of shoggoth.py; see package __init__."""
import os
import re
import shutil
import tempfile
import time

from ..git import Git
from ..util import die

class GithubMirrorSync:
    def __init__(self, gitea, github):
        self.gitea = gitea
        self.github = github
        self.gitea_host = f"git.{self.gitea.domain}"
        self.tmpdir = None
        self.errors = 0
        self.failures = []
        self.conflicts = []
        self.repos_total = 0
        self.repos_succeeded = 0
        self.repos_skipped = 0
        self.repos_failed = 0

    def execute(self, github_orgs):
        for org in github_orgs:
            self._sync_org(org)
        self._print_summary()
        if self.failures:
            die(self._failure_summary())
        elif self.conflicts:
            die(self._conflict_summary())

    def _sync_org(self, github_org):
        print(f"=== Syncing GitHub '{github_org}' to Gitea org '{github_org}' ===")

        account = self.github.get(f"users/{github_org}")
        if account is None:
            msg = "GitHub account lookup failed (not found or rate limited)"
            print(f"ERROR: {msg}")
            self._record_failure(github_org, msg)
            return
        account_type = "orgs" if account.get("type") == "Organization" else "users"

        print(f"GitHub account type: {account_type}")

        repos = self.github.list_repos(github_org, account_type)
        if not repos:
            print(f"WARNING: No repos found for GitHub {account_type} '{github_org}' "
                  f"(or the repo listing failed)")
            return

        self.tmpdir = tempfile.mkdtemp()
        try:
            for idx, repo in enumerate(repos):
                if idx:
                    time.sleep(self.github.api_delay)
                self._sync_repo(github_org, repo)
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

        print(f"=== Sync complete for '{github_org}' ===")

    _NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

    def _sync_repo(self, github_org, repo_info):
        repo_name = repo_info.get("name", "")
        repo_desc = repo_info.get("description") or ""
        gitea_repo = f"{github_org}/{repo_name}"

        self.repos_total += 1

        if not self._NAME_RE.match(github_org) or not self._NAME_RE.match(repo_name):
            msg = "invalid org/repo name"
            print(f"ERROR: {msg}: {gitea_repo}")
            self._record_failure(gitea_repo, msg)
            return

        print(f"--- Processing {github_org}/{repo_name} ---")

        repo_data = self.gitea.get_repo(gitea_repo)

        if repo_data is None:
            if self._migrate_repo(github_org, repo_name, repo_desc):
                self.repos_succeeded += 1
            else:
                self.repos_failed += 1
        else:
            status = self._sync_existing_repo(github_org, repo_name, repo_data)
            if status == "ok":
                self.repos_succeeded += 1
            elif status == "skipped":
                self.repos_skipped += 1
            else:
                self.repos_failed += 1

    def _migrate_repo(self, github_org, repo_name, repo_desc):
        print(f"Repository {github_org}/{repo_name} does not exist in Gitea, creating...")

        if self.gitea.status(f"orgs/{github_org}") == 404:
            print(f"Creating Gitea organization: {github_org}")
            self.gitea.create_org(github_org, github_org)

        result = self.gitea.migrate_repo(
            f"https://github.com/{github_org}/{repo_name}",
            repo_name, github_org, repo_desc)
        if result is not None:
            print(f"Migrated {github_org}/{repo_name}")
            return True
        self._record_failure(f"{github_org}/{repo_name}", "migration to Gitea failed")
        return False

    def _github_refs(self, github_org, repo_name):
        """Branch and tag heads from GitHub via `git ls-remote`.

        One unauthenticated git request replaces the per-repo branches/tags
        REST calls, which exhausted the GitHub API quota for orgs with more
        than ~30 repos (2 unauthenticated calls per repo, 60/hour limit).
        """
        ls_remote = Git().ls_remote(
            f"https://github.com/{github_org}/{repo_name}.git", check=False)
        if ls_remote.returncode != 0:
            return None
        branches, tags = {}, {}
        for line in ls_remote.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                branches[ref[len("refs/heads/"):]] = sha
            elif ref.startswith("refs/tags/"):
                ref = ref[len("refs/tags/"):]
                if ref.endswith("^{}"):
                    tags[ref[:-len("^{}")]] = sha
                else:
                    tags.setdefault(ref, sha)
        return branches, tags

    def _sync_existing_repo(self, github_org, repo_name, repo_data):
        gitea_repo = f"{github_org}/{repo_name}"

        github_refs = self._github_refs(github_org, repo_name)
        if github_refs is None:
            self._record_failure(gitea_repo, "failed to list refs from GitHub (git ls-remote)")
            return "failed"
        github_by_name, github_tag_sha = github_refs

        gitea_branches = self.gitea.list_branches(gitea_repo) or []

        if not github_by_name:
            self._record_failure(gitea_repo, "GitHub repository has no branches")
            return "failed"
        if not gitea_branches:
            self._record_failure(gitea_repo, "Gitea list_branches returned no branches")
            return "failed"

        gitea_tags = self.gitea.list_tags(gitea_repo) or []

        gitea_by_name = {b["name"]: b["commit"]["id"] for b in gitea_branches}
        gitea_tag_sha = {t["name"]: t["commit"]["sha"] for t in gitea_tags}

        out_of_sync = [
            name for name in github_by_name
            if github_by_name[name] != gitea_by_name.get(name)
        ]
        out_of_sync += [
            name for name in github_tag_sha
            if github_tag_sha[name] != gitea_tag_sha.get(name)
        ]

        if not out_of_sync:
            print(f"Repository {github_org}/{repo_name} is up to date "
                  f"({len(github_by_name)} branch(es), {len(github_tag_sha)} tag(s) match), "
                  f"skipping")
            return "skipped"

        print(f"Repository {github_org}/{repo_name}: {len(out_of_sync)} branch(es) differ "
              f"from Gitea, syncing...")

        clone_url = f"http://{self.gitea_host}/{github_org}/{repo_name}.git"
        clone_dir = os.path.join(self.tmpdir, repo_name)

        cred_file = os.path.join(self.tmpdir, f".git-credentials-{repo_name}")
        with open(cred_file, "w") as f:
            f.write(f"http://token:{self.gitea.token}@{self.gitea_host}\n")
        os.chmod(cred_file, 0o600)

        cred_helper = f"store --file={cred_file}"

        # --bare is not surfaced as a named ``clone`` flag because
        # no other call site wants a bare clone; reuse the helper
        # shape via ``raw``.
        clone = Git().raw("-c", f"credential.helper={cred_helper}",
                          "clone", "--quiet", "--bare",
                          clone_url, clone_dir, check=False)
        if clone.returncode != 0:
            self._record_failure(gitea_repo, "failed to clone from Gitea")
            os.unlink(cred_file)
            return "failed"

        fetch = Git(clone_dir).fetch_url_refspecs(
            f"https://github.com/{github_org}/{repo_name}.git",
            "+refs/heads/*:refs/remotes/github/*",
            tags=True, quiet=True, check=False)
        if fetch.returncode != 0:
            self._record_failure(gitea_repo, "failed to fetch from GitHub")
            os.unlink(cred_file)
            return "failed"

        changed_branches = []
        repo_had_conflicts = False
        clone_git = Git(clone_dir)
        refs = clone_git.raw("for-each-ref", "--format=%(refname)",
                              "refs/remotes/github/", check=False)
        for ref in refs.stdout.strip().splitlines() if refs.stdout else []:
            branch_name = ref.replace("refs/remotes/github/", "")
            if branch_name == "HEAD":
                continue

            remote_sha = clone_git.rev_parse(ref, check=False).stdout.strip()

            local_check = clone_git.rev_parse_verify_quiet(
                f"refs/heads/{branch_name}", check=False)
            if local_check.returncode == 0:
                local_sha = local_check.stdout.strip()
                if local_sha == remote_sha:
                    print(f"  Branch '{branch_name}' already up to date")
                    continue
                ancestor = clone_git.merge_base_is_ancestor(
                    f"refs/heads/{branch_name}", ref, check=False)
                if ancestor.returncode == 0:
                    print(f"  Branch '{branch_name}': fast-forwarding")
                    clone_git.branch(branch_name, ref, force=True,
                                      check=False)
                    changed_branches.append(branch_name)
                else:
                    print(f"  WARNING: Branch '{branch_name}' has diverged from GitHub, skipping (manual reconciliation needed)")
                    repo_had_conflicts = True
            else:
                clone_git.branch(branch_name, ref, check=False)
                changed_branches.append(branch_name)

        status = "ok"
        if changed_branches:
            print(f"  Pushing {len(changed_branches)} changed branch(es) to Gitea")
            push = Git(clone_dir).push("origin", *changed_branches,
                                        credential_helper=cred_helper,
                                        check=False)
            if push.returncode != 0:
                self._record_failure(gitea_repo, "failed to push branches to Gitea")
                status = "failed"
        else:
            print(f"  No changed branches to push")

        if status != "failed":
            tags = Git(clone_dir).raw("tag", "--list", check=False)
            if tags.stdout.strip():
                tag_push = Git().raw("-c",
                                      f"credential.helper={cred_helper}",
                                      "-C", clone_dir, "push", "--quiet",
                                      "--tags", "origin", check=False)
                if tag_push.returncode != 0:
                    self._record_failure(gitea_repo, "failed to push tags to Gitea")
                    status = "failed"
            else:
                print(f"  No tags to push")

        os.unlink(cred_file)
        if repo_had_conflicts:
            self.conflicts.append(gitea_repo)
        print(f"Synced {github_org}/{repo_name}")
        return status

    def _record_failure(self, repo, reason):
        self.errors += 1
        self.failures.append((repo, reason))
        print(f"WARNING: {repo}: {reason}")

    def _print_summary(self):
        print(f"=== Mirror sync summary ===")
        print(f"  Repos processed: {self.repos_total}")
        print(f"  Succeeded:       {self.repos_succeeded}")
        if self.repos_skipped:
            print(f"  Skipped (up to date): {self.repos_skipped}")
        if self.conflicts:
            print(f"  With diverged branches: {len(self.conflicts)} (skipped, manual reconciliation needed)")
        if self.repos_failed:
            print(f"  Failed:          {self.repos_failed}")
        if self.conflicts:
            print(f"\nRepos with diverged branches (no automatic fix possible):")
            for repo in self.conflicts:
                print(f"  - {repo}")

    def _failure_summary(self):
        lines = [f"github-mirror-sync completed with {self.errors} error(s):"]
        for repo, reason in self.failures:
            lines.append(f"  - {repo}: {reason}")
        if self.conflicts:
            lines.append("")
            lines.append(f"Additionally, {len(self.conflicts)} repo(s) have diverged branches (manual reconciliation needed):")
            for repo in self.conflicts:
                lines.append(f"  - {repo}")
        return "\n".join(lines)

    def _conflict_summary(self):
        lines = [f"github-mirror-sync completed with {len(self.conflicts)} repo(s) containing diverged branches (manual reconciliation needed):"]
        for repo in self.conflicts:
            lines.append(f"  - {repo}")
        return "\n".join(lines)
