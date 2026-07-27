#!/bin/sh
set -e

DOCKER_PROXY_PORT="${DOCKER_PROXY_PORT:-3128}"
CONFIGURE_DOCKER="${CONFIGURE_DOCKER:-}"
CONFIGURE_HOSTS="${CONFIGURE_HOSTS:-}"
CONFIGURE_ALL="${CONFIGURE_ALL:-}"
CONFIGURE_APT_CACHE="${CONFIGURE_APT_CACHE:-}"
CONFIGURE_CLIENT_CONF="${CONFIGURE_CLIENT_CONF:-}"
CONFIGURE_GITEA_USER="${CONFIGURE_GITEA_USER:-}"
CONFIGURE_AI_TOKEN="${CONFIGURE_AI_TOKEN:-ai}"
CONFIGURE_API_GATEWAY="${CONFIGURE_API_GATEWAY:-}"
CONFIGURE_SSH_CONFIG="${CONFIGURE_SSH_CONFIG:-}"
DOMAIN="${DOMAIN:-s.local}"
HOST_IP="${HOST_IP:-127.0.0.1}"
CLIENT_CONF_DIR="${CLIENT_CONF_DIR:-${HOME}/.config/shoggoth}"

PRIV_CMD=""
USE_SU=""

get_priv_cmd() {
    if command -v sudo >/dev/null 2>&1; then
        PRIV_CMD="sudo"
        USE_SU="false"
    elif command -v su >/dev/null 2>&1; then
        PRIV_CMD="su"
        USE_SU="true"
    else
        echo "Error: Neither sudo nor su is available. Cannot perform privileged operations."
        exit 1
    fi
}

run_priv_cmd() {
    if [ "$USE_SU" = "true" ]; then
        $PRIV_CMD -c "$*"
    else
        $PRIV_CMD sh -c "$*"
    fi
}

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Set up Docker client to use shoggoth proxy and generate configuration files.

Options:
    --domain DOMAIN         Domain name for service URLs and hosts entries (default: s.local)
    --host-ip IP            IP address for /etc/hosts entries (default: 127.0.0.1)
    --docker [PORT]         Configure Docker proxy, optionally with a port (default: 3128)
    --update-hosts          Append generated hosts file to /etc/hosts
    --apt-cache             Install apt cache config to system apt config
    --gitea-user USER       Configure gitea tea CLI username for basic auth
    --ai-token TOKEN        Configure OpenAI API key for AI services (OPENAI_API_KEY)
    --api-gateway           Use API gateway for Gitea/Redmine auth (tokens injected by gateway)
    --ssh-config            Generate SSH config with known hosts for git server
    --client-conf [DIR]     Generate configuration files, optionally in DIR (default: ${HOME}/.config/shoggoth)
    --all                   Configure and print all setup instructions
    --help                  Show this help message
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --domain)
                DOMAIN="$2"
                shift 2
                ;;
            --host-ip)
                HOST_IP="$2"
                shift 2
                ;;
            --docker)
                CONFIGURE_DOCKER="true"
                if [ -n "${2:-}" ]; then
                    case "${2}" in
                        --*) ;;
                        *) DOCKER_PROXY_PORT="$2"; shift ;;
                    esac
                fi
                shift
                ;;
            --update-hosts)
                CONFIGURE_HOSTS="true"
                shift
                ;;
            --apt-cache)
                CONFIGURE_APT_CACHE="true"
                shift
                ;;
            --gitea-user)
                CONFIGURE_GITEA_USER="$2"
                shift 2
                ;;
            --ai-token)
                CONFIGURE_AI_TOKEN="$2"
                shift 2
                ;;
            --api-gateway)
                CONFIGURE_API_GATEWAY="true"
                shift
                ;;
            --ssh-config)
                CONFIGURE_SSH_CONFIG="true"
                shift
                ;;
            --client-conf)
                CONFIGURE_CLIENT_CONF="true"
                if [ -n "${2:-}" ]; then
                    case "${2}" in
                        --*) ;;
                        *) CLIENT_CONF_DIR="$2"; shift ;;
                    esac
                fi
                shift
                ;;
            --all)
                CONFIGURE_ALL="true"
                shift
                ;;
            --help)
                usage
                exit 0
                ;;
            *)
                echo "Error: Unknown option '$1'"
                usage
                exit 1
                ;;
        esac
    done
}

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

