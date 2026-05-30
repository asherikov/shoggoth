#!/usr/bin/env bash

set -euo pipefail

cp /shoggoth/workflow/qwen-settings.json "${HOME}/.qwen/settings.json"

OTEL_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT}"

normalize_for_branch() {
    local INPUT="$1"
    echo "${INPUT}" \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/[^a-z0-9]/-/g' \
        | sed 's/-\+/-/g' \
        | sed 's/^-\|-$//g'
}

resolve_task() {
    local BRANCH="$1"
    if ! echo "${BRANCH}" | grep -q '/'; then
        return
    fi
    local BRANCH_PROJECT
    BRANCH_PROJECT="$(echo "${BRANCH}" | cut -d'/' -f1)"
    local BRANCH_SUBJECT
    BRANCH_SUBJECT="$(normalize_for_branch "$(echo "${BRANCH}" | cut -d'/' -f2-)")"
    local REDMINE_PROJECT
    REDMINE_PROJECT="$(redmine projects list --output=json 2>/dev/null | jq -r --arg norm "${BRANCH_PROJECT}" '.[] | select(.identifier == $norm or (.name | ascii_downcase | gsub("[^a-z0-9]";"-") | gsub("-+";"-") | gsub("^-|-$";"")) == $norm) | .identifier' | head -1)" || true
    if [ -z "${REDMINE_PROJECT}" ]; then
        return
    fi
    redmine issues list --project "${REDMINE_PROJECT}" --limit=100 --output=json 2>/dev/null | jq -r --arg norm "${BRANCH_SUBJECT}" '.[] | select((.subject | ascii_downcase | gsub("[^a-z0-9]";"-") | gsub("-+";"-") | gsub("^-|-$";"")) == $norm) | .id' | head -1
}

resolve_project() {
    local BRANCH="$1"
    local REPO="$2"
    local BRANCH_PREFIX=""
    if echo "${BRANCH}" | grep -q '/'; then
        BRANCH_PREFIX="$(echo "${BRANCH}" | cut -d'/' -f1)"
        LOCAL_NORMALIZED="$(normalize_for_branch "${BRANCH_PREFIX}")"
        REDMINE_PROJECT="$(redmine projects list --output=json 2>/dev/null | jq -r --arg norm "${LOCAL_NORMALIZED}" '.[] | select(.identifier == $norm or (.name | ascii_downcase | gsub("[^a-z0-9]";"-") | gsub("-+";"-") | gsub("^-|-$";"")) == $norm) | .identifier' | head -1)" || true
        if [ -n "${REDMINE_PROJECT}" ]; then
            echo "${REDMINE_PROJECT}"
            return
        fi
    fi
    echo "${REPO}" | sed 's/\.git$//'
}

