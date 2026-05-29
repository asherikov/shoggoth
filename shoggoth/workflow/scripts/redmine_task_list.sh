#!/usr/bin/env bash
set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/workflow/env}"
set -a
source "${ENV_FILE}"
set +a

TASK_IDS="$(redmine issues list --status="In Progress" --assignee="shoggoth shoggoth" --limit=100 --output=json | jq -c '[.[].id]')"
echo "::{\"outputs\":{\"task_ids\":${TASK_IDS}}}::"