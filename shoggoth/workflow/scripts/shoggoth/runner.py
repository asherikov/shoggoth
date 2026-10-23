"""Module split out of shoggoth.py; see package __init__."""
import json
import os
import re
import shutil
import sys
import uuid

from .git import Git, WsHandler
from .util import die, log, normalize_for_branch, run
from .state import params

class Shoggoth:
    def __init__(self, redmine, gitea, args):
        self.project = None
        self.domain = params.shoggoth_domain
        self.github_org = params.shoggoth_github_org
        self.type = None
        self.working_repo = None
        self.working_branch = None
        self.project_repo = None
        self.clone_url = None
        self.repo_dir = None
        self.task_subject = None
        self.gitea = gitea

        self.identify_project(redmine, gitea, args)
        self.identify_project_repo(redmine, gitea)
        self.load_manifest(gitea)

    def identify_project(self, redmine, gitea, args):
        log(f"identify_project: command={args.command}")
        if args.command == "task":
            task = redmine.get_issue(args.task_id)
            self._check_task_actionable(task, redmine)
            self.task_subject = task.get("subject", "")
            task_project = task.get("project", {}).get("name", "")
            if not task_project:
                die(f"task #{args.task_id} has no project assigned")
            self.project = normalize_for_branch(task_project)
            log(f"identify_project: project={self.project} subject={self.task_subject}")
            return
        self.working_repo = gitea.get_repo_full_name()
        if args.command == "ci-failure":
            self.working_branch = gitea.get_ci_branch()
        else:
            self.working_branch = gitea.get_pr_branch()
        self.task_subject = ""
        if "/" in self.working_branch:
            self.task_subject = self.working_branch.split("/", 1)[1]
        self.project = redmine.identify_project_from_branch(self.working_branch, self.working_repo)
        log(f"identify_project: repo={self.working_repo} branch={self.working_branch} project={self.project}")

    def _check_task_actionable(self, task, redmine):
        task_id = task.get("id")
        status_name = (task.get("status") or {}).get("name")
        slave_user_login = params.shoggoth_slave_user
        assigned_to = task.get("assigned_to") or {}
        assigned_to_id = assigned_to.get("id")
        assigned_to_login = assigned_to.get("login")
        if assigned_to_login is None and assigned_to_id is not None:
            assigned_to_login = redmine.get_user_login(assigned_to_id)
        if status_name != "In Progress":
            log(f"task: skipping #{task_id} status={status_name!r} (expected 'In Progress')")
            print(f"Task #{task_id} status is {status_name!r}, not 'In Progress' — skipping",
                  flush=True)
            sys.exit(0)
        if assigned_to_login != slave_user_login:
            log(f"task: skipping #{task_id} assignee_id={assigned_to_id!r} "
                f"assignee_login={assigned_to_login!r} (expected {slave_user_login!r})")
            print(f"Task #{task_id} assigned to {assigned_to_login!r}, "
                f"not {slave_user_login!r} — skipping",
                  flush=True)
            sys.exit(0)

    def identify_project_repo(self, redmine, gitea):
        normalized = normalize_for_branch(self.project)
        log(f"identify_project_repo: normalized={normalized}")
        proj = redmine.match_project(normalized)
        if not proj:
            die(f"could not find redmine project for '{self.project}'")
        project_id = proj.get("id")
        log(f"identify_project_repo: redmine project_id={project_id}")

        project_repo = redmine.get_project_repo(project_id, self.domain)

        if project_repo:
            self.project_repo = project_repo
            self.clone_url = f"ssh://git@git.{self.domain}/{project_repo}.git"
            log(f"identify_project_repo: project_repo={project_repo} clone_url={self.clone_url}")
            return

        log("identify_project_repo: no homepage repo, searching gitea")
        self.clone_url, self.project_repo = gitea.find_repo(self.project)
        log(f"identify_project_repo: found repo={self.project_repo} clone_url={self.clone_url}")

    def load_manifest(self, gitea):
        shoggoth_json = gitea.get_file(self.project_repo, "shoggoth.json")
        if shoggoth_json is not None:
            try:
                data = json.loads(shoggoth_json)
            except json.JSONDecodeError:
                die(f"invalid JSON in shoggoth.json for {self.project_repo}")
            config_type = data.get("type", "")
            if config_type in ("standalone", "ccws"):
                self.type = config_type
            elif config_type != "":
                die(f"unsupported checkout type '{config_type}' in shoggoth.json for {self.project_repo}")

        if self.type is None:
            repos_file = gitea.get_file(self.project_repo, ".repos")
            if repos_file is not None:
                self.type = "ccws"
            else:
                self.type = "standalone"

        log(f"load_manifest: type={self.type}")

    def checkout(self):
        workspace_dir = params.workspace_src
        os.makedirs(workspace_dir, exist_ok=True)

        log(f"checkout: type={self.type} branch={self.working_branch} url={self.clone_url} dest={workspace_dir}")

        g = Git()
        if self.working_branch:
            result = g.clone(self.clone_url, workspace_dir,
                              branch=self.working_branch, check=False)
            if result.returncode != 0 and self.type == "ccws":
                log(f"checkout: branch {self.working_branch} not found, cloning default branch")
                result = g.clone(self.clone_url, workspace_dir, check=False)
        else:
            result = g.clone(self.clone_url, workspace_dir, check=False)

        if result.returncode != 0:
            die(f"git clone failed for {self.clone_url}: {result.stderr}")

        if self.type == "ccws":
            org_re = self.github_org.replace(".", r"\.")
            wsh_args = ["-s", f"s|https://github.com/{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-s", f"s|git@github.com:{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-s", f"s|ssh://git@github.com:{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-s", f"s|git+ssh://git@github.com/{org_re}|ssh://git@git.{self.domain}/{org_re}|g",
                        "-p", "shallow"]
            if self.working_branch:
                wsh_args += ["-P", self.working_branch]
            wsh_args.append("update")
            ws_handler = WsHandler(workspace_dir)
            ws_handler.run(wsh_args)

            log("checkout: post-update repository list:")
            ws_handler.status(quiet=True)

            apt_update = run(["sudo", "-S", "apt", "update"], check=False, input="ccws\n")
            if apt_update.stdout:
                print(apt_update.stdout, file=sys.stderr, flush=True)
            if apt_update.stderr:
                print(apt_update.stderr, file=sys.stderr, flush=True)
            if apt_update.returncode != 0:
                die(f"apt update failed with exit code "
                    f"{apt_update.returncode}: "
                    f"{(apt_update.stderr or '').strip()[-500:]}")

            dep_install = run(["make", "dep_install"], cwd="/ccws",
                              input="ccws\n", check=False)
            if dep_install.stdout:
                print(dep_install.stdout, file=sys.stderr, flush=True)
            if dep_install.stderr:
                print(dep_install.stderr, file=sys.stderr, flush=True)
            if dep_install.returncode != 0:
                die(f"make dep_install failed with exit code "
                    f"{dep_install.returncode}: "
                    f"{(dep_install.stderr or '').strip()[-500:]}")

        self.repo_dir = workspace_dir

    def _unshallow(self, repo_dir):
        """Fetch full history if the local clone is shallow.
        Idempotent: no-op when the repo already has full history.
        """
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            log(f"_unshallow: {repo_dir} is not a git repo, skipping")
            return
        if not os.path.exists(os.path.join(repo_dir, ".git", "shallow")):
            log(f"_unshallow: {repo_dir} already full history")
            return
        result = Git(repo_dir).fetch(remote="origin", unshallow=True,
                                      tags=True, check=False)
        if result.returncode != 0:
            log(f"_unshallow: warning: {result.stderr.strip()[:200]}")

    def _ensure_remote_ref(self, repo_dir, ref_name, pr_number=None):
        """Ensure refs/remotes/origin/{ref_name} exists locally.
        For PR head refs, falls back to fetch origin pull/{n}/head:pr-head.
        Returns True if the ref is available after the call.
        """
        if not ref_name:
            return False
        safe = re.sub(r"[^A-Za-z0-9_./-]", "-", ref_name)
        if not safe or safe.startswith("-") or ".." in safe:
            log(f"_ensure_remote_ref: refusing unsafe ref {ref_name!r}")
            return False
        g = Git(repo_dir)
        result = g.fetch(remote="origin", ref=ref_name,
                         local_ref=f"refs/remotes/origin/{safe}",
                         check=False)
        if result.returncode == 0:
            return True
        if pr_number is not None:
            log(f"_ensure_remote_ref: trying pull/{pr_number}/head as fallback")
            result = g.fetch(remote="origin",
                             ref=f"pull/{pr_number}/head",
                             local_ref=f"refs/remotes/origin/pr-{pr_number}",
                             check=False)
            return result.returncode == 0
        return False

    def _write_pr_diff(self, repo_dir, base_ref, head_ref, diff_path):
        """Run git diff and write to diff_path. Returns True if non-empty.
        Tries several candidate specs in order:
            origin/{base}...{head}
            origin/{base}...HEAD
            HEAD~1...HEAD
        Empty result still creates an empty file at diff_path so the slash
        command has a target to inspect.
        """
        candidates = []
        if base_ref and head_ref:
            candidates.append(f"origin/{base_ref}...{head_ref}")
        if base_ref:
            candidates.append(f"origin/{base_ref}...HEAD")
        candidates.append("HEAD~1...HEAD")
        g = Git(repo_dir)
        for spec in candidates:
            result = g.diff(spec, "--binary", "--no-color", check=False)
            if result.returncode == 0 and result.stdout.strip():
                with open(diff_path, "w") as f:
                    f.write(result.stdout)
                log(f"_write_pr_diff: wrote {len(result.stdout)} bytes via {spec}")
                return True
        open(diff_path, "w").close()
        log("_write_pr_diff: diff is empty, wrote empty file")
        return False

    def _apply_pr_to_worktree(self, repo_dir, base_ref, head_ref,
                              review_repo_dir, pr_number):
        """Create a worktree at base_ref and apply the PR diff as uncommitted
        changes. Returns (worktree_path, diff_source) where diff_source is
        one of "git-diff", "gitea-api", or "" (empty).

        The worktree is at /tmp/shoggoth-review-<n>-<rand>; cleanup is the
        caller's responsibility (use _remove_review_worktree).

        For ccws sub-repos, worktrees are created from the sub-repo's own
        .git (not the ccws repo) so the resulting tree matches the sub-repo
        layout the agent will review.
        """
        wt_path = (f"/tmp/shoggoth-review-{pr_number}-"
                   f"{uuid.uuid4().hex[:8]}")

        worktree_source = repo_dir
        if (review_repo_dir and review_repo_dir != repo_dir
                and os.path.isdir(os.path.join(review_repo_dir, ".git"))):
            worktree_source = review_repo_dir
            log(f"_apply_pr_to_worktree: using sub-repo {review_repo_dir} "
                f"as worktree source")

        base_sha = None
        source_git = Git(worktree_source)
        for candidate in ([f"origin/{base_ref}", base_ref]
                          if base_ref else []):
            resolved = source_git.rev_parse_verify(candidate, check=False)
            if resolved.returncode == 0 and resolved.stdout.strip():
                base_sha = resolved.stdout.strip()
                break

        if base_sha is None:
            log(f"_apply_pr_to_worktree: cannot resolve base ref "
                f"{base_ref!r} from {worktree_source}")
            return None, ""

        add_wt = source_git.worktree_add(wt_path, base_sha, detach=True,
                                          check=False)
        if add_wt.returncode != 0:
            log(f"_apply_pr_to_worktree: worktree add failed: "
                f"{add_wt.stderr.strip()[:200]}")
            return None, ""

        diff_text = None
        diff_source = ""
        for spec in ([f"origin/{base_ref}...{head_ref}",
                      f"origin/{base_ref}...HEAD",
                      "HEAD~1...HEAD"] if base_ref and head_ref else
                     [f"origin/{base_ref}...HEAD", "HEAD~1...HEAD"]
                     if base_ref else ["HEAD~1...HEAD"]):
            res = source_git.diff(spec, "--binary", "--no-color",
                                   check=False)
            if res.returncode == 0 and res.stdout.strip():
                diff_text = res.stdout
                diff_source = f"git-diff:{spec}"
                log(f"_apply_pr_to_worktree: got diff ({len(diff_text)} "
                    f"bytes) via {spec} (from {worktree_source})")
                break

        if diff_text is None:
            log("_apply_pr_to_worktree: trying Gitea .diff fallback")
            gitea_diff = self.gitea.get_pr_diff_text(
                self.working_repo, pr_number)
            if gitea_diff:
                diff_text = gitea_diff
                diff_source = "gitea-api"

        if not diff_text or not diff_text.strip():
            log("_apply_pr_to_worktree: no diff available")
            self._remove_review_worktree(wt_path)
            return None, ""

        if review_repo_dir != repo_dir:
            log(f"_apply_pr_to_worktree: rewriting patch paths "
                f"(review_repo_dir={review_repo_dir})")
            diff_text = self._rewrite_patch_paths(
                diff_text, review_repo_dir)

        wt_git = Git(wt_path)
        apply = wt_git.apply_stdin(diff_text, whitespace_fix=True,
                                    recount=True, check=False)
        if apply.returncode != 0:
            log(f"_apply_pr_to_worktree: git apply failed "
                f"(will retry with -3): {apply.stderr.strip()[:300]}")
            apply3 = wt_git.apply_stdin(diff_text, three_way=True,
                                         whitespace_fix=True, check=False)
            if apply3.returncode != 0:
                log(f"_apply_pr_to_worktree: git apply --3way also "
                    f"failed: {apply3.stderr.strip()[:300]}")
                self._remove_review_worktree(wt_path)
                return None, ""

        status = wt_git.status_porcelain(check=False)
        log(f"_apply_pr_to_worktree: worktree {wt_path} ready; "
            f"diff_source={diff_source}; "
            f"changed_files={len([l for l in status.stdout.splitlines() if l.strip()])}")
        return wt_path, diff_source

    @staticmethod
    def _rewrite_patch_paths(patch_text, review_repo_dir=None):
        """Rewrite `diff --git a/<path> b/<path>` headers so the patch can be
        applied at a sub-repo path instead of the ccws repo root.
        Strips the longest common top-level directory prefix.
        ``review_repo_dir`` is the sub-repo path the patch is being
        applied at; currently unused beyond keeping the call-site
        signature stable, but reserved for future per-sub-repo
        rewrite rules.
        """
        a_paths = []
        for line in patch_text.splitlines():
            if line.startswith("--- a/"):
                a_paths.append(line[len("--- a/"):])
        if not a_paths:
            return patch_text
        prefixes = {p.split("/", 1)[0] for p in a_paths if "/" in p}
        if len(prefixes) != 1:
            return patch_text
        top = next(iter(prefixes))
        top_slash = f"{top}/"
        stripped = []
        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                rest = line[len("diff --git "):]
                if rest.startswith("a/") and " b/" in rest:
                    a_path, b_path_with_marker = rest[2:].split(" b/", 1)
                    b_path = b_path_with_marker
                    if a_path.startswith(top_slash):
                        a_path = a_path[len(top_slash):]
                    if b_path.startswith(top_slash):
                        b_path = b_path[len(top_slash):]
                    line = f"diff --git a/{a_path} b/{b_path}"
            elif line.startswith(f"--- a/{top_slash}"):
                line = f"--- a/{line[len('--- a/' + top_slash):]}"
            elif line.startswith(f"+++ b/{top_slash}"):
                line = f"+++ b/{line[len('+++ b/' + top_slash):]}"
            stripped.append(line)
        return "\n".join(stripped)

    @staticmethod
    def _remove_review_worktree(wt_path):
        """Remove a worktree and prune the worktree list."""
        if not wt_path or not os.path.isdir(wt_path):
            return
        repo_dir = None
        try:
            with open(os.path.join(wt_path, ".git"), "r") as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                gitdir = content[len("gitdir:"):].strip()
                worktrees_dir = os.path.dirname(gitdir)
                repo_git_dir = os.path.dirname(worktrees_dir)
                repo_dir = os.path.dirname(repo_git_dir)
        except (OSError, ValueError):
            pass
        if repo_dir is None:
            log(f"_remove_review_worktree: cannot infer repo_dir from "
                f"{wt_path}; falling back to force remove")
            shutil.rmtree(wt_path, ignore_errors=True)
            return
        g = Git(repo_dir)
        g.worktree_remove(wt_path, force=True, check=False)
        g.worktree_prune(check=False)
        if os.path.isdir(wt_path):
            shutil.rmtree(wt_path, ignore_errors=True)

    def _resolve_review_repo_dir(self, pr_repo, pr_number):
        """If ccws and the PR touches exactly one sub-repo with its own .git,
        return that sub-repo's absolute path. Otherwise return self.repo_dir.
        """
        if self.type != "ccws":
            return self.repo_dir
        files = self.gitea.get_pr_files(pr_repo, pr_number) or []
        prefixes = {f["path"].split("/", 1)[0]
                    for f in files if "/" in f.get("path", "")}
        if len(prefixes) == 1:
            subrepo = next(iter(prefixes))
            path = os.path.join(self.repo_dir, subrepo)
            if os.path.isdir(os.path.join(path, ".git")):
                log(f"_resolve_review_repo_dir: scoped to {subrepo}")
                return path
        if len(prefixes) > 1:
            log(f"_resolve_review_repo_dir: PR touches {len(prefixes)} "
                f"sub-repos, reviewing at the ccws level")
        return self.repo_dir