install_ca_certificate() {
    local os_id
    os_id=$(detect_os)
    local ca_url="https://${HOST_IP}/ca.crt"

    if [ "${HOST_IP}" = "127.0.0.1" ] && [ "${CONFIGURE_DOCKER}" = "true" ]; then
        echo "Warning: HOST_IP is 127.0.0.1 (default). CA certificate download will fail on remote clients." >&2
        echo "Use --host-ip <SERVER_IP> to specify the server address." >&2
        exit 1
    fi

    case "$os_id" in
        ubuntu|debian)
            curl -sk "${ca_url}" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /usr/share/ca-certificates/docker_registry_proxy.crt"
            echo "docker_registry_proxy.crt" | run_priv_cmd "tee -a /etc/ca-certificates.conf" >/dev/null
            run_priv_cmd "update-ca-certificates --fresh"
            ;;
        centos|rhel|rocky|almalinux|fedora)
            curl -sk "${ca_url}" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /etc/pki/ca-trust/source/anchors/docker_registry_proxy.crt"
            run_priv_cmd "update-ca-trust"
            ;;
        alpine)
            curl -sk "${ca_url}" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /usr/local/share/ca-certificates/docker_registry_proxy.crt"
            run_priv_cmd "update-ca-certificates"
            ;;
        nixos)
            curl -sk "${ca_url}" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /etc/ssl/certs/docker_registry_proxy.crt"
            ;;
        *)
            echo "Warning: Unsupported OS '$os_id'. Please install CA certificate manually."
            echo "Download from: ${ca_url}"
            ;;
    esac
}

configure_docker_proxy() {
    local os_id
    os_id=$(detect_os)

    if [ "$os_id" = "nixos" ]; then
        echo "For NixOS, add the following to your configuration.nix:"
        echo "  virtualisation.docker.daemon.settings = {"
        echo "    \"insecure-registries\" = [\"docker-registry.${DOMAIN}\"];"
        echo "    proxies = {"
        echo "      \"http-proxy\" = \"http://docker-cache.${DOMAIN}:${DOCKER_PROXY_PORT}\";"
        echo "      \"https-proxy\" = \"http://docker-cache.${DOMAIN}:${DOCKER_PROXY_PORT}\";"
        echo "      \"no-proxy\" = \"*.${DOMAIN}\";"
        echo "    };"
        echo "  };"
        echo "Then run: nixos-rebuild switch"
        return
    fi

    local docker_daemon_dir="/etc/docker"
    local docker_daemon_file="${docker_daemon_dir}/daemon.json"

    run_priv_cmd "mkdir -p ${docker_daemon_dir}"

    run_priv_cmd "cat > ${docker_daemon_file} <<EOF
{
  \"insecure-registries\": [\"docker-registry.${DOMAIN}\"],
  \"proxies\": {
    \"http-proxy\": \"${PROXY_URL}\",
    \"https-proxy\": \"${PROXY_URL}\",
    \"no-proxy\": \"*.${DOMAIN}\"
  }
}
EOF"

    echo "Restarting Docker daemon..."
    if command -v systemctl >/dev/null 2>&1; then
        run_priv_cmd "systemctl restart docker"
    elif command -v service >/dev/null 2>&1; then
        run_priv_cmd "service docker restart"
    else
        echo "Please restart Docker daemon manually"
    fi
}

update_hosts() {
    services="kestra dns apt-cache docker-cache litellm git build-cache git-pages redmine python-cache docker-registry grafana otelcol api slave-term"
    hosts_entries="${HOST_IP} ${DOMAIN}
"

    for service in ${services}; do
        hosts_entries="${hosts_entries}${HOST_IP} ${service}.${DOMAIN}
"
    done

    run_priv_cmd "sed -i '/${DOMAIN}/d' /etc/hosts && cat >> /etc/hosts <<EOF
${hosts_entries}EOF"

    echo "Updated /etc/hosts with entries for all services:"
    echo "${hosts_entries}"
}

generate_apt_cache_conf() {
    cat <<EOF
Acquire::http::Proxy "http://apt-cache.${DOMAIN}:3142";
Acquire::https::Proxy "false";
EOF
}

configure_apt_cache() {
    local apt_config_file="/etc/apt/apt.conf.d/01-shoggoth-apt-cache"

    generate_apt_cache_conf | run_priv_cmd "cat > ${apt_config_file}"
    run_priv_cmd "chmod 644 ${apt_config_file}"

    echo "Generated ${apt_config_file}"
    echo ""
    echo "Verifying apt configuration:"
    apt-config dump | grep -i proxy || true
    echo ""
    echo "Test with: sudo apt update"
}

