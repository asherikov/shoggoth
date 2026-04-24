#!/usr/bin/env bash

set -euxo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

TASK_ID="${1:?Usage: redmine-task-processor.sh TASK_ID}"

TASK_DETAILS="$(redmine issues get "${TASK_ID}" --journals --children --output=json)"

TASK_SUBJECT="$(echo "${TASK_DETAILS}" | jq -r '.subject')"

qwen --yolo --output-format json --prompt "Find and execute Redmine task #${TASK_ID}: ${TASK_SUBJECT}

Task details:
${TASK_DETAILS}"

redmine issues update "${TASK_ID}" --status "Resolved"