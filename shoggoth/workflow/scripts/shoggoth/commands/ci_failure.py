"""Module split out of shoggoth.py; see package __init__."""


from ..util import die, log
from .base import CommandBase

class CiFailureCommand(CommandBase):
    def __init__(self, shoggoth, gitea, redmine, agent):
        super().__init__(gitea, redmine, agent, shoggoth=shoggoth)

    def execute(self):
        conclusion = self.gitea.get_ci_conclusion()
        log(f"ci-failure: conclusion={conclusion}")
        if conclusion != "failure":
            print(f"Ignoring workflow_run conclusion: {conclusion}")
            return

        ci_repo = self.shoggoth.working_repo
        ci_sha = self.gitea.get_ci_sha()
        ci_branch = self.shoggoth.working_branch
        ci_run_url = self.gitea.get_ci_run_url()
        ci_workflow = self.gitea.get_ci_workflow_name()
        ci_run_id = self.gitea.get_ci_run_id()
        log(f"ci-failure: repo={ci_repo} sha={ci_sha} branch={ci_branch} workflow={ci_workflow} run_id={ci_run_id}")
        print(f"=== CI FAILURE FIX: repo={ci_repo} workflow={ci_workflow} "
              f"branch={ci_branch} sha={ci_sha} ===", flush=True)

        ci_logs = self.gitea.get_ci_logs(ci_repo, ci_run_id)

        self.agent.start_session("ci-failure")

        prompt = (
            f"CI workflow '{ci_workflow}' failed on repository {ci_repo} "
            f"at commit {ci_sha} (branch {ci_branch}).\n"
            f"Run URL: {ci_run_url}\n\n"
            f"CI logs:\n{ci_logs}\n\n"
            f"Use the codebase-memory skill to understand the code related "
            f"to the failure. Fix the code. Leave all changes uncommitted "
            f"in the working tree; the workflow script handles commit and push."
        )

        rc = self.agent.prompt_with_retry(prompt)
        if rc != 0:
            die(f"qwen agent exited with code {rc}")

        self.agent.stop_session()

        commit_subject = f"Fix CI failure in {ci_workflow}"
        commit_msg = (
            f"{commit_subject}\n\n"
            f"Resolves CI failure on {ci_repo}@{ci_sha[:12]} "
            f"(branch {ci_branch}).\n"
            f"Run URL: {ci_run_url}"
        )
        committed = self._commit_only(commit_msg)
        if committed:
            log(f"ci-failure: committed fix on branch {ci_branch}")
            self._push_pending_commits()
        else:
            log(f"ci-failure: no source changes to commit")
