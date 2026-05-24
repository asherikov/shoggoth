#!/usr/bin/env bash

set -euo pipefail

ENV_FILE="${SHOGGOTH_ENV:-/shoggoth/env}"
set -a
source "${ENV_FILE}"
set +a

OTEL_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"

normalize_for_branch() {
    local INPUT="$1"
    echo "${INPUT}" \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/[^a-z0-9]/-/g' \
        | sed 's/-\+/-/g' \
        | sed 's/^-\|-$//g'
}

strip_repo_to_project() {
    local repo="$1"
    repo="${repo#ssh://}"
    repo="${repo#git://}"
    repo="${repo#http://}"
    repo="${repo#https://}"
    repo="${repo#*@}"
    repo="${repo%.git}"
    if [[ "${repo}" == *:* ]]; then
        repo="${repo#*:}"
    fi
    echo "${repo}" | awk -F'/' '{print $NF}'
}

otlp_log_push() {
    local ENDPOINT="${1}"
    local SERVICE_NAME="${2}"
    local SESSION_ID="${3}"
    local FIFO="${4}"
    local BODY
    while IFS= read -r LINE; do
        BODY="$(jq -n \
            --arg sn "${SERVICE_NAME}" \
            --arg sid "${SESSION_ID}" \
            --argjson ts "$(date +%s%N)" \
            --arg body "${LINE}" \
            '{
                resourceLogs: [{
                    resource: {
                        attributes: [
                            {key: "service.name", value: {stringValue: $sn}},
                            {key: "session.id", value: {stringValue: $sid}}
                        ]
                    },
                    scopeLogs: [{
                        scope: {},
                        logRecords: [{
                            timeUnixNano: $ts,
                            observedTimeUnixNano: $ts,
                            severityNumber: 9,
                            severityText: "INFO",
                            body: {stringValue: $body}
                        }]
                    }]
                }]
            }'
        )"
        curl -sS -X POST \
            -H "Content-Type: application/json" \
            -d "${BODY}" \
            "${ENDPOINT}/v1/logs" &
        wait $!
    done < "${FIFO}"
}

TASK_ID="${1:?Usage: redmine_task_processor.sh TASK_ID}"

TASK_DETAILS="$(redmine issues get "${TASK_ID}" --journals --children --output=json)"

TASK_SUBJECT="$(echo "${TASK_DETAILS}" | jq -r '.subject')"
TASK_PROJECT="$(echo "${TASK_DETAILS}" | jq -r '.project.name // empty')"

if [ -z "${TASK_PROJECT}" ]; then
    TASK_DESCRIPTION="$(echo "${TASK_DETAILS}" | jq -r '(.description // "")')"
    TASK_REPO="$(echo "${TASK_DESCRIPTION}" | grep -Eo '(https?|ssh)://[^ ]+|git@[^ ]+' | head -1)" || true
    if [ -n "${TASK_REPO}" ]; then
        TASK_PROJECT="$(strip_repo_to_project "${TASK_REPO}")"
    fi
fi

export SHOGGOTH_PROJECT="$(normalize_for_branch "${TASK_PROJECT}")"
export SHOGGOTH_BRANCH="${SHOGGOTH_PROJECT}/$(normalize_for_branch "${TASK_SUBJECT}")"

SESSION_ID="$(cat /proc/sys/kernel/random/uuid)"
FIFO_DIR="/tmp/qwen-session-${SESSION_ID}"
mkdir -p "${FIFO_DIR}"
SESSION_LOG_FIFO="${FIFO_DIR}/events.jsonl"
mkfifo "${SESSION_LOG_FIFO}"
otlp_log_push \
    "${OTEL_OTLP_ENDPOINT}" \
    "qwen-task" \
    "${SESSION_ID}" \
    "${SESSION_LOG_FIFO}" \
    &
SESSION_LOG_PID=$!
exec 3>"${SESSION_LOG_FIFO}"

qwen --yolo --session-id "${SESSION_ID}" --output-format stream-json --prompt "Find and execute Redmine task #${TASK_ID}: ${TASK_SUBJECT}

Task details:
${TASK_DETAILS}

If the given task requires modification of repositories, create a feature branch with the name taken from the SHOGGOTH_BRANCH environment variable (${SHOGGOTH_BRANCH}) before making changes, push it after completion, and open a pull request using the Gitea CLI (tea).

After completing the task, update basic memory with any new information learned about the project ${SHOGGOTH_PROJECT} and task \"${TASK_SUBJECT}\"." > "${SESSION_LOG_FIFO}" 2>/dev/null

exec 3>&-
wait "${SESSION_LOG_PID}" 2>/dev/null || true
rm -rf "${FIFO_DIR}"

redmine issues update "${TASK_ID}" --status "Resolved"