clone_if_missing() {
    local REPO="$1"
    local REPO_DIR
    REPO_DIR="$(echo "${REPO}" | cut -d'/' -f2)"
    if [ ! -d "${REPO_DIR}" ]; then
        tea repos list --owner "$(echo "${REPO}" | cut -d'/' -f1)" -o json | jq -r ".[].ssh_url" | head -1 | xargs git clone
    fi
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

start_session_log() {
    local EVENT_TYPE="${1}"
    SESSION_ID="$(cat /proc/sys/kernel/random/uuid)"
    local FIFO_DIR="/tmp/qwen-session-${SESSION_ID}"
    mkdir -p "${FIFO_DIR}"
    SESSION_LOG_FIFO="${FIFO_DIR}/events.jsonl"
    mkfifo "${SESSION_LOG_FIFO}"
    otlp_log_push \
        "${OTEL_OTLP_ENDPOINT}" \
        "qwen-${EVENT_TYPE}" \
        "${SESSION_ID}" \
        "${SESSION_LOG_FIFO}" \
        &
    SESSION_LOG_PID=$!
    exec 3>"${SESSION_LOG_FIFO}"
}

qwen_with_log() {
    qwen --yolo --output-format stream-json "$@" > "${SESSION_LOG_FIFO}" 2>/dev/null
}

stop_session_log() {
    exec 3>&-
    wait "${SESSION_LOG_PID}" 2>/dev/null || true
    rm -rf "$(dirname "${SESSION_LOG_FIFO}")"
}

cmd_ci_failure() {
    local PAYLOAD="${GITEA_PAYLOAD}"

    local CONCLUSION
    CONCLUSION="$(echo "${PAYLOAD}" | jq -r '.workflow_run.conclusion')"
    if [ "${CONCLUSION}" != "failure" ]; then
        echo "Ignoring workflow_run conclusion: ${CONCLUSION}"
        exit 0
    fi

    local CI_REPO CI_SHA CI_BRANCH CI_RUN_URL CI_WORKFLOW
    CI_REPO="$(echo "${PAYLOAD}" | jq -r '.repository.full_name')"
    CI_SHA="$(echo "${PAYLOAD}" | jq -r '.workflow_run.head_sha')"
    CI_BRANCH="$(echo "${PAYLOAD}" | jq -r '.workflow_run.head_branch')"
    CI_RUN_URL="$(echo "${PAYLOAD}" | jq -r '.workflow_run.html_url')"
    CI_WORKFLOW="$(echo "${PAYLOAD}" | jq -r '.workflow.name')"

    export SHOGGOTH_REPO="${CI_REPO}"
    if [ -z "${SHOGGOTH_PROJECT:-}" ]; then
        export SHOGGOTH_PROJECT="$(resolve_project "${CI_BRANCH}" "$(echo "${CI_REPO}" | cut -d'/' -f2)")"
    fi

    cd /ccws/workspace/src

    clone_if_missing "${CI_REPO}"

    local REPO_DIR
    REPO_DIR="$(echo "${CI_REPO}" | cut -d'/' -f2)"
    cd "${REPO_DIR}"
    git fetch origin
    git checkout "${CI_SHA}"

    local CI_LOGS
    CI_LOGS="$(tea actions runs view --repo "${CI_REPO}" --log 2>/dev/null || echo 'CI logs unavailable')"

    start_session_log "ci-failure"

    qwen_with_log --session-id "${SESSION_ID}" --prompt "CI workflow '${CI_WORKFLOW}' failed on repository ${CI_REPO} at commit ${CI_SHA} (branch ${CI_BRANCH}).
Run URL: ${CI_RUN_URL}

CI logs:
${CI_LOGS}

Fetch the CI log and try to resolve the issue. Fix the code, commit, and push."

    stop_session_log
}

cmd_pr_update() {
    local PAYLOAD="${GITEA_PAYLOAD}"

    local ACTION
    ACTION="$(echo "${PAYLOAD}" | jq -r '.action')"
    if [ "${ACTION}" = "deleted" ]; then
        exit 0
    fi

    local PR_NUMBER PR_URL PR_REPO PR_BRANCH CLONE_URL
    PR_NUMBER="$(echo "${PAYLOAD}" | jq -r '.pull_request.number')"
    PR_URL="$(echo "${PAYLOAD}" | jq -r '.pull_request.html_url')"
    PR_REPO="$(echo "${PAYLOAD}" | jq -r '.repository.full_name')"
    PR_BRANCH="$(echo "${PAYLOAD}" | jq -r '.pull_request.head.ref')"
    CLONE_URL="$(echo "${PAYLOAD}" | jq -r '.repository.ssh_url')"

    export SHOGGOTH_REPO="${PR_REPO}"
    if [ -z "${SHOGGOTH_PROJECT:-}" ]; then
        export SHOGGOTH_PROJECT="$(resolve_project "${PR_BRANCH}" "$(echo "${PR_REPO}" | cut -d'/' -f2)")"
    fi

    local TASK_SUBJECT=""
    if echo "${PR_BRANCH}" | grep -q '/'; then
        TASK_SUBJECT="$(echo "${PR_BRANCH}" | cut -d'/' -f2-)"
    fi

    mkdir -p /ccws/workspace/src
    cd /ccws/workspace/src

    local REPO_DIR
    REPO_DIR="$(echo "${PR_REPO}" | cut -d'/' -f2)"

    if [ ! -d "${REPO_DIR}" ]; then
        git clone "${CLONE_URL}" "${REPO_DIR}"
    fi

    cd "${REPO_DIR}"
    git fetch origin
    git checkout "origin/${PR_BRANCH}"

    local HAS_REVIEW
    HAS_REVIEW="$(echo "${PAYLOAD}" | jq 'has("review")')"

    if [ "${ACTION}" = "opened" ] && [ "${HAS_REVIEW}" = "false" ]; then
        cmd_pr_review "${PR_URL}" "${PR_BRANCH}"
        return
    fi

    cmd_pr_comment "${PAYLOAD}" "${PR_NUMBER}" "${PR_URL}" "${PR_REPO}" "${PR_BRANCH}" "${TASK_SUBJECT}"
}

cmd_pr_review() {
    local PR_URL="${1}"
    local PR_BRANCH="${2}"

    start_session_log "pr-review"

    qwen_with_log --session-id "${SESSION_ID}" --prompt "/review ${PR_URL}"

    stop_session_log
}

cmd_pr_comment() {
    local PAYLOAD="${1}"
    local PR_NUMBER="${2}"
    local PR_URL="${3}"
    local PR_REPO="${4}"
    local PR_BRANCH="${5}"
    local TASK_SUBJECT="${6}"

    local PR_OWNER PR_REPO_NAME API_AUTH
    PR_OWNER="$(echo "${PR_REPO}" | cut -d'/' -f1)"
    PR_REPO_NAME="$(echo "${PR_REPO}" | cut -d'/' -f2)"
    API_AUTH=(-H "Authorization: token ${GITEA_SERVER_TOKEN}" -H "Content-Type: application/json")
    API_BASE="${GITEA_SERVER_URL}/api/v1/repos/${PR_OWNER}/${PR_REPO_NAME}"

    local ALL_REVIEWS="[]"
    local PAGE=1
    local LIMIT=50

    while true; do
        local PAGE_JSON
        PAGE_JSON="$(curl -sfS \
            "${API_AUTH[@]}" \
            "${API_BASE}/pulls/${PR_NUMBER}/reviews?page=${PAGE}&limit=${LIMIT}")"

        if [ -z "${PAGE_JSON}" ] || echo "${PAGE_JSON}" | jq -e 'length == 0' >/dev/null; then
            break
        fi

        ALL_REVIEWS="$(echo "${ALL_REVIEWS}" | jq -c --argjson page "${PAGE_JSON}" '. + $page')"

        if [ "$(echo "${PAGE_JSON}" | jq 'length')" -lt "${LIMIT}" ]; then
            break
        fi

        PAGE=$((PAGE + 1))
    done

    local ALL_COMMENTS="[]"
    local REVIEW_ID
    for REVIEW_ID in $(echo "${ALL_REVIEWS}" | jq -r '.[].id'); do
        local PAGE=1
        local LIMIT=50

        while true; do
            local PAGE_JSON
            PAGE_JSON="$(curl -sfS \
                "${API_AUTH[@]}" \
                "${API_BASE}/pulls/${PR_NUMBER}/reviews/${REVIEW_ID}/comments?page=${PAGE}&limit=${LIMIT}")"

            if [ -z "${PAGE_JSON}" ] || echo "${PAGE_JSON}" | jq -e 'length == 0' >/dev/null; then
                break
            fi

            ALL_COMMENTS="$(echo "${ALL_COMMENTS}" | jq -c --argjson page "${PAGE_JSON}" '. + $page')"

            if [ "$(echo "${PAGE_JSON}" | jq 'length')" -lt "${LIMIT}" ]; then
                break
            fi

            PAGE=$((PAGE + 1))
        done
    done

    local COMMENTS_JSON
    COMMENTS_JSON="$(echo "${ALL_COMMENTS}" | jq -c '[.[] | select(.resolver == null or .resolver == "") | {id: .id, path: .path, line: .line, body: .body}]')"

    local COMMENT_COUNT
    COMMENT_COUNT="$(echo "${COMMENTS_JSON}" | jq 'length')"
    if [ "${COMMENT_COUNT}" -eq 0 ]; then
        echo "No unresolved review comments"
        return 0
    fi

    start_session_log "pr-comment"

    qwen_with_log --session-id "${SESSION_ID}" --prompt "Load memories regarding the project ${SHOGGOTH_PROJECT} from basic memory. Proceed if memory is not available."
    if [ -n "${TASK_SUBJECT}" ]; then
        qwen_with_log --resume "${SESSION_ID}" --prompt "Load memories regarding task \"${TASK_SUBJECT}\" in project ${SHOGGOTH_PROJECT} from basic memory. Proceed if memory is not available."
    fi

    local I COMMENT_BODY COMMENT_PATH COMMENT_LINE
    for I in $(seq 0 $((COMMENT_COUNT - 1))); do
        COMMENT_BODY="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].body")"
        COMMENT_PATH="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].path")"
        COMMENT_LINE="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].line")"

        qwen_with_log --resume "${SESSION_ID}" --prompt "Address the following review comment on PR ${PR_URL} (file: ${COMMENT_PATH}, line: ${COMMENT_LINE}): ${COMMENT_BODY}. Commit implemented changes in the repository."
    done

    qwen_with_log --resume "${SESSION_ID}" --prompt "Finalize all remaining work. Update basic memory with any new information learned about the project ${SHOGGOTH_PROJECT}. Then commit any remaining changes and push."
    if [ -n "${TASK_SUBJECT}" ]; then
        qwen_with_log --resume "${SESSION_ID}" --prompt "Update basic memory with any new information learned about the task \"${TASK_SUBJECT}\"."
    fi

    local COMMENT_ID RESOLVE_URL
    for I in $(seq 0 $((COMMENT_COUNT - 1))); do
        COMMENT_ID="$(echo "${COMMENTS_JSON}" | jq -r ".[${I}].id")"
        RESOLVE_URL="${GITEA_SERVER_URL}/api/v1/repos/${PR_OWNER}/${PR_REPO_NAME}/pulls/comments/${COMMENT_ID}/resolve"
        curl -sfS -X POST \
            -H "Authorization: token ${GITEA_SERVER_TOKEN}" \
            -H "Content-Type: application/json" \
            "${RESOLVE_URL}" || true
    done

    local REDMINE_TASK_ID
    REDMINE_TASK_ID="$(resolve_task "${PR_BRANCH}")" || true
    if [ -n "${REDMINE_TASK_ID}" ]; then
        redmine issues update "${REDMINE_TASK_ID}" --note "Review comments on ${PR_URL} have been addressed."
    fi

    stop_session_log
}

case "${1:-}" in
    ci-failure)
        cmd_ci_failure
        ;;
    pr-update)
        cmd_pr_update
        ;;
    *)
        echo "Usage: $0 {ci-failure|pr-update}" >&2
        exit 1
        ;;
esac
