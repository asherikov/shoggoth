"""Module split out of shoggoth.py; see package __init__."""
import json
import re
import sys
from urllib.parse import urlparse

from ..git import Git, WsHandler
from ..util import die, log, normalize_for_branch
from .base import CommandBase
from ..state import params

class TaskCommand(CommandBase):
    def __init__(self, task_id, shoggoth, gitea, redmine, agent):
        super().__init__(gitea, redmine, agent, shoggoth=shoggoth)
        self.task_id = task_id

    @staticmethod
    def _sanitize_subject(subject):
        return subject.replace("\n", " ").replace("\r", "")[:200]

    def create_pull_request(self, repo, branch, task_id, title, base="main"):
        self.gitea.ensure_pull_requests_enabled(repo)
        slave_user = params.shoggoth_slave_user
        review_user = params.shoggoth_review_user
        result = self.gitea.post(f"repos/{repo}/pulls", {
            "base": base,
            "head": branch,
            "title": title,
            "body": f"Task#{task_id}: {title}",
            "assignees": [slave_user],
            "reviewers": [review_user],
        })
        if result is None:
            print(f"WARNING: failed to create pull request for {repo} "
                  f"branch {branch} (base {base})", file=sys.stderr)
            return None
        return result.get("html_url")

    def commit_push_and_create_mr_standalone(self, branch, task_id, task_subject):
        repo_dir = self.shoggoth.repo_dir
        g = Git(repo_dir)
        status = g.status_porcelain()
        if not status.stdout.strip():
            print("No local changes, skipping commit and push")
            return False, []

        checkout = g.checkout_new_b(branch, check=False)
        if checkout.returncode != 0:
            die(f"git checkout -B {branch} failed: {checkout.stderr}")
        add = g.add_all(check=False)
        if add.returncode != 0:
            die(f"failed to stage changes: {add.stderr}")

        staged = g.diff_cached_name_only(check=False)
        if staged.returncode != 0:
            die(f"git diff --cached failed: {staged.stderr}")
        if not staged.stdout.strip():
            print("No staged changes, skipping commit and push")
            return False, []

        sanitized_subject = self._sanitize_subject(task_subject)
        commit = g.commit(f"Task#{task_id}: {sanitized_subject}", check=False)
        if commit.returncode != 0:
            die(f"git commit failed: {commit.stderr}")

        push = g.push("origin", branch, set_upstream=True, check=False)
        if push.returncode != 0:
            die(f"failed to push branch '{branch}': {push.stderr}")

        repo_full = self.shoggoth.project_repo
        base = self.gitea.get_default_branch(repo_full)
        pr_url = self.create_pull_request(
            repo_full, branch, task_id, sanitized_subject, base,
        )
        return True, ([pr_url] if pr_url else [])

    def commit_push_and_create_mr_ccws(self, branch, task_id, task_subject):
        repo_dir = self.shoggoth.repo_dir
        ws_handler = WsHandler(repo_dir)
        log("commit_push: pre-commit repository status:")
        status = ws_handler.status()
        if not status.stdout.strip():
            print("No local changes, skipping commit and push")
            return False, []

        modified_repos = ws_handler.parse_modified_repos(status.stdout)
        if not modified_repos:
            print("No local changes, skipping commit and push")
            return False, []

        sanitized_subject = self._sanitize_subject(task_subject)
        commit_msg = f"Task#{task_id}: {sanitized_subject}"

        ws_handler.branch_new(branch)

        self._stage_and_commit_ccws(repo_dir, modified_repos, commit_msg)
        push = ws_handler.push(modified_repos, check=False)
        if push.returncode != 0:
            die(f"failed to push branches: {push.stderr}")

        log("commit_push: post-push repository status:")
        status = ws_handler.status()
        repo_urls = {}
        for line in status.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            repo_urls[parts[0]] = parts[-1]

        pr_urls = []
        for repo_name in modified_repos:
            repo_url = repo_urls.get(repo_name)
            if not repo_url:
                continue
            parsed = urlparse(repo_url)
            path = parsed.path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            if "/" not in path:
                continue
            repo_full = path
            base = self.gitea.get_default_branch(repo_full)
            pr_url = self.create_pull_request(
                repo_full, branch, task_id, sanitized_subject, base,
            )
            if pr_url:
                pr_urls.append(pr_url)

        return True, pr_urls

    def commit_push_and_create_mr(self, branch, task_id, task_subject):
        if self.shoggoth.type == "ccws":
            return self.commit_push_and_create_mr_ccws(
                branch, task_id, task_subject)
        return self.commit_push_and_create_mr_standalone(
            branch, task_id, task_subject)

    def execute(self):
        if not re.match(r"^\d+$", self.task_id):
            die(f"invalid task ID: {self.task_id}")

        task = self.redmine.get_issue(self.task_id)

        task_subject = self.shoggoth.task_subject
        normalized_subject = normalize_for_branch(task_subject)
        if not normalized_subject:
            normalized_subject = f"task-{self.task_id}"
        shoggoth_branch = f"{self.shoggoth.project}/{normalized_subject}"
        log(f"task: branch={shoggoth_branch} subject={task_subject}")
        print(f"=== TASK IMPLEMENTATION: task #{self.task_id} "
              f"project={self.shoggoth.project} subject={task_subject} ===",
              flush=True)

        log(f"task: switching #{self.task_id} status to 'Feedback'")
        self.redmine.update_issue(
            self.task_id,
            "--status", "Feedback",
            "--note",
            "Task execution triggered; status changed from 'In Progress' to 'Feedback'.",
        )

        self.agent.start_session("task")

        task_data = {
            "subject": task.get("subject", ""),
            "description": task.get("description", ""),
            "custom_fields": task.get("custom_fields", []),
        }

        prompt = (
            f"Execute the following task: {task_subject}\n\n"
            f"{json.dumps(task_data, indent=2)}\n\n"
            f"Use the codebase-memory skill to explore the codebase and "
            f"understand the relevant code before making changes.\n\n"
            f"Leave all changes uncommitted in the working tree.\n\n"
            f"Update basic memory with any new information learned about "
            f"the project {self.shoggoth.project} and task \"{task_subject}\"."
        )

        rc = self.agent.prompt_with_retry(prompt)
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        self.agent.stop_session()

        log("task: committing and creating MR")
        pushed, pr_urls = self.commit_push_and_create_mr(
            shoggoth_branch, self.task_id, task_subject,
        )

        if pr_urls:
            note = "Merge requests created: " + ", ".join(pr_urls)
            redmine_update_args = ["--status", "Resolved", "--note", note]
            for url in pr_urls:
                print(f"Pull request created: {url}")
        elif pushed:
            redmine_update_args = ["--note",
                                   f"Changes pushed to branch {shoggoth_branch} but MR creation failed"]
        else:
            redmine_update_args = ["--status", "Feedback",
                                   "--note", "Reverted to Feedback: agent produced no source code changes"]

        self.redmine.update_issue(self.task_id, *redmine_update_args)
