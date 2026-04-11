---
name: shoggoth-workflow
description: Implement Redmine tasks and push results to Gitea repositories. Use when working on Redmine issues that require code implementation, feature development, or bug fixes. Covers the full workflow from task selection through Redmine, repository identification via Gitea CLI, implementation, branching, committing, and creating merge requests. Automatically resolves completed tasks and subtasks in Redmine.
---

# Shoggoth Workflow

## Overview

This skill provides a standardized workflow for implementing Redmine tasks and delivering results through Gitea repositories. It integrates the `redmine` and `tea` (Gitea CLI) command-line tools to manage the complete development lifecycle from task selection to merge request creation.

## When to Use This Skill

This skill should be used when:
- Implementing features or fixing bugs tracked in Redmine
- Working on tasks that require code changes in Gitea repositories
- Following a structured development workflow with proper task tracking
- Creating merge requests for completed work
- Addressing review comments on existing pull requests
- Resolving completed tasks and subtasks in Redmine

## Prerequisites

- `redmine` CLI tool configured and authenticated
- `tea` (Gitea CLI) tool configured and authenticated
- Access to relevant Redmine projects and Gitea repositories
- Workspace environment set up for development

## Workflow Steps

### 1. Detect In-Progress Tasks

Find tasks currently marked as "In Progress" in Redmine:

```bash
redmine issues list --status "In Progress" --limit 0 -o json
```

Review the results and identify tasks ready for implementation.

### 2. Select Task for Implementation

Get detailed information about a specific task including subtasks:

```bash
redmine issues get <issue_id> --journals --children -o json
```

If the task has children (subtasks), retrieve their details:

```bash
redmine issues get <child_id> -o json
```

### 3. Determine Corresponding Gitea Repositories

Search for repositories related to the task:

```bash
tea repos list --search <keyword> -o json
```

Or search by organization:

```bash
tea repos list --owner <owner> -o json
```

Identify the repository/repositories that need modifications based on the task description.

### 4. Clone Repositories

Clone the identified repositories to the workspace:

```bash
cd /ccws/workspace/src
git clone <repository_url>
```

For SSH URLs, ensure SSH keys are configured. For HTTP URLs, credentials may be required.

### 5. Implement the Feature

Work on the implementation:
- Analyze existing code structure
- Make necessary modifications
- Follow project conventions and coding standards
- Test changes if applicable

### 6. Create Feature Branch and Commit

Create a new feature branch with a descriptive name:

```bash
cd <repository_directory>
git checkout -b feature/<descriptive-branch-name>
```

Stage and commit changes:

```bash
git add <modified_files>
git commit -m "Implement feature: <brief description>

Detailed description of changes and rationale.
References Redmine issue #<issue_id>"
```

### 7. Push Branch and Open Merge Request

Push the feature branch to Gitea:

```bash
git push origin feature/<descriptive-branch-name>
```

Create a merge request using tea:

```bash
tea pr create \
  --repo <owner>/<repo> \
  --base master \
  --head feature/<descriptive-branch-name> \
  --title "Implement <feature description>" \
  --description "<detailed PR description with references to Redmine issue>"
```

**Important**: If merge request creation fails (e.g., due to insufficient permissions or the repository being a mirror), do NOT create a fork. Instead, inform the user about the failure and stop. Report the error message and the repository that could not be pushed to, so the user can decide how to proceed.

### 8. Update Redmine Task

Add a comment to the Redmine issue with the merge request URL:

```bash
redmine issues update <issue_id> --note "Merge request created: <MR_URL>"
```

### 9. Address Pull Request Comments

When review comments are posted on a merge request, address each one systematically.

#### List Review Comments

Get all review comments on a pull request:

```bash
tea pulls <pr_index> --repo <owner>/<repo> --comments -o json
```

Or use the Gitea API directly for detailed review comments:

```bash
curl -s "http://git.shoggoth.local/api/v1/repos/<owner>/<repo>/pulls/<pr_index>/reviews/<review_id>/comments" \
  -H "Authorization: token <token>" | jq .
```

#### Address Each Comment

For each review comment:
1. Read the comment and understand the requested change
2. Make the necessary code modifications
3. Commit the fix with a descriptive message referencing the comment
4. Post a reply comment indicating the fix has been implemented

Post a fix comment for each addressed review thread:

```bash
curl -s -X POST "http://git.shoggoth.local/api/v1/repos/<owner>/<repo>/issues/<pr_index>/comments" \
  -H "Authorization: token <token>" \
  -H "Content-Type: application/json" \
  -d '{"body": "Fixed: <brief description of the change made>"}'
```

#### Push Additional Commits

If changes are needed after the PR was opened, commit and push to the same branch:

```bash
git add <modified_files>
git commit -m "Address review comment: <description>

Fixes review comment #<comment_id> on PR #<pr_index>"
git push origin feature/<branch-name>
```

#### Resolve Conversations

If the Gitea instance supports it, resolve review comment threads:

