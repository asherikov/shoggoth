#!/usr/bin/env bash
set -o pipefail
set -e

DOCKER_PROXY_PORT="${DOCKER_PROXY_PORT:-3128}"
CONFIGURE_DOCKER="${CONFIGURE_DOCKER:-}"
CONFIGURE_HOSTS="${CONFIGURE_HOSTS:-}"
CONFIGURE_BUILD_CACHE="${CONFIGURE_BUILD_CACHE:-}"
CONFIGURE_ALL="${CONFIGURE_ALL:-}"
CONFIGURE_APT="${CONFIGURE_APT:-}"
CONFIGURE_OLLAMA="${CONFIGURE_OLLAMA:-}"
CONFIGURE_GITEA_MCP="${CONFIGURE_GITEA_MCP:-}"
CONFIGURE_REDMINE_MCP="${CONFIGURE_REDMINE_MCP:-}"
CONFIGURE_PROXPI="${CONFIGURE_PROXPI:-}"
HOST="${HOST:-shoggoth.local}"
HOST_IP="${HOST_IP:-127.0.0.1}"

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

Set up Docker client to use shoggoth proxy and generate ~/.shoggothrc file.

Options:
    -h, --host HOST         Hostname for /etc/hosts entries (default: shoggoth.local)
    --host-ip IP            IP address for /etc/hosts entries (default: 127.0.0.1)
    -p, --port PORT         Proxy port (default: 3128, or \$DOCKER_PROXY_PORT)
    --docker-proxy          Configure Docker proxy (default if no action specified)
    --update-hosts          Update /etc/hosts to map all service hostnames to IP
    --build-cache           Print build cache with nginx proxy setup instructions
    --apt-proxy             Configure apt proxy for package caching
    --ollama                Configure ollama client environment
    --proxpi                Configure proxpi (PyPI caching proxy) client environment
    --gitea-mcp TOKEN       Generate qwen-code MCP server configuration example with given token
    --redmine-mcp TOKEN     Generate Redmine MCP server configuration example with given token
    --all                   Configure and print all setup instructions
    --help                  Show this help message

The script generates \${HOME}/.shoggothrc file with environment variables.
Source this file in your .bashrc or .zshrc:
    source ~/.shoggothrc

Environment variables:
    DOCKER_PROXY_PORT       Proxy port (default: 3128)
    HOST                    Hostname for /etc/hosts (default: shoggoth.local)
    HOST_IP                 IP address for /etc/hosts (default: 127.0.0.1)
    CONFIGURE_DOCKER        Set to "true" to configure Docker proxy
    CONFIGURE_HOSTS         Set to "true" to update /etc/hosts
    CONFIGURE_BUILD_CACHE   Set to "true" to print build cache setup instructions
    CONFIGURE_ALL           Set to "true" to configure all options
    CONFIGURE_APT           Set to "true" to configure apt proxy
    CONFIGURE_OLLAMA        Set to "true" to configure ollama client
    CONFIGURE_GITEA_MCP     Set to authorization token to generate qwen-code MCP configuration
    CONFIGURE_REDMINE_MCP   Set to authorization token to generate qwen-code MCP configuration
    CONFIGURE_PROXPI        Set to "true" to configure proxpi client

Examples:
    ./setup-client.sh --docker-proxy --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --update-hosts --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --docker-proxy --update-hosts --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --apt-proxy --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --ollama --host shoggoth.local --host-ip 192.168.1.100
    ./setup-client.sh --host shoggoth.local --gitea-mcp your-mcp-token
    ./setup-client.sh --host shoggoth.local --redmine-mcp your-mcp-token
    ./setup-client.sh --build-cache
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
            -p|--port)
                DOCKER_PROXY_PORT="$2"
                shift 2
                ;;
            --docker-proxy)
                CONFIGURE_DOCKER="true"
                shift
                ;;
            --update-hosts)
                CONFIGURE_HOSTS="true"
                shift
                ;;
            --build-cache)
                CONFIGURE_BUILD_CACHE="true"
                shift
                ;;
            --apt-proxy)
                CONFIGURE_APT="true"
                shift
                ;;
            --ollama)
                CONFIGURE_OLLAMA="true"
                shift
                ;;
            --proxpi)
                CONFIGURE_PROXPI="true"
                shift
                ;;
            --gitea-mcp)
                CONFIGURE_GITEA_MCP="$2"
                shift 2
                ;;
            --redmine-mcp)
                CONFIGURE_REDMINE_MCP="$2"
                shift 2
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

