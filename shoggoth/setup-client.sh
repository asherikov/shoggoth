#!/usr/bin/env bash
set -o pipefail
set -e

DOCKER_PROXY_PORT="${DOCKER_PROXY_PORT:-3128}"
CONFIGURE_DOCKER="${CONFIGURE_DOCKER:-}"
CONFIGURE_HOSTS="${CONFIGURE_HOSTS:-}"
CONFIGURE_ALL="${CONFIGURE_ALL:-}"
CONFIGURE_APT_CACHE="${CONFIGURE_APT_CACHE:-}"
CONFIGURE_CLIENT_CONF="${CONFIGURE_CLIENT_CONF:-}"
CONFIGURE_GITEA="${CONFIGURE_GITEA:-}"
CONFIGURE_REDMINE="${CONFIGURE_REDMINE:-}"
HOST="${HOST:-shoggoth.local}"
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
        $PRIV_CMD bash -c "$*"
    fi
}

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Set up Docker client to use shoggoth proxy and generate configuration files.

Options:
    -h, --host HOST         Hostname for /etc/hosts entries (default: shoggoth.local)
    --host-ip IP            IP address for /etc/hosts entries (default: 127.0.0.1)
    --docker [PORT]         Configure Docker proxy, optionally with a port (default: 3128)
    --update-hosts          Append generated hosts file to /etc/hosts
    --apt-cache             Install apt cache config to system apt config
    --gitea-token TOKEN     Configure gitea tea CLI auth and MCP server config
    --redmine-token TOKEN   Configure redmine CLI auth and MCP server config
    --client-conf [DIR]     Generate configuration files, optionally in DIR (default: ${HOME}/.config/shoggoth)
    --all                   Configure and print all setup instructions
    --help                  Show this help message

The script generates an env file with environment variables (KEY=VALUE format)
in the config directory. To load it in your shell, add to your ~/.bashrc or ~/.zshrc:
    set -a; source ~/.config/shoggoth/env; set +a
See: https://gist.github.com/mihow/9c7f559807069a03e302605691f85572

Environment variables:
    DOCKER_PROXY_PORT       Proxy port (default: 3128)
    HOST                    Hostname for /etc/hosts (default: shoggoth.local)
    HOST_IP                 IP address for /etc/hosts (default: 127.0.0.1)
    CONFIGURE_DOCKER        Set to "true" or a port number to configure Docker proxy
    CONFIGURE_HOSTS         Set to "true" to update /etc/hosts
    CONFIGURE_ALL           Set to "true" to configure all options
    CONFIGURE_APT_CACHE     Set to "true" to install apt cache config
    CONFIGURE_CLIENT_CONF   Set to "true" or a directory path to generate client config files
    CONFIGURE_GITEA         Set to token for gitea tea CLI and MCP server config
    CONFIGURE_REDMINE       Set to API key for redmine CLI and MCP server config
    CLIENT_CONF_DIR         Directory for config files (default: ${HOME}/.config/shoggoth)

Examples:
    ./setup-client.sh --docker --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --docker 8080 --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --update-hosts --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --docker --update-hosts --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --client-conf --host shoggoth.local
    ./setup-client.sh --client-conf /path/to/dir --host shoggoth.local
    ./setup-client.sh --client-conf --gitea-token your-token --host shoggoth.local
    ./setup-client.sh --client-conf --apt-cache --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --host shoggoth.local --redmine-token your-token
    ./setup-client.sh --all
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -h|--host)
                HOST="$2"
                shift 2
                ;;
            --host-ip)
                HOST_IP="$2"
                shift 2
                ;;
            --docker)
                CONFIGURE_DOCKER="true"
                if [ -n "${2:-}" ] && [[ ! "${2:-}" == --* ]]; then
                    DOCKER_PROXY_PORT="$2"
                    shift 2
                else
                    shift
                fi
                ;;
            --update-hosts)
                CONFIGURE_HOSTS="true"
                shift
                ;;
            --apt-cache)
                CONFIGURE_APT_CACHE="true"
                shift
                ;;
            --gitea-token)
                CONFIGURE_GITEA="$2"
                shift 2
                ;;
            --redmine-token)
                CONFIGURE_REDMINE="$2"
                shift 2
                ;;
            --client-conf)
                CONFIGURE_CLIENT_CONF="true"
                if [ -n "${2:-}" ] && [[ ! "${2:-}" == --* ]]; then
                    CLIENT_CONF_DIR="$2"
                    shift 2
                else
                    shift
                fi
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

    case "$os_id" in
        ubuntu|debian)
            curl -s "${PROXY_URL}/ca.crt" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /usr/share/ca-certificates/docker_registry_proxy.crt"
            echo "docker_registry_proxy.crt" | run_priv_cmd "tee -a /etc/ca-certificates.conf" >/dev/null
            run_priv_cmd "update-ca-certificates --fresh"
            ;;
        centos|rhel|rocky|almalinux|fedora)
            curl -s "${PROXY_URL}/ca.crt" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /etc/pki/ca-trust/source/anchors/docker_registry_proxy.crt"
            run_priv_cmd "update-ca-trust"
            ;;
        alpine)
            curl -s "${PROXY_URL}/ca.crt" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /usr/local/share/ca-certificates/docker_registry_proxy.crt"
            run_priv_cmd "update-ca-certificates"
            ;;
        nixos)
            curl -s "${PROXY_URL}/ca.crt" -o /tmp/docker_registry_proxy.crt
            run_priv_cmd "cp /tmp/docker_registry_proxy.crt /etc/ssl/certs/docker_registry_proxy.crt"
            ;;
        *)
            echo "Warning: Unsupported OS '$os_id'. Please install CA certificate manually."
            echo "Download from: ${PROXY_URL}/ca.crt"
            ;;
    esac
}

