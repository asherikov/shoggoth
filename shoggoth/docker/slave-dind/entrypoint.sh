#!/bin/sh
set -eu

export DOCKER_HOST=unix:///dind/docker.sock

mkdir -p /etc/docker/
printf '{"insecure-registries":["docker-registry.%s"]}\n' "${SHOGGOTH_DOMAIN}" > /etc/docker/daemon.json

cleanup() {
    echo "Shutting down..." >&2
    for PID in ${LOGS_PIDS}; do
        kill "${PID}" 2>/dev/null || true
    done
    docker compose -f /shoggoth/compose/docker-compose.yml down 2>/dev/null || true
    kill "${DOCKERD_PID}" 2>/dev/null || true
    kill "${CRED_CACHE_PID}" 2>/dev/null || true
    ssh-agent -k 2>/dev/null || true
    wait "${DOCKERD_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting dockerd..." >&2
dockerd --host unix:///dind/docker.sock --config-file /etc/docker/daemon.json &
DOCKERD_PID=$!

echo "Waiting for dockerd to be ready..." >&2
ATTEMPT=1
while [ "${ATTEMPT}" -le 60 ]; do
    if docker info > /dev/null 2>&1; then
        echo "dockerd is ready (attempt ${ATTEMPT})" >&2
        break
    fi
    sleep 1
    ATTEMPT=$((ATTEMPT + 1))
done

if ! docker info > /dev/null 2>&1; then
    echo "FATAL: dockerd failed to start" >&2
    exit 1
fi

BASE_IMAGE="docker-registry.${SHOGGOTH_DOMAIN}/slave_noble"

echo "Pulling base image ${BASE_IMAGE}:latest..." >&2
ATTEMPT=1
while [ "${ATTEMPT}" -le 30 ]; do
    if docker pull "${BASE_IMAGE}:latest"; then
        break
    fi
    echo "Pull attempt ${ATTEMPT} failed, retrying in 5 seconds..." >&2
    sleep 5
    ATTEMPT=$((ATTEMPT + 1))
done

if ! docker image inspect "${BASE_IMAGE}:latest" >/dev/null 2>&1; then
    echo "FATAL: Failed to pull base image ${BASE_IMAGE}:latest after 30 attempts" >&2
    exit 1
fi

echo "Building shoggoth-slave image..." >&2
BUILD_DIR="/shoggoth/compose/build"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/workflow" "${BUILD_DIR}/scripts"

echo "Generating client configuration..." >&2
/host_volumes/setup-client.sh \
    --client-conf "${BUILD_DIR}/workflow" \
    --domain "${SHOGGOTH_DOMAIN}" \
    --api-gateway \
    --gitea-user "$(cat /run/secrets/gitea_user)" \
    --ai-token litellm
mv "${BUILD_DIR}/workflow/qwen.json" "${BUILD_DIR}/workflow/qwen-settings.json"
cp -r /host_volumes/workflow-scripts/* "${BUILD_DIR}/scripts/"

DOCKERFILE="${BUILD_DIR}/Dockerfile"
cat > "${DOCKERFILE}" <<'DOCKERFILE_HEAD'
FROM docker-registry.__SHOGGOTH_DOMAIN__/slave_noble:latest

ENV SSH_AUTH_SOCK="/shoggoth/git-cred/ssh_auth_sock"
ENV CCWS_CACHE="/cache/ccws/"
ENV PIP_CACHE_DIR="/cache/ccws/pip"

RUN mkdir -p /home/ccws/.qwen
COPY workflow/qwen-settings.json /home/ccws/.qwen/settings.json

USER root

RUN mkdir -p /root/.qwen \
    && chown -R ccws /home/ccws/.qwen \
    && git config --system credential.helper 'cache --socket=/shoggoth/git-cred/git_credential_sock' \
    && printf 'Host *\n  UserKnownHostsFile /shoggoth/git-cred/known_hosts\n' > /etc/ssh/ssh_config.d/shoggoth.conf

COPY workflow/apt-cache.conf /etc/apt/apt.conf.d/00-shoggoth-apt-cache
COPY workflow/qwen-settings.json /root/.qwen/settings.json
COPY scripts/ /shoggoth/workflow/scripts/

USER ccws

DOCKERFILE_HEAD

sed -i "s|__SHOGGOTH_DOMAIN__|${SHOGGOTH_DOMAIN}|g" "${DOCKERFILE}"

while IFS='=' read -r key value; do
    [ -z "${key}" ] && continue
    case "${key}" in
        \#*) continue ;;
    esac
    printf 'ENV %s="%s"\n' "${key}" "${value}" >> "${DOCKERFILE}"
done < "${BUILD_DIR}/workflow/env"

echo "Creating ci_cache directory..." >&2
mkdir -p /shoggoth/ci_cache
chown -R 1000:1000 /shoggoth/ci_cache

CREDENTIAL_TIMEOUT=31536000
SSH_AUTH_SOCK="/shoggoth/git-cred/ssh_auth_sock"
GIT_CREDENTIAL_SOCK="/shoggoth/git-cred/git_credential_sock"
KNOWN_HOSTS="/shoggoth/git-cred/known_hosts"

echo "Preparing git-cred directory..." >&2
mkdir -p /shoggoth/git-cred
chmod 0700 /shoggoth/git-cred

echo "Cleaning up stale sockets..." >&2
rm -f "${SSH_AUTH_SOCK}" "${GIT_CREDENTIAL_SOCK}"

echo "Scanning Gitea SSH host keys on git.${SHOGGOTH_DOMAIN}..." >&2
for ATTEMPT in $(seq 1 30); do
    if ssh-keyscan "git.${SHOGGOTH_DOMAIN}" > "${KNOWN_HOSTS}.tmp" 2>/dev/null; then
        mv "${KNOWN_HOSTS}.tmp" "${KNOWN_HOSTS}"
        chmod 644 "${KNOWN_HOSTS}"
        echo "ssh-keyscan succeeded on attempt ${ATTEMPT}" >&2
        break
    fi
    SLEEP=$((ATTEMPT < 10 ? 2 : 5))
    echo "Waiting for Gitea SSH on git.${SHOGGOTH_DOMAIN} (attempt ${ATTEMPT}/30)..." >&2
    sleep "${SLEEP}"
done

if [ ! -s "${KNOWN_HOSTS}" ]; then
    echo "FATAL: ssh-keyscan failed after 30 attempts" >&2
    exit 1
fi

echo "Starting SSH agent at ${SSH_AUTH_SOCK}..." >&2
eval "$(ssh-agent -a "${SSH_AUTH_SOCK}")"
chmod 600 "${SSH_AUTH_SOCK}"

echo "Adding SSH identity..." >&2
ssh-add -v /run/secrets/ssh_id_rsa

echo "Listing SSH agent identities:" >&2
ssh-add -l >&2

echo "Starting git credential cache daemon at ${GIT_CREDENTIAL_SOCK}..." >&2
git credential-cache--daemon "${GIT_CREDENTIAL_SOCK}" &
CRED_CACHE_PID=$!
sleep 1
chmod 600 "${GIT_CREDENTIAL_SOCK}"

echo "Storing Gitea HTTP credentials..." >&2
GITEA_SERVER_TOKEN="$(cat /run/secrets/gitea_server_token)"
printf 'protocol=http\nhost=git.%s\nusername=token\npassword=%s\n' \
    "${SHOGGOTH_DOMAIN}" "${GITEA_SERVER_TOKEN}" \
    | git credential-cache --socket "${GIT_CREDENTIAL_SOCK}" --timeout "${CREDENTIAL_TIMEOUT}" store

echo "slave-dind setup complete" >&2

COMPOSE_DIR="/shoggoth/compose"
mkdir -p "${COMPOSE_DIR}"

cat > "${COMPOSE_DIR}/docker-compose.yml" <<EOF
services:
    slave-term:
        build:
            context: ${BUILD_DIR}
        image: docker-registry.${SHOGGOTH_DOMAIN}/slave_noble:dind
        container_name: slave-term
        restart: unless-stopped
        network_mode: host
        volumes:
            - /shoggoth/git-cred:/shoggoth/git-cred
            - /shoggoth/ci_cache:/cache
        command: ttyd --max-clients 1 --port 80 --writable -t disableReconnect=true --once tmux -u new-session qwen
EOF

docker compose -f "${COMPOSE_DIR}/docker-compose.yml" up -d --build

LOGS_PIDS=""
for service in $(docker compose -f "${COMPOSE_DIR}/docker-compose.yml" config --services); do
    docker compose -f "${COMPOSE_DIR}/docker-compose.yml" logs -f "${service}" &
    LOGS_PIDS="${LOGS_PIDS} $!"
done

wait "${DOCKERD_PID}" || true