configure_apt_proxy() {
    local apt_proxy_url="http://apt-cache.${HOST}:3142/"
    local apt_config_file="/etc/apt/apt.conf.d/01-shoggoth-apt-proxy"

    run_priv_cmd "cat > ${apt_config_file} <<EOF
Acquire::http::Proxy \"${apt_proxy_url}\";
Acquire::https::Proxy \"false\";
EOF"

    echo "Created ${apt_config_file} with proxy ${apt_proxy_url}"
    echo ""
    echo "Verifying apt configuration:"
    apt-config dump | grep -i proxy || true
    echo ""
    echo "Test with: sudo apt update"
}

generate_shoggothrc() {
    local shoggothrc_file="${HOME}/.shoggothrc"
    local ollama_url="http://ollama.${HOST}"
    local build_cache_url="http://build-cache.${HOST}"
    local proxpi_url="http://proxpi.${HOST}"

    cat > "${shoggothrc_file}" <<EOF
# Shoggoth environment variables
# Source this file in your .bashrc or .zshrc
# source ~/.shoggothrc

# Ollama
export OLLAMA_HOST="${ollama_url}"
export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL="https://api-ollama.arrc.tii.ae/v1/"
export OPENAI_MODEL="qwen3-coder:30b"

# Build cache (ccache)
export CCACHE_REMOTE_STORAGE="${build_cache_url}"
export CCACHE_REMOTE_ONLY="true"

# Proxpi (PyPI caching proxy)
export PIP_INDEX_URL="${proxpi_url}/index/"
export PIP_TRUSTED_HOST="proxpi.${HOST}"
EOF

    echo "Generated ${shoggothrc_file}"
    echo "Add the following to your ~/.bashrc or ~/.zshrc:"
    echo "  source ~/.shoggothrc"
}

generate_gitea_mcp_config() {
    local mcp_server_url="http://gitea-mcp.${HOST}/mcp"

    cat <<EOF
{
  "mcpServers": {
    "shoggoth-gitea-mcp": {
      "httpUrl": "${mcp_server_url}",
      "headers": {
        "Authorization": "Bearer ${CONFIGURE_GITEA_MCP}"
      },
      "timeout": 5000
    }
  }
}
EOF
}

generate_redmine_mcp_config() {
    local redmine_mcp_server_url="http://redmine-mcp.${HOST}/sse"

    cat <<EOF
{
  "mcpServers": {
    "shoggoth-redmine-mcp": {
      "url": "${redmine_mcp_server_url}",
      "headers": {
        "X-Redmine-API-Key": "${CONFIGURE_REDMINE_MCP}"
      },
      "timeout": 5000
    }
  }
}
EOF
}

main() {
    parse_args "$@"

    PROXY_URL="http://${HOST}:${DOCKER_PROXY_PORT}"

    if [ $# -eq 0 ]; then
        usage
        exit 0
    fi

    if [ "${CONFIGURE_ALL}" = "true" ]; then
        CONFIGURE_DOCKER="true"
        CONFIGURE_HOSTS="true"
        CONFIGURE_BUILD_CACHE="true"
        CONFIGURE_APT="true"
        CONFIGURE_OLLAMA="true"
        CONFIGURE_PROXPI="true"
    fi

    get_priv_cmd

    if [ "${CONFIGURE_DOCKER}" = "true" ]; then
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

    if [ "${CONFIGURE_APT}" = "true" ]; then
        echo "Setting up apt proxy at http://apt-cache.${HOST}/"
        configure_apt_proxy
        echo "Apt proxy setup complete."
    fi

    if [ "${CONFIGURE_OLLAMA}" = "true" ] || [ "${CONFIGURE_BUILD_CACHE}" = "true" ] || [ "${CONFIGURE_PROXPI}" = "true" ]; then
        echo ""
        generate_shoggothrc
    fi

    if [ -n "${CONFIGURE_GITEA_MCP}" ]; then
        echo ""
        generate_gitea_mcp_config
    fi

    if [ -n "${CONFIGURE_REDMINE_MCP}" ]; then
        echo ""
        generate_redmine_mcp_config
    fi
}

main "$@"
