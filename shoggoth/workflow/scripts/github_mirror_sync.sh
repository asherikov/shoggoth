#!/usr/bin/env bash
set -euo pipefail

GITHUB_ORG="${1:?Usage: $0 <github_org>}"

GITEA_API="${GITEA_SERVER_URL}/api/v1"
GITEA_AUTH_TOKEN="${GITEA_SERVER_TOKEN:-${GITEA_TOKEN}}"

GITHUB_API="https://api.github.com"

GITEA_HOST="git.${SHOGGOTH_DOMAIN}"
GITEA_HTTP_URL="http://${GITEA_AUTH_TOKEN}@${GITEA_HOST}"

SYNC_TMPDIR="$(mktemp -d)"
trap 'rm -rf "${SYNC_TMPDIR}"' EXIT

echo "=== Syncing GitHub '${GITHUB_ORG}' to Gitea org '${GITHUB_ORG}' ==="

if [ -n "${GITHUB_TOKEN:-}" ]; then
    GITHUB_AUTH_HEADER=(-H "Authorization: token ${GITHUB_TOKEN}")
else
    GITHUB_AUTH_HEADER=()
fi

GITHUB_ACCOUNT_TYPE=""
org_check="$(curl -s -o /dev/null -w "%{http_code}" \
    "${GITHUB_API}/orgs/${GITHUB_ORG}" "${GITHUB_AUTH_HEADER[@]}")"
if [ "${org_check}" = "200" ]; then
    GITHUB_ACCOUNT_TYPE="orgs"
else
    user_check="$(curl -s -o /dev/null -w "%{http_code}" \
        "${GITHUB_API}/users/${GITHUB_ORG}" "${GITHUB_AUTH_HEADER[@]}")"
    if [ "${user_check}" = "200" ]; then
        GITHUB_ACCOUNT_TYPE="users"
    else
        echo "ERROR: GitHub account '${GITHUB_ORG}' is neither an organization nor a user (org: ${org_check}, user: ${user_check})"
        exit 1
    fi
fi
echo "GitHub account type: ${GITHUB_ACCOUNT_TYPE}"

