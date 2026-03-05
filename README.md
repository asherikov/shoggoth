# Shoggoth

A self-hosted development service multitool intended for personal use.


## Features

- Caching:
    - Debian/Ubuntu package caching proxy (`apt-cacher-ng`).
    - Docker registry caching proxy.
    - Build cache server, to be used with ccache or sccache.
- Development:
    - Local AI model server (`ollama`).
    - Git server (`gitea`) with CI/CD actions support.
    - Gitea MCP server for AI coding agent integration.


## Architecture

The system consists of three parts:

- A set of services managed using docker compose, located in `shoggoth`
  subfolder.
- A set of utilities for (remote) control over docker compose, refer to
  `Makefile` for more information.
- A setup script for configuration of service clients.

The main use-case is to run the services on a dedicated headless server and use
or control them from multiple client computers.


## Services

The following services are available:

| Service | Hostname | Description |
|---------|----------|-------------|
| `apt-cache` | `apt-cache.<host>` | APT package caching proxy |
| `docker-cache` | `<host>` | Docker registry caching proxy |
| `dns` | `<host>` | Unbound DNS resolver |
| `web` | `<host>` | Welcome home page and angie reverse proxy |
| `web` | `build-cache.<host>` | Build cache storage |
| `ollama` | `ollama.<host>` | Local AI model server |
| `git` | `git.<host>` | Gitea Git server with web UI |
| `gitea-runner` | - | Gitea Actions runner |
| `gitea-mcp` | `gitea-mcp.<host>` | Gitea MCP server for AI agents |


<img src="https://raw.githubusercontent.com/asherikov/shoggoth/refs/heads/main/docs/architecture.svg" alt="architecture" />



## Domain Name Resolution

Domain names are used to access services via the angie reverse proxy server.
Two resolution methods are supported:

### Hosts File Resolution

Add service hostnames to `/etc/hosts` on each client machine:

```bash
# Using the setup script
./shoggoth/setup-client.sh --update-hosts --host shoggoth.local --host-ip 192.168.1.100

# Or manually add to /etc/hosts:
192.168.1.100 shoggoth.local
192.168.1.100 <service>.shoggoth.local
```

### DNS Resolution

The `dns` service (Unbound) can be configured as the DNS server on client machines.
It resolves all service hostnames automatically. Configure your network settings
to use the shoggoth server IP as the DNS server.


# Client Configuration

Run the setup script on each client machine:

```bash
# Configure apt proxy
./shoggoth/setup-client.sh --configure-apt --host shoggoth.local --host-ip 192.168.1.100

# Configure Docker proxy
./shoggoth/setup-client.sh --docker-proxy --host shoggoth.local --host-ip 192.168.1.100

# Configure both apt and Docker proxy
./shoggoth/setup-client.sh --configure-apt --docker-proxy --host shoggoth.local --host-ip 192.168.1.100

# Configure all services (apt, Docker, hosts, build cache)
./shoggoth/setup-client.sh --all --host shoggoth.local --host-ip 192.168.1.100

# Using environment variables
HOST=shoggoth.local HOST_IP=192.168.1.100 CONFIGURE_APT=true ./shoggoth/setup-client.sh
```

The script generates `${HOME}/.shoggothrc` file with environment variables for all services.
Source this file in your `~/.bashrc` or `~/.zshrc`:

```bash
echo "source ~/.shoggothrc" >> ~/.bashrc
source ~/.bashrc
```

## Service Usage Examples

### APT Proxy

After configuration, APT requests are automatically cached:

```bash
sudo apt update
sudo apt install <package>

# View cache statistics
firefox http://apt-cache.shoggoth.local/acng-report.html
```

### Docker Registry Proxy

Docker pulls are cached after initial configuration:

```bash
docker pull nginx:latest
docker pull ubuntu:24.04
```

### Build Cache (ccache/sccache)

Source the `.shoggothrc` file and build with ccache:

```bash
source ~/.shoggothrc
export CCACHE_REMOTE_STORAGE="http://build-cache.shoggoth.local"
export CCACHE_REMOTE_ONLY=true
```

### Ollama AI Server

Query the local AI model:

```bash
source ~/.shoggothrc
curl http://ollama.shoggoth.local/api/tags
curl http://ollama.shoggoth.local/v1/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: ollama" \
    -d '{"model": "qwen3-coder:30b", "prompt": "What is the capital of UAE?"}'
```

### Gitea Git Server

Clone repositories via SSH or HTTP, note that port 3022 is used to avoid
conflicts with ssh server running on the host machine:

```bash
# SSH (configure SSH key in Gitea first)
git clone ssh://git@git.shoggoth.local:3022/admin/repo.git

# HTTP
git clone http://git.shoggoth.local/admin/repo.git
```

### Gitea MCP Server (AI Agent Integration)

Configure your AI coding agent (e.g., Qwen Code) with the MCP server:

```bash
# Generate MCP configuration
./shoggoth/setup-client.sh --mcp --host shoggoth.local --mcp-token your-api-token
```

## Server Management

Use the Makefile targets for server management:

```bash
# Start all services
make up

# Stop all services
make down

# View logs
make log SERVICE=ollama

# SSH to server
make ssh

# Sync changes and restart
make sync_restart
```