generate_shoggoth_conf() {
    mkdir -p "${CLIENT_CONF_DIR}"
    chmod 700 "${CLIENT_CONF_DIR}"

    cat > "${ENV_FILE}" <<EOF
# Shoggoth environment variables
# Load with: set -a; source ${ENV_FILE}; set +a
# See: https://gist.github.com/mihow/9c7f559807069a03e302605691f85572

# LLM (LiteLLM → LocalAI)
OPENAI_API_KEY=${CONFIGURE_AI_TOKEN}
OPENAI_BASE_URL=http://litellm.${DOMAIN}/v1/
OPENAI_MODEL=shoggoth-default
#qwen3-coder-next:cloud, qwen3-coder:30b, qwen3-coder:480b-cloud
BM_MCP_URL=http://litellm.${DOMAIN}/mcp

# Build cache (ccache)
CCACHE_REMOTE_STORAGE=http://build-cache.${DOMAIN}
CCACHE_REMOTE_ONLY=true

# Python cache (PyPI caching proxy)
PIP_INDEX_URL=http://python-cache.${DOMAIN}/index/
PIP_TRUSTED_HOST=python-cache.${DOMAIN}

# Shoggoth
SHOGGOTH_DOMAIN=${DOMAIN}

# Kestra
KESTRA_HOST=kestra.${DOMAIN}
EOF
    chmod 600 "${ENV_FILE}"

    echo "Generated ${ENV_FILE}"
    echo "Add the following to your ~/.bashrc or ~/.zshrc:"
    echo "  set -a; source ${ENV_FILE}; set +a"
    echo "See: https://gist.github.com/mihow/9c7f559807069a03e302605691f85572"
}

generate_gitea_config() {
    cat >> "${ENV_FILE}" <<EOF

# Gitea tea CLI
GITEA_SERVER_URL=http://api.${DOMAIN}/gitea
GITEA_SERVER_TOKEN=gateway
GITEA_INSTANCE_SSH_HOST=git.${DOMAIN}
EOF
    chmod 600 "${ENV_FILE}"
}

generate_redmine_config() {
    cat >> "${ENV_FILE}" <<EOF

# Redmine CLI
REDMINE_SERVER=http://api.${DOMAIN}/redmine
REDMINE_AUTH_METHOD=apikey
REDMINE_API_KEY=gateway
REDMINE_NO_UPDATE_CHECK=1
EOF
    chmod 600 "${ENV_FILE}"
}

generate_gitea_cli_conf() {
    local tea_config_file="${CLIENT_CONF_DIR}/tea-config.yml"

    if [ -n "${CONFIGURE_GITEA_USER}" ]; then
        cat > "${tea_config_file}" <<EOF
logins:
  - name: shoggoth
    url: http://api.${DOMAIN}/gitea
    ssh_host: git.${DOMAIN}
    token: gateway
    user: ${CONFIGURE_GITEA_USER}
    default: true
    version_check: false
EOF
    else
        cat > "${tea_config_file}" <<EOF
logins:
  - name: shoggoth
    url: http://api.${DOMAIN}/gitea
    ssh_host: git.${DOMAIN}
    token: gateway
    default: true
    version_check: false
EOF
    fi

    chmod 600 "${tea_config_file}"
    echo "Generated ${tea_config_file}"
}

generate_redmine_cli_conf() {
    local redmine_config_file="${CLIENT_CONF_DIR}/redmine-config.yml"

    cat > "${redmine_config_file}" <<EOF
server: http://api.${DOMAIN}/redmine
auth_method: apikey
api_key: gateway
no_color: true
EOF

    chmod 600 "${redmine_config_file}"
    echo "Generated ${redmine_config_file}"
}

generate_client_conf() {
    mkdir -p "${CLIENT_CONF_DIR}"
    chmod 700 "${CLIENT_CONF_DIR}"

    generate_apt_cache_conf > "${CLIENT_CONF_DIR}/apt-cache.conf"
    chmod 600 "${CLIENT_CONF_DIR}/apt-cache.conf"

    echo "Generated ${CLIENT_CONF_DIR}/apt-cache.conf"
}

generate_ssh_config() {
    mkdir -p "${CLIENT_CONF_DIR}"

    local KNOWN_HOSTS_FILE="${CLIENT_CONF_DIR}/known_hosts"
    local GIT_HOST="git.${DOMAIN}"

    local SSH_KEYSCAN_OUTPUT
    SSH_KEYSCAN_OUTPUT="$(ssh-keyscan "${GIT_HOST}" 2>/dev/null)" || {
        echo "Warning: ssh-keyscan for ${GIT_HOST}:22 failed" >&2
        echo "The git server may not be reachable. SSH host key verification will fail." >&2
        return 1
    }
    printf '%s\n' "${SSH_KEYSCAN_OUTPUT}" > "${KNOWN_HOSTS_FILE}"

    chmod 600 "${KNOWN_HOSTS_FILE}"

    local SSH_CONFIG_FILE="${CLIENT_CONF_DIR}/ssh_config"
    cat > "${SSH_CONFIG_FILE}" <<EOF
Host ${GIT_HOST}
    UserKnownHostsFile ${KNOWN_HOSTS_FILE}
EOF
    chmod 600 "${SSH_CONFIG_FILE}"

    echo "Add the following to your ~/.ssh/config:"
    echo "  Include ${SSH_CONFIG_FILE}"
    echo ""
    echo "Generated SSH config for ${GIT_HOST} with known hosts in ${CLIENT_CONF_DIR}"
}

