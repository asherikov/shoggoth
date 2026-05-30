#!/bin/sh
set -eu

export DOCKER_HOST=unix:///dind/docker.sock

mkdir -p /etc/docker/
printf '{"insecure-registries":["docker-registry.%s"]}\n' "${SHOGGOTH_DOMAIN}" > /etc/docker/daemon.json

cleanup() {
    echo "Shutting down..." >&2
    kill "${DOCKERD_PID}" 2>/dev/null || true
    wait "${DOCKERD_PID}" 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT

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
BUILD_DIR=$(mktemp -d)
cleanup_build() { rm -rf "${BUILD_DIR}"; }
trap cleanup_build EXIT

echo "Generating client configuration..." >&2
mkdir -p "${BUILD_DIR}/workflow"
mkdir -p "${BUILD_DIR}/scripts"
/host_volumes/setup-client.sh \
    --client-conf "${BUILD_DIR}/workflow" \
    --host "${SHOGGOTH_DOMAIN}" --host-ip "${SHOGGOTH_IP}" \
    --gitea-token "$(cat /run/secrets/gitea_server_token)" \
    --gitea-user "$(cat /run/secrets/gitea_user)" \
    --ai-token litellm \
    --redmine-token "$(cat /run/secrets/redmine_token)"
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

docker build -t "${BASE_IMAGE}:dind" "${BUILD_DIR}"

echo "Creating ci_cache directory..." >&2
mkdir -p /shoggoth/ci_cache
chown -R 1000:1000 /shoggoth/ci_cache

echo "Preparing git-cred directory and secrets..." >&2
mkdir -p /shoggoth/git-cred/secrets
cp /run/secrets/ssh_id_rsa /shoggoth/git-cred/secrets/ssh_id_rsa
cp /run/secrets/gitea_server_token /shoggoth/git-cred/secrets/gitea_server_token
chmod 600 /shoggoth/git-cred/secrets/ssh_id_rsa /shoggoth/git-cred/secrets/gitea_server_token
chown -R 1000:1000 /shoggoth/git-cred

echo "Starting git-cred container..." >&2
docker rm -f git-cred > /dev/null 2>&1 || true
docker run \
    --detach \
    --name git-cred \
    --restart unless-stopped \
    --network host \
    --volume /shoggoth/git-cred:/shoggoth/git-cred \
    --volume /host_volumes/git-cred-entrypoint:/shoggoth_entrypoint.sh:ro \
    --env SHOGGOTH_DOMAIN="${SHOGGOTH_DOMAIN}" \
    --env SHOGGOTH_GIT_SSH_PORT="${SHOGGOTH_GIT_SSH_PORT}" \
    docker-registry."${SHOGGOTH_DOMAIN}"/slave_noble:latest \
    /shoggoth_entrypoint.sh

echo "dind-slave setup complete" >&2
wait "${DOCKERD_PID}" || true