configure_docker_proxy() {
    local os_id
    os_id=$(detect_os)

    if [ "$os_id" = "nixos" ]; then
        echo "For NixOS, add the following to your configuration.nix (TODO this wont work):"
        echo "  virtualisation.docker.daemon.settings = {"
        echo "    \"insecure-registries\" = [\"docker-registry.${HOST}\"];"
        echo "    proxies = {"
        echo "      \"http-proxy\" = \"${PROXY_URL}\";"
        echo "      \"https-proxy\" = \"${PROXY_URL}\";"
        echo "      \"no-proxy\" = \"*.${HOST}\";"
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
  \"insecure-registries\": [\"docker-registry.${HOST}\"],
  \"proxies\": {
    \"http-proxy\": \"${PROXY_URL}\",
    \"https-proxy\": \"${PROXY_URL}\",
    \"no-proxy\": \"*.${HOST}\"
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
    local services=("dns" "apt-cache" "docker-cache" "ollama" "git" "build-cache" "gitea-mcp" "git-pages" "redmine" "redmine-mcp" "proxpi" "docker-registry")
    local hosts_entries="${HOST_IP} ${HOST}"$'\n'

    for service in "${services[@]}"; do
        hosts_entries="${hosts_entries}${HOST_IP} ${service}.${HOST}"$'\n'
    done

    run_priv_cmd "sed -i '/${HOST}/d' /etc/hosts && cat >> /etc/hosts <<EOF
${hosts_entries}EOF"

    echo "Updated /etc/hosts with entries for all services:"
    echo "${hosts_entries}"
}

generate_apt_cache_conf() {
    cat <<EOF
Acquire::http::Proxy "http://apt-cache.${HOST}:3142";
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

# Ollama
OPENAI_API_KEY=ollama
OPENAI_BASE_URL="http://ollama.${HOST}/v1/"
OPENAI_MODEL="qwen3-coder:30b"

# Build cache (ccache)
CCACHE_REMOTE_STORAGE="http://build-cache.${HOST}"
CCACHE_REMOTE_ONLY="true"

# Proxpi (PyPI caching proxy)
PIP_INDEX_URL="http://proxpi.${HOST}/index/"
PIP_TRUSTED_HOST="proxpi.${HOST}"
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
GITEA_SERVER_URL=http://git.${HOST}
GITEA_SERVER_TOKEN=${CONFIGURE_GITEA}
GITEA_INSTANCE_SSH_HOST=git.${HOST}:3022
EOF
    chmod 600 "${ENV_FILE}"
}

generate_redmine_config() {
    cat >> "${ENV_FILE}" <<EOF

# Redmine CLI
REDMINE_SERVER=http://redmine.${HOST}
REDMINE_AUTH_METHOD=apikey
REDMINE_API_KEY=${CONFIGURE_REDMINE}
EOF
    chmod 600 "${ENV_FILE}"
}

generate_client_conf() {
    mkdir -p "${CLIENT_CONF_DIR}"
    chmod 700 "${CLIENT_CONF_DIR}"

    generate_apt_cache_conf > "${CLIENT_CONF_DIR}/apt-cache.conf"
    chmod 600 "${CLIENT_CONF_DIR}/apt-cache.conf"

    local dns_ip
    dns_ip=$(getent hosts dns.${HOST} | cut -f 1 -d ' ')

    cat > "${CLIENT_CONF_DIR}/resolv.conf" <<EOF
nameserver ${dns_ip}
search ${HOST}
EOF
    chmod 600 "${CLIENT_CONF_DIR}/resolv.conf"

    echo "Generated ${CLIENT_CONF_DIR}/apt-cache.conf"
    echo "Generated ${CLIENT_CONF_DIR}/resolv.conf"
}

