"""Project resolution, shared by the worker checkout and the sandbox.

Which repository a job works on, where it is cloned from, and whether it
is a plain repository or a ccws workspace are the same questions for the
PR-comment / Redmine-task worker and for the sandbox pod, so they are
answered in one place.
"""
import json

from .util import die, log, normalize_for_branch
from .workspace import REPO_TYPE_PLAIN, REPO_TYPE_WORKSPACE


class ProjectResolver:
    """Resolve a project's repository and clone URL.

    The project is the Redmine project the work belongs to. Its
    repository is the project's homepage repository, or -- for a project
    without one -- the Gitea repository that matches the project name.
    """

    def __init__(self, gitea, redmine, domain):
        self.gitea = gitea
        self.redmine = redmine
        self.domain = domain

    def repo_for_branch(self, branch, repo_full):
        """The project repository behind a PR branch.

        The branch names the project (``<project>/<topic>``); a branch
        that names no known project falls back to the repository's own
        name, which is what the worker does.
        """
        project = self.redmine.identify_project_from_branch(branch,
                                                            repo_full)
        return self.repo_for_project(project)

    def repo_for_project(self, project):
        """``(project_repo, clone_url)`` for a Redmine project name."""
        normalized = normalize_for_branch(project)
        log(f"project: normalized={normalized}")
        proj = self.redmine.match_project(normalized)
        if not proj:
            return None, None
        project_id = proj.get("id")
        log(f"project: redmine project_id={project_id}")

        project_repo = self.redmine.get_project_repo(project_id, self.domain)
        if project_repo:
            clone_url = f"ssh://git@git.{self.domain}/{project_repo}.git"
            log(f"project: project_repo={project_repo} "
                f"clone_url={clone_url}")
            return project_repo, clone_url

        log("project: no homepage repo, searching gitea")
        clone_url, project_repo = self.gitea.find_repo(project)
        log(f"project: found repo={project_repo} clone_url={clone_url}")
        return project_repo, clone_url

    def repo_type(self, project_repo):
        """``standalone`` or ``ccws`` for a project repository.

        A repository that declares ``shoggoth.json`` says which it is; a
        repository that carries a repository list (``.repos``) is a ccws
        workspace.
        """
        return repo_type(self.gitea, project_repo)


def repo_type(gitea, project_repo):
    """``standalone`` or ``ccws`` for a project repository.

    A repository that declares ``shoggoth.json`` says which it is; a
    repository that carries a repository list (``.repos``) is a ccws
    workspace.
    """
    manifest = gitea.get_file(project_repo, "shoggoth.json")
    if manifest is not None:
        try:
            data = json.loads(manifest)
        except json.JSONDecodeError:
            die(f"invalid JSON in shoggoth.json for {project_repo}")
        config_type = data.get("type", "")
        if config_type in (REPO_TYPE_PLAIN, REPO_TYPE_WORKSPACE):
            return config_type
        if config_type != "":
            die(f"unsupported checkout type '{config_type}' in "
                f"shoggoth.json for {project_repo}")

    if gitea.get_file(project_repo, ".repos") is not None:
        return REPO_TYPE_WORKSPACE
    return REPO_TYPE_PLAIN