```bash
# Via tea CLI (requires local repository)
cd <repository_directory>
tea pulls resolve <comment_id> -r <owner>/<repo>
```

Or via API:

```bash
curl -s -X POST "http://git.shoggoth.local/api/v1/repos/<owner>/<repo>/pulls/comments/<comment_id>/resolve" \
  -H "Authorization: token <token>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Note: Some Gitea instances may not support the resolve API (returns 405). In that case, post fix comments to indicate each thread has been addressed.

### 10. Resolve Completed Tasks

After the merge request is created (or merged), resolve the task and all completed subtasks:

```bash
# Resolve main task
redmine issues update <issue_id> --status "Resolved" --note "Implementation completed. MR: <MR_URL>"

# Resolve each completed subtask
redmine issues update <subtask_id> --status "Resolved" --note "Completed as part of parent issue #<issue_id>. MR: <MR_URL>"
```

## Complete Example

Implementing a feature with subtasks:

```bash
# Step 1: Find in-progress tasks
redmine issues list --status "In Progress" --limit 0 -o json

# Step 2: Get task details (issue #42 with subtasks)
redmine issues get 42 --journals --children -o json
redmine issues get 43 -o json  # subtask
redmine issues get 44 -o json  # subtask

# Step 3: Find related repository
tea repos list --search "myproject" -o json

# Step 4: Clone repository
cd /ccws/workspace/src
git clone git@git.shoggoth.local:owner/myproject.git
cd myproject

# Step 5: Implement feature (make code changes)

# Step 6: Create branch and commit
git checkout -b feature/new-endpoint
git add src/ tests/
git commit -m "Add new API endpoint for data export

Implements export functionality with CSV and JSON support.
References Redmine issue #42"

# Step 7: Push and create MR
git push origin feature/new-endpoint
tea pr create \
  --repo owner/myproject \
  --base main \
  --head feature/new-endpoint \
  --title "Add new API endpoint for data export" \
  --description "Implements Redmine issue #42 and subtasks #43, #44"

# Step 8: Update Redmine with MR URL
redmine issues update 42 --note "Merge request created: http://git.shoggoth.local/owner/myproject/pulls/1"

# Step 9: Address review comments (if any)
tea pulls 1 --repo owner/myproject --comments -o json
# Review comments, make fixes, then:
git add src/fix.py
git commit -m "Address review comment: fix edge case in export"
git push origin feature/new-endpoint
curl -s -X POST "http://git.shoggoth.local/api/v1/repos/owner/myproject/issues/1/comments" \
  -H "Authorization: token <token>" \
  -H "Content-Type: application/json" \
  -d '{"body": "Fixed: handle edge case in export function"}'

# Step 10: Resolve all completed tasks
redmine issues update 42 --status "Resolved" --note "Implementation completed. MR: http://git.shoggoth.local/owner/myproject/pulls/1"
redmine issues update 43 --status "Resolved" --note "Completed as part of parent issue #42. MR: http://git.shoggoth.local/owner/myproject/pulls/1"
redmine issues update 44 --status "Resolved" --note "Completed as part of parent issue #42. MR: http://git.shoggoth.local/owner/myproject/pulls/1"
```

## Best Practices

1. **Always check for subtasks**: Parent tasks often have children that need individual resolution
2. **Use descriptive branch names**: Follow convention `feature/<description>` or `fix/<description>`
3. **Reference Redmine issues**: Include issue IDs in commit messages and PR descriptions
4. **Resolve all related tasks**: Don't forget to resolve subtasks, not just the parent
5. **Provide MR links**: Always add merge request URLs to Redmine issues for traceability
6. **Use JSON output**: When parsing CLI output programmatically, always use `-o json` flag
7. **Verify before resolving**: Ensure implementation is complete before marking tasks as resolved
8. **Address all PR comments**: Systematically respond to each review comment with a fix
9. **Post fix confirmations**: Add a comment for each addressed review thread indicating what was changed
10. **Push fixes to same branch**: Additional commits go to the existing feature branch, no new PR needed

## Common Status Values

Redmine issue statuses may include:
- `New` - Task created but not started
- `In Progress` - Task being worked on
- `Resolved` - Implementation completed, awaiting review/merge
- `Closed` - Task fully completed and verified
- `Feedback` - Task needs clarification

Use `redmine statuses list -o json` to see available statuses in your Redmine instance.

## Troubleshooting

### Repository Clone Fails
- Check SSH key configuration for SSH URLs
- Verify credentials for HTTP URLs
- Ensure repository exists and you have access: `tea repos list --search <name>`

### Merge Request Creation Fails
- Verify branch was pushed: `git branch -r`
- Check repository permissions: `tea repos get --owner <owner> --name <repo>`
- Ensure base branch exists in target repository
- **Do NOT create a fork as a workaround** — if PR creation fails, inform the user and stop. Report the error and repository details so the user can grant permissions or choose an alternative approach.

### Redmine Update Fails
- Verify issue ID exists: `redmine issues get <id>`
- Check status name is valid: `redmine statuses list`
- Ensure you have permission to update the issue
