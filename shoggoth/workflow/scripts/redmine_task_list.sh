#!/usr/bin/env bash
set -euxo pipefail

TASK_IDS="$(redmine issues list --status="In Progress" --assignee="sslave" --limit=100 --output=json | jq -c '[.[].id]')"
echo "::{\"outputs\":{\"task_ids\":${TASK_IDS}}}::"