page=1
while true; do
    resp="$(curl -sS \
        "${GITHUB_API}/${GITHUB_ACCOUNT_TYPE}/${GITHUB_ORG}/repos?page=${page}&per_page=100&type=public" \
        "${GITHUB_AUTH_HEADER[@]}" || true)"

    if [ -z "${resp}" ] || ! echo "${resp}" | jq -e 'type == "array"' > /dev/null 2>&1; then
        echo "WARNING: Failed to list repos for GitHub ${GITHUB_ACCOUNT_TYPE} '${GITHUB_ORG}' (page ${page}), stopping"
        if [ -n "${resp}" ]; then
            echo "${resp}" | head -c 500
            echo
        fi
        exit 1
    fi

    count="$(echo "${resp}" | jq 'length')"
    if [ "${count}" -eq 0 ]; then
        break
    fi

    echo "${resp}" | jq -r '.[] | .name + "\t" + (.description // "") + "\t" + (.pushed_at // "")' | while IFS=$'\t' read -r repo_name repo_desc repo_pushed_at; do
        echo "--- Processing ${GITHUB_ORG}/${repo_name} ---"

        repo_check="$(curl -s -o /dev/null -w "%{http_code}" \
            "${GITEA_API}/repos/${GITHUB_ORG}/${repo_name}" \
            -H "Authorization: token ${GITEA_AUTH_TOKEN}")"

        if [ "${repo_check}" = "404" ]; then
            echo "Repository ${GITHUB_ORG}/${repo_name} does not exist in Gitea, creating..."

            org_check="$(curl -s -o /dev/null -w "%{http_code}" \
                "${GITEA_API}/orgs/${GITHUB_ORG}" \
                -H "Authorization: token ${GITEA_AUTH_TOKEN}")"
            if [ "${org_check}" = "404" ]; then
                echo "Creating Gitea organization: ${GITHUB_ORG}"
                curl -sfS -X POST \
                    "${GITEA_API}/orgs" \
                    -H "Authorization: token ${GITEA_AUTH_TOKEN}" \
                    -H "Content-Type: application/json" \
                    -d "{\"username\": \"${GITHUB_ORG}\", \"full_name\": \"${GITHUB_ORG}\"}" \
                    > /dev/null
            fi

            desc_escaped="$(echo "${repo_desc}" | jq -Rs '.')"
            if curl -sfS \
                "${GITEA_API}/repos/migrate" \
                -H "Authorization: token ${GITEA_AUTH_TOKEN}" \
                -H "Content-Type: application/json" \
                -d "{ \
                    \"clone_addr\": \"https://github.com/${GITHUB_ORG}/${repo_name}\", \
                    \"description\": ${desc_escaped}, \
                    \"issues\": false, \
                    \"labels\": false, \
                    \"milestones\": false, \
                    \"mirror\": false, \
                    \"private\": false, \
                    \"pull_requests\": false, \
                    \"releases\": false, \
                    \"repo_name\": \"${repo_name}\", \
                    \"repo_owner\": \"${GITHUB_ORG}\", \
                    \"service\": \"git\", \
                    \"wiki\": false \
                }" > /dev/null; then
                echo "Migrated ${GITHUB_ORG}/${repo_name}"
            else
                echo "WARNING: Failed to migrate ${GITHUB_ORG}/${repo_name}, will sync on next run"
            fi
        elif [ "${repo_check}" = "200" ]; then
            gitea_repo_json="$(curl -sS \
                "${GITEA_API}/repos/${GITHUB_ORG}/${repo_name}" \
                -H "Authorization: token ${GITEA_AUTH_TOKEN}")"

            gitea_updated_at="$(echo "${gitea_repo_json}" | jq -r '.updated_at // ""')"
            if [ -n "${repo_pushed_at}" ] && [ -n "${gitea_updated_at}" ] \
               && [ "${repo_pushed_at}" \< "${gitea_updated_at}" ]; then
                echo "Repository ${GITHUB_ORG}/${repo_name} is up to date (GitHub pushed ${repo_pushed_at} <= Gitea updated ${gitea_updated_at}), skipping"
                continue
            fi

            echo "Repository ${GITHUB_ORG}/${repo_name} exists, syncing branches and tags..."

            clone_dir="${SYNC_TMPDIR}/${repo_name}"
            if ! git clone --quiet --bare "${GITEA_HTTP_URL}/${GITHUB_ORG}/${repo_name}.git" "${clone_dir}" 2>&1; then
                echo "WARNING: Failed to clone ${GITHUB_ORG}/${repo_name} from Gitea, skipping"
                continue
            fi

            if ! git -C "${clone_dir}" fetch --quiet --tags "https://github.com/${GITHUB_ORG}/${repo_name}.git" "+refs/heads/*:refs/remotes/github/*" 2>&1; then
                echo "WARNING: Failed to fetch from GitHub for ${GITHUB_ORG}/${repo_name}, skipping"
                continue
            fi

            changed_branches=()
            while IFS= read -r ref; do
                branch_name="${ref#refs/remotes/github/}"
                if [ "${branch_name}" = "HEAD" ]; then
                    continue
                fi

                remote_sha="$(git -C "${clone_dir}" rev-parse "${ref}")"
                if git -C "${clone_dir}" rev-parse --verify -q "refs/heads/${branch_name}" > /dev/null 2>&1; then
                    local_sha="$(git -C "${clone_dir}" rev-parse "refs/heads/${branch_name}")"
                    if [ "${local_sha}" = "${remote_sha}" ]; then
                        echo "  Branch '${branch_name}' already up to date"
                        continue
                    fi
                    if git -C "${clone_dir}" merge-base --is-ancestor "refs/heads/${branch_name}" "${ref}" 2>/dev/null; then
                        echo "  Branch '${branch_name}': fast-forwarding"
                        git -C "${clone_dir}" branch -f "${branch_name}" "${ref}" 2>/dev/null || true
                        changed_branches+=("${branch_name}")
                    else
                        echo "  WARNING: Branch '${branch_name}' has conflicting history, skipping"
                        continue
                    fi
                fi
            done < <(git -C "${clone_dir}" for-each-ref --format='%(refname)' refs/remotes/github/)

            if [ "${#changed_branches[@]}" -gt 0 ]; then
                echo "  Pushing ${#changed_branches[@]} changed branch(es) to Gitea"
                git -C "${clone_dir}" push --quiet origin "${changed_branches[@]}" 2>&1 || echo "  WARNING: Failed to push branches to Gitea"
            else
                echo "  No changed branches to push"
            fi

            if git -C "${clone_dir}" tag --list | grep -q .; then
                echo "  Pushing tags to Gitea"
                git -C "${clone_dir}" push --quiet --tags origin 2>&1 || echo "  WARNING: Failed to push tags to Gitea"
            else
                echo "  No tags to push"
            fi

            echo "Synced ${GITHUB_ORG}/${repo_name}"
        else
            echo "WARNING: Unexpected response ${repo_check} when checking ${GITHUB_ORG}/${repo_name}, skipping"
        fi
    done

    if [ "${count}" -lt 100 ]; then
        break
    fi
    page=$((page + 1))
done

echo "=== Sync complete for '${GITHUB_ORG}' ==="