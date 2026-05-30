#!/usr/bin/env bash
set -euo pipefail

CREDENTIAL_TIMEOUT=31536000
SSH_AUTH_SOCK="/shoggoth/git-cred/ssh_auth_sock"
GIT_CREDENTIAL_SOCK="/shoggoth/git-cred/git_credential_sock"
KNOWN_HOSTS="/shoggoth/git-cred/known_hosts"
GITEA_SERVER_TOKEN="$(cat /shoggoth/git-cred/secrets/gitea_server_token)"

echo "Cleaning up stale sockets..." >&2
rm -f "${SSH_AUTH_SOCK}" "${GIT_CREDENTIAL_SOCK}"
chmod 0700 /shoggoth/git-cred

echo "Scanning Gitea SSH host keys on git.${SHOGGOTH_DOMAIN}:${SHOGGOTH_GIT_SSH_PORT:-3022}..." >&2
for ATTEMPT in $(seq 1 30); do
    if ssh-keyscan -p "${SHOGGOTH_GIT_SSH_PORT:-3022}" "git.${SHOGGOTH_DOMAIN}" > "${KNOWN_HOSTS}.tmp" 2>/dev/null; then
        mv "${KNOWN_HOSTS}.tmp" "${KNOWN_HOSTS}"
        chmod 644 "${KNOWN_HOSTS}"
        echo "ssh-keyscan succeeded on attempt ${ATTEMPT}" >&2
        break
    fi
    SLEEP=$((ATTEMPT < 10 ? 2 : 5))
    echo "Waiting for Gitea SSH on git.${SHOGGOTH_DOMAIN}:${SHOGGOTH_GIT_SSH_PORT:-3022} (attempt ${ATTEMPT}/30)..." >&2
    sleep "${SLEEP}"
done

if [ ! -s "${KNOWN_HOSTS}" ]; then
    echo "FATAL: ssh-keyscan failed after 30 attempts" >&2
    exit 1
fi

echo "Known hosts content:" >&2
cat "${KNOWN_HOSTS}" >&2

echo "Starting SSH agent at ${SSH_AUTH_SOCK}..." >&2
eval "$(ssh-agent -a "${SSH_AUTH_SOCK}")"
chmod 600 "${SSH_AUTH_SOCK}"

echo "Adding SSH identity /shoggoth/git-cred/secrets/ssh_id_rsa..." >&2
ssh-add -v /shoggoth/git-cred/secrets/ssh_id_rsa

echo "Listing SSH agent identities:" >&2
ssh-add -l >&2

echo "Starting git credential cache daemon at ${GIT_CREDENTIAL_SOCK}..." >&2
git credential-cache--daemon "${GIT_CREDENTIAL_SOCK}" &
sleep 1
chmod 600 "${GIT_CREDENTIAL_SOCK}"

echo "Storing Gitea HTTP credentials..." >&2
printf 'protocol=http\nhost=git.%s\nusername=token\npassword=%s\n' \
    "${SHOGGOTH_DOMAIN}" "${GITEA_SERVER_TOKEN}" \
    | git credential-cache --socket "${GIT_CREDENTIAL_SOCK}" --timeout "${CREDENTIAL_TIMEOUT}" store

echo "git-cred setup complete, entering idle loop" >&2
exec sleep "${CREDENTIAL_TIMEOUT}"