generate_qwen_conf() {
    local mcp_servers=""

    if [ -n "${CONFIGURE_GITEA}" ]; then
        mcp_servers="${mcp_servers}
    \"shoggoth-gitea-mcp\": {
      \"httpUrl\": \"http://gitea-mcp.${HOST}/mcp\",
      \"headers\": {
        \"Authorization\": \"Bearer ${CONFIGURE_GITEA}\"
      },
      \"timeout\": 5000
    }"
    fi

    if [ -n "${CONFIGURE_REDMINE}" ]; then
        if [ -n "${mcp_servers}" ]; then
            mcp_servers="${mcp_servers},"
        fi
        mcp_servers="${mcp_servers}
    \"shoggoth-redmine-mcp\": {
      \"url\": \"http://redmine-mcp.${HOST}/sse\",
      \"headers\": {
        \"X-Redmine-API-Key\": \"${CONFIGURE_REDMINE}\"
      },
      \"timeout\": 5000
    }"
    fi

    if [ -n "${mcp_servers}" ]; then
        cat > "${CLIENT_CONF_DIR}/qwen.json" <<EOF
{
  "mcpServers": {${mcp_servers}
  }
}
EOF
        chmod 600 "${CLIENT_CONF_DIR}/qwen.json"
        echo "Generated ${CLIENT_CONF_DIR}/qwen.json"
    fi
}

main() {
    parse_args "$@"

    ENV_FILE="${CLIENT_CONF_DIR}/env"
    PROXY_URL="http://${HOST}:${DOCKER_PROXY_PORT}"

    if [ $# -eq 0 ]; then
        usage
        exit 0
    fi

    if [ "${CONFIGURE_ALL}" = "true" ]; then
        CONFIGURE_DOCKER="true"
        CONFIGURE_HOSTS="true"
        CONFIGURE_APT_CACHE="true"
        CONFIGURE_CLIENT_CONF="true"
    fi

    get_priv_cmd

    if [ -n "${CONFIGURE_CLIENT_CONF}" ]; then
        if [[ "${CONFIGURE_CLIENT_CONF}" != "true" ]]; then
            CLIENT_CONF_DIR="${CONFIGURE_CLIENT_CONF}"
            ENV_FILE="${CLIENT_CONF_DIR}/env"
        fi
        echo ""
        generate_shoggoth_conf
        generate_client_conf
    fi

    if [ -n "${CONFIGURE_DOCKER}" ]; then
        if [[ "${CONFIGURE_DOCKER}" =~ ^[0-9]+$ ]]; then
            DOCKER_PROXY_PORT="${CONFIGURE_DOCKER}"
            PROXY_URL="http://${HOST}:${DOCKER_PROXY_PORT}"
        fi
        echo "Setting up Docker client to use shoggoth proxy at ${PROXY_URL}"

        configure_docker_proxy
        install_ca_certificate

        if verify_setup; then
            echo "Docker proxy setup complete. Test with: docker pull nginx:latest"
        else
            echo "Docker proxy setup completed with warnings. Please verify proxy connectivity."
        fi
    fi

    if [ "${CONFIGURE_HOSTS}" = "true" ]; then
        update_hosts
        echo "Hosts file update complete."
    fi

    if [ "${CONFIGURE_APT_CACHE}" = "true" ]; then
        echo "Setting up apt cache at http://apt-cache.${HOST}/"
        configure_apt_cache
        echo "Apt cache setup complete."
    fi

    if [ -n "${CONFIGURE_GITEA}" ] && [ -n "${CONFIGURE_CLIENT_CONF}" ]; then
        generate_gitea_config
        echo "Gitea tea CLI configured via environment variables (GITEA_SERVER_URL, GITEA_SERVER_TOKEN)"
    fi

    if [ -n "${CONFIGURE_REDMINE}" ] && [ -n "${CONFIGURE_CLIENT_CONF}" ]; then
        generate_redmine_config
        echo "Redmine CLI configured via environment variables (REDMINE_SERVER, REDMINE_API_KEY)"
    fi

    if [ -n "${CONFIGURE_CLIENT_CONF}" ] && { [ -n "${CONFIGURE_GITEA}" ] || [ -n "${CONFIGURE_REDMINE}" ]; }; then
        generate_qwen_conf
    fi
}

main "$@"
