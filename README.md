- [Introduction](#introduction)
  - [Goals](#goals)
  - [Features](#features)
  - [Disclaimer](#disclaimer)
- [Architecture](#architecture)
  - [Services](#services)
  - [Monitoring](#monitoring)
  - [Shoggoth slave container](#shoggoth-slave-container)
  - [Domain Name Resolution](#domain-name-resolution)
- [Client Configuration](#client-configuration)
  - [Caveats](#caveats)
  - [Service Usage Examples](#service-usage-examples)
  - [Server Management](#server-management)
- [Troubleshooting](#troubleshooting)
- [References](#references)

Introduction
============

`shoggoth` is a self-hosted agentic development multitool intended for personal
use.

Goals
-----

- Build an environment where coding agents work as team members, execute
  assigned tasks, perform and respond to code reviews.

- All components of the system should be open source, self-hostable, and
  replaceable by similar software.

Features
--------

- Caching:
  - Debian/Ubuntu package caching proxy (`apt-cacher-ng`).
  - Docker registry caching proxy.
  - Python package caching proxy (`proxpi`).
  - Build cache server, to be used with ccache or sccache.
- Development:
  - Local AI model server (`ollama`).
  - Docker registry.
  - Git server (`gitea`) with CI/CD actions support.
- Project management
  - Redmine project management server.
- Coding agents:
  - Gitea MCP server for AI coding agent integration.
  - Redmine MCP server for AI agent integration.
- Other:
  - DNS server with blacklisting support.

Disclaimer
----------

- This is an experimental project in an early development stage, do not expect
  it to work out of the box.

- Security is non-existent: it is not a priority atm and shoggoth is supposed to
  be running in a local network only.

- The project is tailored for my primary stack (Ubuntu/C++/python) and working
  style (no IDE, no GUI, everything-as-code).

Architecture
============

The system consists of three parts:

- A set of services managed using docker compose, located in `shoggoth`
  subfolder.
- A set of utilities for (remote) control over docker compose, refer to
  `Makefile` for more information.
- A setup script for configuration of service clients.

The main use-case is to run the services on a dedicated headless server and use
or control them from multiple client computers.

Services
--------

The following services are available:

| Service | Hostname | Description |
|----|----|----|
| `web` | `<host>` | Welcome home page and Angie reverse proxy |
| `web` | `build-cache.<host>` | Build cache storage (ccache/sccache) |
| `dns` | `<host>` | Unbound DNS resolver with blacklisting |
| `apt-cache` | `apt-cache.<host>` | APT package caching proxy |
| `docker-cache` | `<host>:3128` | Docker registry caching proxy |
| `docker-registry` | `docker-registry.<host>` | Private Docker registry |
| `proxpi` | `proxpi.<host>` | Python package caching proxy |
| `ollama`/`litellm` | `ollama.<host>` | LLM proxy (LiteLLM) with MCP gateway |
| `git` | `git.<host>` | Gitea Git server with web UI |
| `git-pages` | `git-pages.<host>` | Git Pages static site hosting |
| `gitea-runner` | — | Gitea Actions runner |
| `mcp-gitea` | — | Gitea MCP server (proxied via LiteLLM) |
| `basic-memory` | — | Basic Memory MCP server (proxied via LiteLLM) |
| `kestra` | `kestra.<host>` | Kestra workflow orchestration |
| `redmine` | `redmine.<host>` | Redmine project management server |
| `grafana` | `grafana.<host>` | Observability dashboard (traces, metrics, logs) |
| `loki` | — | Log aggregation backend |
| `tempo` | — | Trace storage backend |
| `victoria-metrics` | — | Metrics storage backend (Prometheus-compatible) |
| `node-exporter` | — | Host-level metrics (CPU, memory, disk, network) |
| `cadvisor` | — | Container metrics (per-container resource usage) |
| `otelcol` | — | OpenTelemetry Collector (Prometheus scrape → OTLP) |

<img src="https://raw.githubusercontent.com/asherikov/shoggoth/refs/heads/main/docs/architecture.svg" alt="architecture overview" />

Monitoring
----------

Shoggoth uses the [LGTM stack](https://grafana.com/docs/) (Loki + Grafana +
Tempo + VictoriaMetrics) as the central observability backend, with an
[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) pipeline for
telemetry ingestion. Traces, metrics, and logs from services and the host system
are collected and forwarded to the appropriate backends, where they can be
viewed on the Grafana dashboard.

Shoggoth slave container
------------------------

Shoggoth slave container includes three main layers:

- <https://github.com/asherikov/ccws> build environemnt with compilers, static
  analysis tools, documentation and other utilitites, refer to
  <https://github.com/asherikov/ccws/blob/master/ccws/examples/Dockerfile>.
- <https://github.com/QwenLM/qwen-code> terminal coding agent, see
  <https://github.com/asherikov/ccws/blob/master/ccws/examples/Dockerfile.qwen>.
- `shoggoth`-sepecific set iof utilities, such as gitea and redmin cli clients,
  docker file is located in `shoggoth/dockerfiles/slave`.

The slave container is intended to be used in three different ways:

- plain CI/CD;
- non-interactive agentic flows, e.g., coding or reviews;
- interactive development with or without a coding agent.

Domain Name Resolution
----------------------

Domain names are used to access services via the angie reverse proxy server. Two
resolution methods are supported:

### Hosts File Resolution

Add service hostnames to `/etc/hosts` on each client machine:

``` bash
# Using the setup script
./shoggoth/setup-client.sh --update-hosts --host shoggoth.local --host-ip 192.168.1.100

# Or manually add to /etc/hosts:
192.168.1.100 shoggoth.local
192.168.1.100 <service>.shoggoth.local
```

### DNS Resolution

The `dns` service (Unbound) can be configured as the DNS server on client
machines, use the shoggoth server IP as the DNS server.

Client Configuration
====================

Run the setup script on each client machine.

``` bash
# Generate config files in default directory `~/.config/shoggoth/`
./shoggoth/setup-client.sh --client-conf --host shoggoth.local

# Generate config files in a custom directory
./shoggoth/setup-client.sh --client-conf /path/to/dir --host shoggoth.local

# Configure Docker caching proxy (default port 3128) and registry
./shoggoth/setup-client.sh --docker --host shoggoth.local --host-ip 192.168.1.100

# Update /etc/hosts with service hostnames (modifies /etc/hosts directly)
./shoggoth/setup-client.sh --update-hosts --host shoggoth.local --host-ip 192.168.1.100

# Install apt cache config to system apt config (requires --client-conf first)
./shoggoth/setup-client.sh --client-conf --apt-cache --host shoggoth.local --host-ip 192.168.1.100

# Configure Docker, hosts, apt cache, and generate client config files
./shoggoth/setup-client.sh --all --host shoggoth.local --host-ip 192.168.1.100

# Configure with Gitea and Redmine tokens (generates env, qwen configuration)
./shoggoth/setup-client.sh --client-conf --host shoggoth.local --gitea-token your-token --redmine-token your-token
```

The script generates the following files when `--client-conf` is used:

| File | Description |
|----|----|
| `env` | Environment variables for all services (ollama, ccache, proxpi, gitea, redmine) |
| `apt-cache.conf` | APT cache configuration |
| `resolv.conf` | DNS resolver configuration |
| `qwen/settings.json` | Qwen Code configuration |

Source the `env` file in your `~/.bashrc` or `~/.zshrc`:

``` bash
echo 'set -a; source ~/.config/shoggoth/env; set +a' >> ~/.bashrc
source ~/.bashrc
```

Caveats
-------

Neither redmine nor gitea cli clients can be configured exclusively with
environment variables. Moreover gitea cli requires user name to be specified in
addition to a token for smooth operation.

Service Usage Examples
----------------------

### APT Cache

After running `--apt-cache`, APT requests are automatically cached:

``` bash
sudo apt update
sudo apt install <package>

# View cache statistics
firefox http://apt-cache.shoggoth.local/acng-report.html
```

### Docker Registry Proxy

Docker pulls are cached after initial configuration:

``` bash
docker pull nginx:latest
docker pull ubuntu:24.04
```

### Build Cache (ccache/sccache)

Source the `env` file and build with ccache:

``` bash
set -a; source ~/.config/shoggoth/env; set +a
# CCACHE_REMOTE_STORAGE and CCACHE_REMOTE_ONLY are already set
```

### Ollama AI Server

Query the local AI model: `make ollama_tags`, `make ollama_query`.

### Gitea Git Server

Clone repositories via SSH or HTTP, note that port 3022 is used to avoid
conflicts with ssh server running on the host machine:

``` bash
# SSH (configure SSH key in Gitea first)
git clone ssh://git@git.shoggoth.local:3022/admin/repo.git

# HTTP
git clone http://git.shoggoth.local/admin/repo.git
```

Configure the `tea` CLI by providing a token:

``` bash
./shoggoth/setup-client.sh --host shoggoth.local --gitea-token your-token
set -a; source ~/.config/shoggoth/env; set +a
tea issues list
```

### Gitea MCP Server (AI Agent Integration)

The `--gitea-token` flag also generates qwen-code settings with the Gitea MCP
server configuration. Copy the relevant server block into your Qwen Code MCP
settings.

### Redmine Project Management Server

Access the Redmine web interface:

``` bash
# Open Redmine in browser
firefox http://redmine.shoggoth.local

# Default credentials (change after first login)
# Username: admin
# Password: admin
```

Install plugins by cloning them into the `shoggoth/redmine/plugins` directory
and restarting the service.

### Redmine MCP Server (AI Agent Integration)

1.  Log in to Redmine with administrator privileges
2.  Go to “Administration” → “Settings” → “API” tab
3.  Check “Enable REST web service”
4.  Generate “API access key” in personal settings.

``` bash
./shoggoth/setup-client.sh --host shoggoth.local --redmine-token your-token
```

The `--redmine-token` flag configures both the Redmine CLI environment variables
and generates the MCP server entry in `qwen/settings.json`.

### Git Pages (Static Site Hosting)

Push static sites using Gitea Actions, refer to `./examples/git-pages.yml` for
an example.

Server Management
-----------------

Use the Makefile targets for server management:

``` bash
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

Troubleshooting
===============

- `LiteLLM:ERROR: opentelemetry.py - 'list' object has no attribute 'get'`:
  known LiteLLM bug (PR
  [\#26713](https://github.com/BerriAI/litellm/pull/26713)) where the OTel
  callback calls `.get()` on `response_obj` which can be a list instead of a
  dict. Non-fatal — traces still emit, just missing some attributes. Will be
  resolved when the PR merges into a stable release.
- `Tempo: "failed to find segment for index"`: WAL watcher error in
  `metrics_generator` after config changes (e.g. `span_metrics.dimensions`).
  Self-healing — the watcher retries every 5 seconds and recovers once new WAL
  segments are written. If persistent, stop Tempo and delete
  `monitoring/tempo/data/generator/wal/` before restarting.
- cmake builds fail to find packages in Ubuntu due to missing system
  information, e.g., `CMAKE_LIBRARY_ARCHITECTURE`: check that build cache is
  operational.

References
==========

- <https://www.reddit.com/r/selfhosted/>
- <https://github.com/awesome-selfhosted/awesome-selfhosted>
- <https://github.com/awesome-foss/awesome-sysadmin>
- <https://leviwheatcroft.github.io/selfhosted-awesome-unlist/> (not maintained)
- <https://gitea.com/gitea/awesome-gitea>
- <https://github.com/trueforge-org/truecharts>