generate_qwen_settings() {
    cat > "${CLIENT_CONF_DIR}/qwen-settings.json" <<EOF
{
  "telemetry": {
    "enabled": true,
    "target": "local",
    "otlpEndpoint": "http://otelcol.${DOMAIN}:4317",
    "otlpProtocol": "grpc",
    "logPrompts": false,
    "includeSensitiveSpanAttributes": false
  }
}
EOF
    chmod 600 "${CLIENT_CONF_DIR}/qwen-settings.json"
    echo "Generated ${CLIENT_CONF_DIR}/qwen-settings.json"
    echo "MCP server configuration is provided by the shoggoth Qwen Code plugin"
    echo "Install with: qwen extensions install http://${DOMAIN}/plugin --consent"
    echo "Telemetry exports to Grafana (LGTM stack) via otelcol at http://otelcol.${DOMAIN}:4317"
    echo "Dashboard: http://grafana.${DOMAIN} (admin)"
}

main() {
    parse_args "$@"

    ENV_FILE="${CLIENT_CONF_DIR}/env"
    PROXY_URL="http://docker-cache.${DOMAIN}:${DOCKER_PROXY_PORT}"

    if [ $# -eq 0 ]; then
        usage
        exit 0
    fi

    if [ "${CONFIGURE_ALL}" = "true" ]; then
        CONFIGURE_DOCKER="true"
        CONFIGURE_HOSTS="true"
        CONFIGURE_APT_CACHE="true"
        CONFIGURE_CLIENT_CONF="true"
        CONFIGURE_API_GATEWAY="true"
        CONFIGURE_SSH_CONFIG="true"
    fi

    get_priv_cmd

    if [ -n "${CONFIGURE_CLIENT_CONF}" ]; then
        if [ "${CONFIGURE_CLIENT_CONF}" != "true" ]; then
            CLIENT_CONF_DIR="${CONFIGURE_CLIENT_CONF}"
            ENV_FILE="${CLIENT_CONF_DIR}/env"
        fi
        echo ""
        generate_shoggoth_conf
        generate_client_conf
    fi

    if [ -n "${CONFIGURE_DOCKER}" ]; then
        case "${CONFIGURE_DOCKER}" in
            ''|*[!0-9]*) ;;
            *) DOCKER_PROXY_PORT="${CONFIGURE_DOCKER}"; PROXY_URL="http://docker-cache.${DOMAIN}:${DOCKER_PROXY_PORT}" ;;
        esac
        echo "Setting up Docker client to use shoggoth proxy at ${PROXY_URL}"

        configure_docker_proxy
        install_ca_certificate

        echo "Docker proxy setup complete. Test with: docker pull nginx:latest"
    fi

    if [ "${CONFIGURE_HOSTS}" = "true" ]; then
        update_hosts
        echo "Hosts file update complete."
    fi

    if [ "${CONFIGURE_APT_CACHE}" = "true" ]; then
        echo "Setting up apt cache at http://apt-cache.${DOMAIN}/"
        configure_apt_cache
        echo "Apt cache setup complete."
    fi

    if [ "${CONFIGURE_API_GATEWAY}" = "true" ] && [ -n "${CONFIGURE_CLIENT_CONF}" ]; then
        generate_gitea_config
        generate_gitea_cli_conf
        echo "Gitea tea CLI configured via environment variables (GITEA_SERVER_URL, GITEA_SERVER_TOKEN) and config file (${CLIENT_CONF_DIR}/tea-config.yml)"
        generate_redmine_config
        generate_redmine_cli_conf
        echo "Redmine CLI configured via environment variables (REDMINE_SERVER, REDMINE_AUTH_METHOD, REDMINE_API_KEY)"
    fi

    if [ -n "${CONFIGURE_CLIENT_CONF}" ]; then
        generate_qwen_settings
    fi

    if [ "${CONFIGURE_SSH_CONFIG}" = "true" ]; then
        generate_ssh_config
    fi
}

main "$@"
