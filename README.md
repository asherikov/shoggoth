- [Introduction](#introduction)
  - [Goals](#goals)
  - [Disclaimer](#disclaimer)
  - [Comparison](#comparison)
- [Features](#features)
- [Architecture](#architecture)
  - [Service interaction diagram](#service-interaction-diagram)
  - [Interaction with the client and external
    services](#interaction-with-the-client-and-external-services)
  - [Bringup order](#bringup-order)
  - [Monitoring](#monitoring)
  - [Shoggoth slave container](#shoggoth-slave-container)
  - [Authentication](#authentication)
- [Client Configuration](#client-configuration)
  - [Caveats](#caveats)
  - [Service Usage Examples](#service-usage-examples)
  - [Server Management](#server-management)
- [Troubleshooting](#troubleshooting)
- [References](#references)
  - [Agentic coding](#agentic-coding)

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

Disclaimer
----------

- This is an experimental project in an early development stage, do not expect
  it to work out of the box.

- Security is non-existent: it is currently not a priority and shoggoth is
  supposed to be running in a local network only.

- The project is tailored for my primary stack (Ubuntu/C++/python) and working
  style (no IDE, everything-as-code).

Comparison
----------

### Task management

- `shoggoth` differs from most of kanban tools for agentic coding in its focus
  on integration of 3rd-party components rather than from-scratch development
  and covers wider range of functionality: source code hosting, VPN, caching,
  agent containerization, etc. At the same time agentic workflows currently are
  not as advanced as those provided by other projects:

  - <https://github.com/BloopAI/vibe-kanban> (discontinued) and forks, e.g.,
    <https://github.com/dexloom/vibe-kanban-indie>.
  - <https://github.com/multica-ai/multica>
  - <https://github.com/kdlbs/kandev/>

- `shoggoth` is not intended for local desktop usage like
  <https://github.com/antopolskiy/kanban-md>.

### Agentic workflow engines

- `shoggoth` relies on general purpose workflow engine for automation, which, in
  my opinion provides more flexibility than a purpose built engine, e.g.,
  <https://github.com/AgentWrapper/agent-orchestrator>,
  <https://github.com/coleam00/Archon>,
  <https://github.com/catlog22/maestro-flow>.

Features
========

Shoogoth includes a number of containerized services that are available under
configurable domain, set to `s.local` by default.

- Networking:
  - `wireguard.` — WireGuard VPN server with web management UI
    (<https://github.com/wg-easy/wg-easy>), most services are available only
    through VPN connection
  - `web-external.` — TLS-terminating reverse proxy
    (<https://en.angie.software/angie/>) that provides HTTPS access to the
    WireGuard web UI from outside the VPN, by server IP address
  - `dns.` — Unbound DNS resolver <https://github.com/NLnetLabs/unbound> with
    blacklisting support <https://github.com/iYUYUE/dns-zone-blacklist>.
- Caching:
  - `apt-cache.` — Debian/Ubuntu package caching proxy
    <https://github.com/sameersbn/docker-apt-cacher-ng>.
  - `docker-cache.` — Docker registry caching proxy
    <https://github.com/rpardini/docker-registry-proxy>.
  - `python-cache.` — Python package caching proxy, served by
    <https://en.angie.software/angie/> (proxy_cache to pypi.org).
  - `build-cache.` — build cache server for ccache/sccache, served by
    <https://en.angie.software/angie/>.
- Development:
  - `docker-registry.` — private Docker registry
    <https://github.com/distribution/distribution>.
  - Gitea development suite:
    - `git.` — Gitea Git server <https://about.gitea.com/>.
    - `git-pages.` — Git Pages static site hosting <https://git-pages.org/>.
    - `gitea-runner` — Gitea Actions runner
      <https://docs.gitea.com/next/usage/actions/act-runner>.
  - `kestra.` — Kestra workflow orchestration <https://kestra.io/>.
  - `slave-dind.` – Docker-in-Docker service for CI and workflow executors:
    - `slave-term.` — interactive web terminal:
      <https://github.com/tsl0922/ttyd> + tmux + Qwen Code. Runs inside
      `slave-dind.`
  - `cdash.` — CDash test result dashboard <https://github.com/Kitware/CDash>.
- Project management:
  - `redmine.` — Redmine project management server <https://www.redmine.org/>.
- LLM and coding agents:
  - `litellm.` — LLM proxy (<https://github.com/BerriAI/litellm>) with MCP
    gateway.
    - `localai.` — local LLM model server <https://github.com/mudler/LocalAI>.
    - `mcp-gitea.` – Gitea MCP server <https://gitea.com/gitea/gitea-mcp>.
    - `basic-memory.` — Basic Memory MCP server <https://basicmemory.com>.
- Grafana monitoring suite:
  - `grafana.` — observability dashboard (traces, metrics, logs)
    <https://grafana.com/>.
  - `loki.` — log aggregation backend <https://github.com/grafana/loki>.
  - `tempo.` — trace storage backend <https://github.com/grafana/tempo>.
  - `victoria-metrics.` — metrics storage backend
    <https://github.com/VictoriaMetrics/VictoriaMetrics>.
  - `otelcol.` — OpenTelemetry Collector (hostmetrics + docker_stats → OTLP)
    <https://opentelemetry.io/docs/collector/>.
- User and secret management:
  - `openldap.` — OpenLDAP directory for centralized authentication
    <https://github.com/osixia/docker-openldap>.
  - `phpldapadmin.` — OpenLDAP web interface
    <https://github.com/leenooks/phpLDAPadmin>.
  - `openbao.` — secret management <https://openbao.org/>.
- Web (internal):
  - `<domain>` — static welcome home page, served by `angie`.
  - `api.` — single entry point for other services' API, served by `angie`.

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

Most services are available only when connected to VPN.

Services outside of VPN
-----------------------

<img src="https://raw.githubusercontent.com/asherikov/shoggoth/refs/heads/main/docs/novpn.svg" alt="novpn" />

Service interaction diagram
---------------------------

<img src="https://raw.githubusercontent.com/asherikov/shoggoth/refs/heads/main/docs/architecture.svg" alt="architecture" />

Interaction with the client and external services
-------------------------------------------------

<img src="https://raw.githubusercontent.com/asherikov/shoggoth/refs/heads/main/docs/external.svg" alt="external" />

Bringup order
-------------

<img src="https://raw.githubusercontent.com/asherikov/shoggoth/refs/heads/main/docs/bringup.svg" alt="bringup" />

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

Authentication
--------------

Shoggoth uses [OpenLDAP](https://www.openldap.org/) (via
[osixia/openldap](https://hub.docker.com/r/osixia/openldap)) as a centralized
LDAP directory for user authentication across services that support it.

### Accounts

Three LDAP accounts exist, each serving a distinct role:

| Account | Purpose | Consumed by |
|----|----|----|
| `admin` | OpenLDAP administrator (`cn=admin`); used for LDAP management and as Grafana’s local admin login | openldap post_start, Grafana |
| `sldapauth` | Service bind account — services that authenticate users via LDAP bind as this account | Gitea, Redmine, CDash, OpenBao |
| `sslave` | CI/automation account — reserved for slave containers and workflows | *(not yet consumed)* |

### Password management

[OpenBao](https://openbao.org/) is the single source of truth for all account
passwords. The `bringup` container generates SSHA password hashes and writes
LDIF files; the openldap `post_start` script applies them on every container
start.

Account passwords are auto-generated on first run and stored in OpenBao. The
admin password is resolved in order of priority:

1.  Existing value in OpenBao (`secret/data/openldap/admin-password`)
2.  File at `private/admin-password.txt` (first-run only; not used for rotation)
3.  Auto-generated random password (stored in OpenBao for subsequent starts)

### Group memberships

Two groups are used to control access:

- **admins** — members are granted the `shoggoth-admin` policy in OpenBao,
  giving full access to the secret store. The `cn=admin` directory admin is the
  initial member. Add human admin users to this group.
- **shoggoth-auth** — service bind accounts that authenticate users on behalf of
  applications. `sldapauth` is the initial member. This group has no OpenBao
  policy mapping.

### OpenBao integration

Members of the `admins` group are mapped to the `shoggoth-admin` policy,
granting full access to OpenBao’s secret store. This allows human operators in
`admins` to log in to OpenBao using their LDAP credentials.

### Services

| Service | Auth method | Notes |
|----|----|----|
| Gitea | LDAP (bind as `sldapauth`) | On-the-fly account creation; `admins` group members become Gitea admins |
| Redmine | LDAP (bind as `sldapauth`) | On-the-fly account creation; auto-configured via entrypoint |
| CDash | LDAP (bind as `sldapauth`) | Configured in entrypoint |
| OpenBao | LDAP (bind as `sldapauth`) | `admins` → `shoggoth-admin` policy |

Client Configuration
====================

Run the setup script on each client machine.

``` bash
# Generate config files in default directory `~/.config/shoggoth/`
./shoggoth/setup-client.sh --client-conf --domain s.local

# Generate config files in a custom directory
./shoggoth/setup-client.sh --client-conf /path/to/dir --domain s.local

# Configure Docker caching proxy and registry
./shoggoth/setup-client.sh --docker --domain s.local --host-ip 192.168.1.100

# Update /etc/hosts with service hostnames (modifies /etc/hosts directly)
./shoggoth/setup-client.sh --update-hosts --domain s.local --host-ip 192.168.1.100

# Install apt cache config to system apt config (requires --client-conf first)
./shoggoth/setup-client.sh --client-conf --apt-cache --domain s.local --host-ip 192.168.1.100

# Configure Docker, hosts, apt cache, and generate client config files
./shoggoth/setup-client.sh --all --domain s.local --host-ip 192.168.1.100

# Configure with Gitea and Redmine tokens (generates env, qwen configuration)
./shoggoth/setup-client.sh --client-conf --domain s.local --api-gateway --gitea-user your-user
```

The script generates the following files when `--client-conf` is used:

| File | Description |
|----|----|
| `env` | Environment variables for all services |
| `apt-cache.conf` | APT cache configuration |
| `resolv.conf` | DNS resolver configuration |
| `qwen-settings.json` | Qwen Code telemetry configuration |

MCP server configuration and skills are provided by the shoggoth Qwen Code
plugin. Install it after running `setup-client.sh`:

``` bash
wget http://s.local/plugin.tar.gz
qwen extensions install plugin.tar.gz
```

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
firefox http://apt-cache.s.local/acng-report.html
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

### Gitea Git Server

Clone repositories via SSH or HTTP:

``` bash
# SSH (configure SSH key in Gitea first)
git clone ssh://git@git.s.local/admin/repo.git

# HTTP
git clone http://git.s.local/admin/repo.git
```

Configure the `tea` CLI by providing a token:

``` bash
./shoggoth/setup-client.sh --domain s.local --gitea-token your-token
set -a; source ~/.config/shoggoth/env; set +a
tea issues list
```

### Gitea MCP Server (AI Agent Integration)

The shoggoth Qwen Code plugin provides the `shoggoth-mcp` MCP server, which
proxies to the Gitea and Basic Memory MCP servers via the LiteLLM MCP gateway.
Install the plugin with:

``` bash
qwen extensions install http://s.local/plugin --consent
```

### Redmine Project Management Server

Access the Redmine web interface:

``` bash
# Open Redmine in browser
firefox http://redmine.s.local

# Default credentials: admin / admin (change on first login)
```

Install plugins by cloning them into the `shoggoth/redmine/plugins` directory
and restarting the service.

### Redmine MCP Server (AI Agent Integration)

1.  Log in to Redmine with administrator privileges
2.  Go to "Administration" → "Settings" → "API" tab
3.  Check "Enable REST web service"
4.  Generate "API access key" in personal settings.

The Redmine CLI is configured via environment variables generated by
`setup-client.sh --api-gateway`. MCP server configuration is provided by the
shoggoth Qwen Code plugin (see Gitea MCP section above).

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
make log SERVICE=localai

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
- <https://github.com/meirwah/awesome-workflow-engines>
- <https://leviwheatcroft.github.io/selfhosted-awesome-unlist/> (not maintained)
- <https://gitea.com/gitea/awesome-gitea>
- <https://github.com/trueforge-org/truecharts>

Agentic coding
--------------

- <https://github.com/bradAGI/awesome-cli-coding-agents>
- <https://github.com/JuliusBrussee/caveman>
- <https://github.com/mattpocock/skills>
- <https://github.com/K-Dense-AI/scientific-agent-skills>
- <https://github.com/arpitg1304/robotics-agent-skills>
- <https://github.com/punkpeye/awesome-mcp-servers>
- <https://github.com/mahdin75/gis-mcp>
- <https://github.com/jjsantos01/qgis_mcp>
- <https://github.com/awesome-opencode/awesome-opencode>
- <https://github.com/shanraisshan/claude-code-best-practice#%EF%B8%8F-development-workflows>
- <https://github.com/ai-boost/awesome-harness-engineering>
- <https://github.com/alibaba/open-code-review>
