# Service Alternatives

Chosen and alternative services for the Shoggoth stack (see `shoggoth/docker-compose.yml`).

## Reverse proxy / API gateway

- **[Angie](https://docker.angie.software/angie:latest)** (selected)
- <https://github.com/apache/apisix> (Apache 2.0)
  - + full API gateway with dynamic routing, load balancing, circuit breaking, health checks
  - + ai-proxy plugin for LLM request normalization (8+ providers, LocalAI via openai-compatible)
  - + ai-rate-limiting (token-based), ai-prompt-decorator plugins
  - + mcp-bridge plugin converts stdio MCP servers to HTTP SSE
  - + can replace both Angie and LiteLLM as a single service
  - + Prometheus metrics (with llm_model labels) and OTel tracing
  - + Docker deployment, no database required (standalone mode)
  - - LLM proxy less feature-rich than LiteLLM (no virtual keys, no cost tracking, fewer providers)
  - - MCP bridge is stdio-to-SSE only, not a full MCP gateway (no OAuth2, no OpenAPI-to-MCP conversion)
  - - ai-proxy only routes to one provider per route (no multi-provider load balancing)
- <https://github.com/Kong/kong> (Apache 2.0 / Enterprise)
  - + one of the most mature API gateways, DB-less mode for simple deployments
  - + ai-proxy plugin for single-provider LLM routing (Enterprise for multi-provider)
  - + MCP governance features (Enterprise only)
  - + Prometheus metrics, OTel tracing
  - - multi-provider load balancing, fallback, and semantic routing require AI Proxy Advanced (Enterprise)
  - - MCP features are behind Kong Konnect paid tier
  - - OSS tier provides single-provider-per-route LLM proxy only, not a full LiteLLM replacement
- <https://github.com/alibaba/higress> (Apache 2.0, CNCF Sandbox)
  - + unified LLM gateway with multi-model load balancing, token rate limiting, caching
  - + MCP server hosting via plugin mechanism, openapi-to-mcp conversion tool
  - + full API gateway built on Istio/Envoy (routing, WAF, service discovery, JWT/OIDC)
  - + can replace both Angie and LiteLLM as a single service with the most complete feature coverage
  - + AI observability for LLM and MCP traffic, audit logging for MCP tool calls
  - + Docker deployment (single all-in-one image)
  - - primarily oriented toward Chinese market; documentation and community largely Chinese-language
  - - Istio/Envoy foundation adds operational complexity compared to nginx/APISIX
- <https://github.com/traefik/traefik> (MIT)
  - + automatic Docker service discovery, Let's Encrypt TLS, HTTP/3
  - + lazy DNS resolution (starts fine even if upstreams are down)
  - - no LLM or MCP capabilities, no static file serving, no WebDAV
- <https://github.com/caddyserver/caddy> (Apache 2.0)
  - + automatic HTTPS, static file serving, WebDAV module
  - + lazy DNS resolution
  - - no Docker service discovery, no LLM or MCP capabilities
- <https://github.com/haproxy/haproxy> (GPL-2.0)
  - + battle-tested L4/L7 proxy, lazy DNS resolution, health checks
  - - no static file serving, no WebDAV, manual Docker integration
- <https://github.com/envoyproxy/envoy> (Apache 2.0)
  - + high-performance L4/L7 proxy, dynamic xDS config, lazy DNS
  - - extremely complex config, no static serving/WebDAV, overkill for Docker Compose

## LLM proxy

- **[LiteLLM](https://github.com/BerriAI/litellm)** (selected)
  - OpenAI-compatible gateway for LocalAI with Prometheus metrics and OTel tracing
  - Also serves as MCP gateway for observability on MCP tool calls
- <https://github.com/Portkey-ai/gateway> (MIT / Enterprise)
  - + 250+ providers, automatic retries, weighted load balancing, fallbacks
  - + dedicated MCP gateway with auth, access control, and tool-call observability
  - + very lightweight (122kb, <1ms latency)
  - - no general reverse proxy capability (cannot serve Gitea/Redmine)
  - - no native Prometheus or OTel in OSS tier (enterprise/hosted only)
- <https://github.com/solo-io/gloo> (Apache 2.0 / Enterprise)
  - + AI gateway with LLM proxy (multiple providers), prompt guards, model failover
  - + MCP gateway support (Beta, enterprise only)
  - + general API gateway built on Envoy (routing, rate limiting, transformations)
  - - AI and MCP features are enterprise-only (Solo Enterprise subscription)
  - - primarily designed for Kubernetes, not Docker Compose
- <https://github.com/Maximhq/bifrost> (Apache 2.0 / Enterprise)
  - + 23+ providers, fallbacks, load balancing, semantic caching
  - + Go-based with very low overhead (~11us per request)
  - + MCP client/server/gateway listed as feature (enterprise-gated)
  - - no general reverse proxy capability (AI-only gateway)
  - - MCP gateway is enterprise-only

### nginx/OpenResty LLM Proxy Modules

- <https://github.com/z1o/openbridge> — SSE streaming, round-robin, model aliases, hot-reload config
- <https://github.com/hannes-sistemica/nginx-llm-proxy> — model routing, per-key token tracking, admin UI, health checks
- <https://github.com/onewesong/one-api-nginx> — decoupled auth, auto model routing from JSON body, Redis-backed
- <https://github.com/harryosmar/llm-ratelimit-gateway> — RPM/TPM/USD rate limiting, SSE tail buffer, circuit breaker (OpenResty + Redis)
- <https://github.com/BMMMM/gateii> — per-user proxy keys, token/cost tracking, provider extensibility (OpenResty)

### MCP Gateways

- <https://github.com/microsoft/mcp-gateway> — session-aware routing, tool gateway, Entra ID auth (C#/ASP.NET Core, K8s-native)
- <https://github.com/aiguicai/mcp-gateway> — unified MCP gateway, auth, management API (Rust)
- <https://github.com/loglux/authmcp-gateway> — JWT auth proxy for MCP servers (Python)

## SSO / Identity

- <https://github.com/authelia/authelia> — SSO / 2FA / OIDC provider; works with any reverse proxy via forward-auth
- <https://github.com/goauthentik/authentik> — full identity management with flows and policies
- <https://github.com/keycloak/keycloak> — enterprise SSO / IdP (heavy but feature-complete OIDC/SAML)

## LDAP directory

- **[OpenLDAP](https://www.openldap.org/)** (selected, via [osixia/openldap](https://hub.docker.com/r/osixia/openldap))
  - Full RFC 4511 LDAPv3 server with overlays (memberof, ppolicy, accesslog, syncprov)
  - openldap `post_start` script creates OUs, users, and groups via `ldapadd`/`ldapmodify`; passwords as SSHA hashes from OpenBao
  - `slapo-memberof` overlay provides real (stored) `memberOf` attribute for group-based auth
  - Config admin (`cn=admin,cn=config`) for overlay and module management via `cn=config` tree
  - Used as auth source by Gitea (Admin API auth source), CDash (LdapRecord-Laravel env vars), Redmine (manual config), OpenBao (LDAP auth method with group→policy mapping)
  - + complete LDAP read/write — arbitrary schema, replication (syncrepl), access controls (ACLs), password policies
  - + largest ecosystem of tools, documentation, and client compatibility — everything works with OpenLDAP
  - + `slapo-memberof` overlay provides real (stored) `memberOf` attribute, not virtual
  - + osixia image provides `_FILE` env vars for Docker secrets integration
  - - no built-in web UI — phpLDAPadmin deployed separately (see phpldapadmin service in docker-compose.yml)
  - - no REST/GraphQL API — all management via LDAP operations or `cn=config`
  - - complex configuration: overlay ordering, ACL syntax, index tuning
  - - ~50–150 MB RAM (smaller than enterprise options, but 3–5× lldap)
- <https://github.com/lldap/lldap> (Mozilla Public License 2.0)
  - Lightweight LDAP server in Rust with SQLite backend and built-in web UI
  - + ~20–50 MB RAM, ~30 MB Docker image (Rust + Alpine + SQLite) — lightest LDAP option by far
  - + built-in web UI for user/group management — no phpLDAPadmin or desktop client needed
  - + GraphQL API for scripted user/group operations (Terraform provider available)
  - + supports `_FILE` env vars for Docker secrets integration
  - + zero-knowledge proof password storage — password hashes never exposed via LDAP
  - - intentionally NOT a full LDAP server: no arbitrary schema, no LDAP browsing tools, no Synology compatibility
  - - group membership cannot be modified via LDAP Modify — must use GraphQL API
  - - no OAuth/OIDC provider — would need Authelia/Keycloak for SSO flows
  - - no Samba/Windows AD integration (WIP)
  - - `memberOf` attribute is virtual (computed at query time), not stored — some clients expect it to be persistent
  - - not compatible with some services.
- <https://www.port389.org/> (389 Directory Server, GPL-3.0)
  - Red Hat's enterprise LDAP server (basis for FreeIPA); multi-master replication, account lockout policies
  - + full LDAP compliance with replication, chaining, password policies, account policy plugin
  - + Cockpit web UI plugin available (separate install) for basic management
  - + `dsconf` CLI for scripted setup and configuration
  - - no built-in web UI — Cockpit plugin is a separate, heavier installation
  - - no native REST API
  - - ~150–400 MB RAM; enterprise-focused configuration complexity
  - - Docker images exist but setup involves `dscreate`/`dsconf` ceremony
- <https://directory.apache.org/apacheds/> (Apache License 2.0)
  - Java-based LDAP server with Kerberos and custom partition support
  - + Apache Directory Studio desktop client for management (not web-based)
  - + supports SASL, Kerberos, custom partitions
  - - Java overhead (~150–300 MB RAM); less actively maintained
  - - no web UI, no REST API
  - - weaker performance than native C/Rust implementations
- <https://github.com/389ds/freeipa> (GPL-3.0)
  - Full identity management: 389 DS + Kerberos + Dogtag CA + HBAC + sudo rules
  - + comprehensive web UI, JSON-RPC API, OTP, SSH key management, host-based access control
  - + full LDAP compliance backed by 389 Directory Server
  - - ~500 MB–1.5 GB RAM (389 DS + Kerberos + CA + Dogtag)
  - - not designed for Docker — community images exist but are complex and fragile
  - - requires FQDN + DNS; designed for enterprise Linux fleets, not self-hosted/homelab
  - - massive overkill when you only need centralized auth for 4–5 web services
- <https://github.com/samba-team/samba> (GPL-3.0)
  - Full Active Directory domain controller (Samba 4 AD DC)
  - + complete AD compatibility: GPOs, trust relationships, Kerberos, DNS
  - + full LDAP read/write with AD-compatible schema
  - - ~200–500 MB RAM; very resource-heavy for simple auth
  - - not designed for containerization; requires careful DNS/Kerberos setup
  - - Windows-centric; use RSAT or third-party tools for management (no web UI)
  - - DNS server requirement conflicts with existing Unbound setup
  - - overkill unless Windows domain integration is specifically needed

## APT cache

- **[apt-cacher-ng](https://hub.docker.com/r/sameersbn/apt-cacher-ng)** (selected)
  - <https://github.com/sameersbn/docker-apt-cacher-ng>
  - <https://github.com/gnzsnz/apt-cacher-ng>

## PyPI cache

- ~~**[proxpi](https://github.com/EpicWink/proxpi)**~~ (replaced with Angie proxy_cache)
  - <https://hub.docker.com/r/epicwink/proxpi>
  - Periodically got stuck, required manual restart; no healthcheck or watchdog
- <https://github.com/EpicWink/proxpi#alternatives> — alternatives list maintained by proxpi author:
  - <https://github.com/your-tools/simpleindex> — URL router to multiple indices; no caching without custom plugins; no official Docker image
  - <https://github.com/pypa/bandersnatch> — full PyPI mirror (manual sync, not on-demand proxy); no official Docker image; disk-heavy (entire PyPI mirror)
  - <https://github.com/devpi/devpi> — full index server with on-demand mirroring, caching, user indexes, replication; heavyweight (DB backend); no official Docker image (30+ fragmented community images); overkill for simple cache
  - <https://github.com/pypiserver/pypiserver> — serves local packages, redirects to PyPI for missing packages (pip fetches directly — no bandwidth savings); actively maintained (v2.4.1, Feb 2026); official Docker image `pypiserver/pypiserver`; no caching of proxied packages
  - <https://github.com/anthraxx/dumb-pypi> — static site generator for package index; no proxy, no caching, no server; not applicable
  - <https://pypi.org/project/pypi-cloud/> — hosted/managed service; private index is paid; no clear proxying support; not self-hostable
  - <https://github.com/whoatemybutter/pypiprivate> — serves local/S3 packages only; no proxy to PyPI; not applicable
  - <https://github.com/pulp/pulpcore> — generic content repository (RPM/deb/Python/etc.); proxying but no caching; requires Redis + Postgres + API + workers; massively overkill
  - <https://github.com/wolever/pip2pi> — manual sync of specific packages; no proxy; not applicable
  - <https://github.com/danihodovic/nginx_pypi_cache> — nginx proxy_cache for PyPI; lightweight; repo appears defunct (404); approach is sound and can be implemented as custom nginx/Angie config
  - <https://github.com/jcsalterego/flask-pypi-proxy> — explicitly unmaintained; no cache size limit; not applicable
  - <https://docs.python.org/3/library/http.server.html> — stdlib static file server; no proxy; not applicable
  - Apache with mod_rewrite + mod_cache_disk — would work but adds Apache to the stack (not currently used)
  - <https://gemfury.com> — hosted/managed; private index is paid; unclear proxy support; not self-hostable
  - **[Angie proxy_cache](https://en.angie.software/angie/)** (selected) — reused existing Angie `web-internal` container with `proxy_cache` to pypi.org; eliminates proxpi container; nginx proxy_cache is battle-tested with no Python process to hang; `python-cache.${SHOGGOTH_DOMAIN}` network alias on `web-internal` service; `/index/` proxies to `https://pypi.org/simple/` with `sub_filter` rewriting file URLs to `/files/`; `/files/` proxies to `https://files.pythonhosted.org/` with 30-day cache; PEP 691 JSON content types handled via `sub_filter_types`
- Additional alternatives found via broader search (not on proxpi's list):
  - <https://github.com/SENYSENYSENY16/PROKKI> — lightweight PyPI reverse proxy cache written in Haskell; Docker image `ghcr.io/senysenyseny16/prokki`; actively maintained (v0.2.15, Jan 2026); BSD-3-Clause; config via `config.toml` (host, port, cache dir, multiple upstream indexes); 16 stars; 0 open issues; purpose-built for the exact use case (caching proxy between pip/uv/poetry and PyPI); supports multiple upstream indexes simultaneously (PyPI, PyTorch CUDA wheels, etc.); pip connects via `PIP_INDEX_URL=http://<host>:<port>/<index_name>/simple`
  - <https://github.com/LIVEHL/AIMIRROR> — multi-source download accelerator (PyPI, Docker Hub, CRAN, HuggingFace); Docker image `ghcr.io/livehl/aimirror:latest`; actively maintained (v0.3.4, Mar 2026); MIT; 212 stars; LRU cache eviction with configurable max size (default 100 GB); parallel chunked downloading for large files; content rewriting for transparent proxying; FastAPI-based (Python); more complex than needed for pure PyPI caching; `/stats` endpoint for cache monitoring
  - <https://github.com/sonatype/nexus-public> (Nexus Repository CE) — enterprise artifact repository manager; official Docker image `sonatype/nexus3` (100M+ pulls); supports PyPI proxy repository with caching; free Community Edition; actively maintained; heavyweight (2-3 min startup, multi-format: Maven, npm, Docker, PyPI, NuGet, etc.); overkill for PyPI-only caching but viable if multi-format artifact management is desired
  - <https://jfrog.com/artifactory> (Artifactory OSS) — enterprise binary repository manager; official Docker image `releases-docker.jfrog.io/jfrog/artifactory-oss`; supports PyPI remote repository with caching; free OSS edition (limited features); actively maintained; heavyweight; overkill for PyPI-only caching
  - <https://inedo.com/proget> (ProGet) — multi-format package manager with PyPI proxy feed; Docker installation supported; actively maintained by Inedo; commercial licensing required (no free edition); overkill for PyPI-only caching
  - <https://github.com/mardiros/pyshop> — Pyramid-based PyPI caching proxy; Docker image `mardiros/pyshop`; requires PostgreSQL/MySQL; archived (Dec 2021); unmaintained; not viable
  - <https://github.com/NATHANVAUGHN/MYPYPI> — lightweight pull-through PyPI cache; includes Dockerfile but no pre-built image; archived (May 2023); unmaintained; not viable
  - <https://github.com/marksholund/pynexus> — FastAPI-based PyPI caching proxy with metadata TTL and ETag support; no Docker image; 0 stars; unproven; not viable
  - <https://github.com/timreynolds/vouch> — multi-format registry proxy (PyPI, npm, Maven, RubyGems, crates.io, Go); supports file/memory/S3/GCS/Azure cache backends; no pre-built Docker image; 3 commits total; extremely early stage; not production-ready
  - <https://github.com/GUYSKK/WEBSITE-MIRROR> — nginx-based PyPI/npm mirror with proxy_cache; Docker image `guyskk/pypi-mirror:latest-amd64`; 7 commits, 1 star; only amd64; very low activity; essentially same approach as nginx_pypi_cache / custom Angie config
  - <https://github.com/PEDIA/REPOCACHE> — universal caching repository (PyPI, Maven, npm, yum, rustup); no Docker image; last release Feb 2021; inactive; not viable
  - <https://github.com/Daneb255/pkggate> — supply-chain security proxy for PyPI (PEP 691); Docker image `ghcr.io/daneb255/pkggate`; does NOT cache package files — only caches threat intelligence; not a caching proxy; not applicable

## Docker

### Security

- <https://github.com/Tecnativa/docker-socket-proxy>

### cache (mirroring proxy)

- **[docker-registry-proxy](https://github.com/rpardini/docker-registry-proxy)** (selected)
- <https://docs.docker.com/docker-hub/image-library/mirror/#run-a-registry-as-a-pull-through-cache>
  - "Only the central Hub can be mirrored."
- <https://github.com/spegel-org/spegel>
  - needs kubernetes

## Docker registry

- **[distribution](https://github.com/distribution/distribution)** (selected)
  - <https://distribution.github.io/distribution/>

## DNS

- **[unbound](https://hub.docker.com/r/mvance/unbound/)** (selected)

### Dynamic DNS injection from Docker

Current Unbound setup uses static records rendered once by the bringup
container. Adding/removing services requires editing the template and
re-deploying. Dynamic injection would allow Docker containers to register their
own DNS records on start/stop.

- <https://github.com/nginx-proxy/docker-gen> + **[Dnsmasq](https://thekelleys.org.uk/dnsmasq/doc.html)**
  - docker-gen watches Docker events (`start`, `stop`, `die`), generates dnsmasq hosts file from Go template
  - dnsmasq auto-reloads via `--hostsdir=` (inotify-like, no restart or signal needed)
  - wildcard subdomains work natively: `address=/.s.local/172.20.0.80`
  - + lightest option (~50MB alpine image for dnsmasq, ~20MB for docker-gen)
  - + no restart, no signal, truly zero-downtime record updates
  - - loses Unbound blacklist format (needs conversion to dnsmasq `address=/...` syntax)
  - - docker-gen template needs writing and maintaining
- <https://github.com/nginx-proxy/docker-gen> + Unbound
  - docker-gen generates `unbound_local-data.conf`, triggers Unbound reload
  - + keeps Unbound, existing blacklist, existing setup
  - - Unbound cannot reload `local-data` on SIGHUP (only reopens logs); requires full `unbound-control reload` or process restart
  - - restart causes brief DNS outage on every container start/stop
  - - `redirect` zone type requires per-subdomain zone+record pairs (awkward for dynamic generation)
- <https://coredns.io/> + <https://etcd.io/> (etcd plugin)
  - CoreDNS reads DNS records from etcd dynamically; a bridge process writes records on container events
  - + truly dynamic — no file regeneration, no restarts, etcd API is simple (HTTP PUT/DELETE)
  - + CoreDNS is production-grade (used by Kubernetes)
  - - adds significant complexity (etcd cluster + CoreDNS + bridge process) for ~10 services
  - - no native Docker watcher — needs custom bridge
  - - no built-in ad-blocking (no equivalent to Unbound blacklist)
- <https://github.com/mageddo/dns-proxy-server>
  - standalone DNS server that natively reads Docker container names/hostnames
  - + zero configuration — auto-discovers containers, wildcard hostname support
  - + solves `host.docker` to Docker host IP, simple web UI
  - - Java-based (heavy JVM footprint), less mature, no ad-blocking, no DNSSEC
- **[Pi-hole](https://pi-hole.net/)** + docker-gen
  - Pi-hole runs dnsmasq/FTL under the hood; combines DNS serving + ad-blocking in one tool
  - docker-gen writes to `/etc/pihole/custom.list` (hosts format) or dnsmasq snippets to `/etc/dnsmasq.d/`, then SIGHUPs FTL
  - wildcard subdomains work natively: `address=/.s.local/172.20.0.80`
  - + built-in ad-blocking replaces both Unbound and vendored blacklist — no format conversion needed
  - + web UI for DNS query log, local record management, blacklist inspection
  - + well-maintained Docker image (`pihole/pihole`)
  - + SIGHUP reloads custom.list without restart (not inotify-like, but fast)
  - - heavier than plain dnsmasq (PHP web UI, FTL daemon, lighttpd — ~100MB image)
  - - local DNS records in hosts format only via `custom.list` (A/AAAA only; CNAME/SRV/TXT needs dnsmasq snippets)
  - - another web UI to manage alongside Angie and wg-easy
- Docker network aliases
  - `aliases` in compose network definitions, resolved by Docker's embedded DNS (127.0.0.11)
  - + already built into Docker, fully automatic, zero extra software
  - - only works for containers on the same Docker network — VPN clients cannot reach 127.0.0.11

## AI

### Model server

- **[LocalAI](https://github.com/mudler/LocalAI)** (selected)
  - <https://hub.docker.com/r/localai/localai>
- <https://hub.docker.com/r/ollama/ollama>
  - <https://github.com/jameschrisa/Ollama_Tuning_Guide>
  - <https://deepwiki.com/ollama/ollama/4.6-quantization>

### Memory

- **[basic-memory](https://basicmemory.com)** (selected)
  - <https://docs.basicmemory.com/reference/docker>
- <https://github.com/doobidoo/mcp-memory-service>
  - very sloppy
- <https://github.com/rohitg00/agentmemory>
  - no docker
- <https://github.com/Gentleman-Programming/engram>
  - always keeps local database
- <https://github.com/MemPalace/mempalace>
  - hype
- <https://github.com/volcengine/OpenViking>
  - requires llm

## Software forge

- **[Gitea](https://gitea.io)** (selected)
- <https://forgejo.org/compare-to-gitea/>

## CI runner

- **[act_runner](https://docs.gitea.com/next/usage/actions/act-runner)** (selected)
- <https://github.com/harness/harness>

## MCP for Gitea

- **[gitea-mcp-server](https://gitea.com/gitea/gitea-mcp)** (selected)
  - <https://hub.docker.com/r/gitea/gitea-mcp-server>

## Git pages

- **[git-pages](https://codeberg.org/git-pages/git-pages)** (selected)
  - <https://git-pages.org/>

## Database

- **[PostgreSQL](https://hub.docker.com/_/postgres)** (selected)

## Project management

- **[Redmine](https://www.redmine.org/)** (selected)
  - <https://hub.docker.com/_/redmine>
  - <https://github.com/topics/redmine-plugin>

### Task management

- **[Redmine](https://www.redmine.org/)** (selected)
  - issue tracking with subtasks, parent-child relationships, time tracking, custom fields, workflows
  - + hierarchical task decomposition (subtasks with arbitrary depth)
  - + cross-project task relations, issue dependencies (blocks/blocked-by via relations)
  - + REST API for integration with Kestra workflows and MCP servers
  - + mature plugin ecosystem (agile boards, Gantt charts, burndown charts)
  - + LDAP authentication via `AuthSourceLdap` (configured in entrypoint)
- [Gitea project boards](https://docs.gitea.com/usage/project-boards/)
  - built-in kanban boards in Gitea, no additional service required
  - + zero infrastructure cost — already deployed as part of the forge
  - + tight integration with issues and pull requests
  - - only flat task structure is supported — no subtask decomposition, no
    parent-child hierarchies, no task dependencies
  - - no time tracking, no custom fields, no cross-project task relations
  - - limited workflow customization (no status transitions, no role-based
    field permissions)
  - - no REST API for board operations (only issue API); Kestra/MCP
    integration would require workarounds

### Redmine webhook plugins

- planned for the next major release -> <https://www.redmine.org/issues/29664>
- <https://github.com/guyinwonder168/redmine_webhook_plugin>
  - only manual triggers work, Zeitwerk naming issues
- <https://github.com/itnode/redmine_webhook>
  - this is a fork, original is archived and does not work with 6.x
  - obscure location (project settings -> enable webhook module -> webhook)
  - not triggered by kanban (<https://github.com/suer/redmine_webhook/issues/4>, <https://www.redmine.org/issues/8757>)

## Workflow/orchestration engine

- **[Kestra](https://kestra.io/docs)** (selected)
  - <https://github.com/kestra-io/kestra>
- <https://github.com/dagucloud/dagu/>
  - no webhook trigger support in free version
- <https://github.com/StackStorm/st2>
  - an overkill
- <https://github.com/autokitteh/autokitteh> (Apache 2.0, Go+Python)
  - + webhook triggers, Python/Starlark workflows
  - - wraps Temporal (heavy dep), no container execution, no OTel, no file-based workflow loading
- <https://github.com/ovh/utask> (BSD-3, Go)
  - + lightweight (Go+Postgres), YAML templates loaded from dirs, conditional steps/loops
  - - no incoming webhook triggers, no container execution, no OTel
- <https://github.com/hatchet-dev/hatchet> (MIT, Go+TS)
  - + webhook triggers, OTel traces+metrics, concurrency control, fully-featured self-hosted
  - - no container execution (workers are code), no file-based workflow loading,
    requires rewriting dispatcher as a Python/TS worker service (Postgres required)
- <https://github.com/runabol/tork> (MIT, Go)
  - + lightweight (single binary, in-memory broker for standalone), YAML job definitions,
    native Docker/Podman container execution with shell scripts, concurrency control
  - - no incoming webhook triggers (API only, would need adapter), no OTel (basic /metrics only),
    no file-watching (jobs submitted via REST API), workflows are not stored and have to be
    submitted with API calls
- <https://github.com/svix/svix-webhooks> (MIT, Rust)
  - webhook sender/receiver service, not a workflow engine
  - + OTel tracing, retries, signature verification
  - - no workflow/container execution, no file-based config, requires Postgres+Redis
- <https://github.com/adnanh/webhook> (MIT, Go)
- <https://github.com/ncarlier/webhookd> (MIT, Go)
  - no container execution, no concurrency control, no persistence, no OTel
- <https://github.com/dagster-io/dagster> (Apache 2.0, Python+TS)
  - data/ML pipeline orchestrator, not a general workflow engine
  - + Python-based asset definitions, lineage/observability, CI/CD integration
  - - no Docker container execution, no webhook triggers, no OTel, no YAML/file-based workflows,
    Python+TS web UI stack is heavyweight, designed for data pipelines not task dispatch

## Monitoring

### Dashboard

- **[Grafana](https://grafana.com/)** (selected)

### Log aggregation

- **[Loki](https://github.com/grafana/loki)** (selected)

### Tracing

- **[Tempo](https://github.com/grafana/tempo)** (selected)

### Metrics

- **[Victoria Metrics](https://github.com/VictoriaMetrics/VictoriaMetrics)** (selected)

### Host metrics

Collected by the OpenTelemetry Collector's [hostmetricsreceiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/receiver/hostmetricsreceiver) — no dedicated service needed.

### Container metrics

Collected by the OpenTelemetry Collector's [dockerstatsreceiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/receiver/dockerstatsreceiver) — no dedicated service needed.

### Telemetry gateway

- **[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)** (selected)


## Terminal access

- **[ttyd](https://github.com/tsl0922/ttyd)** (selected)
- <https://github.com/gbasin/agentboard>

## VPN

- **[wg-easy](https://github.com/wg-easy/wg-easy)** (selected)
  - WireGuard VPN server with web management UI; single container, no extra databases
  - + simplest possible WireGuard deployment — web UI for peer management, QR code config
  - + single container (WireGuard + UI), standard WireGuard protocol
  - + INIT_* environment variables allow zero-config first boot
  - + DNS pushed to clients via INIT_DNS, replaces /etc/hosts workflow
  - + no control plane, no relay, no external dependencies — perfect for private LAN where server IP is known
  - + ~4,000 LOC in-kernel WireGuard, formally verified cryptography, no opaque binary blobs
  - - no automatic key exchange or enrollment (peers added manually via web UI)
  - - no NAT traversal / DERP relay — requires direct UDP connectivity
  - - no ACL policies or subnet routing
- <https://github.com/gravitl/netmaker> (Apache-2.0 core, commercial Pro license)
  - WireGuard mesh VPN with orchestrator, web UI, private DNS, and ACLs
  - + mesh topology — peers connect directly, not through a hub
  - + built-in private DNS (CoreDNS integration)
  - + access control lists, OAuth, remote access gateways, Kubernetes support
  - + automatic node enrollment via netclient agent
  - + STUN-based hole punching for NAT traversal
  - - 4+ container deployment (netmaker, netmaker-ui, caddy, mosquitto) vs wg-easy's single container
  - - requires wildcard DNS and static public IP
  - - Pro features (ACLs, OAuth, egress gateways) require commercial license
  - - significantly more complex setup and maintenance for a personal/homelab use case
- <https://github.com/juanfont/headscale> (BSD-3-Clause)
  - Open-source Tailscale coordination server; clients use Tailscale
  - + automatic key exchange and peer enrollment (auth keys)
  - + MagicDNS pushes Unbound through the tunnel — replaces `/etc/hosts` workflow
  - + NAT traversal via STUN + DERP relay fallback
  - + ACL policies, subnet routing (`--advertise-routes`)
  - + single container + SQLite, no extra databases
  - - Tailscale client GUI wrappers on non-Linux platforms are proprietary (networking core is BSD-3-Clause)
  - - coordination server dependency (existing tunnels survive server downtime, but new peers cannot enroll)
  - - DERP relay requires containers on bridge networks to reach host LAN IP, which Docker blocks by default — fundamental blocker for Docker deployments
- <https://github.com/DefGuard/defguard> (AGPL-3.0 core, proprietary client, enterprise license)
  - + per-connection MFA on WireGuard, LDAP/OIDC integration, nftables-based gateway
  - + compliance-oriented (GDPR, HIPAA, PCI-DSS)
  - - multi-container architecture (core, gateway, proxy, avanguard), proprietary desktop/mobile clients
  - - overkill for personal use; MFA on WireGuard is enterprise compliance theater
- <https://github.com/firezone/firezone> (Elastic License 2.0 + Apache-2.0)
  - + well-engineered (Elixir control plane, Rust data plane), WebRTC NAT traversal
  - + OIDC/SSO integration, split-DNS
  - - self-hosting explicitly "not production supported" — app store clients only work with managed Cloud
  - - self-hosters must build their own clients from source (Rust/Swift/Kotlin)
  - - source-available freemium model; self-hosting is second-class experience
- <https://github.com/slackhq/nebula> (MIT)
  - + fully open-source (all platforms, including mobile), single binary, no kernel module
  - + mesh topology with lighthouse discovery, UDP hole punching, built-in DNS
  - + certificate-based auth with groups and firewall rules
  - - different protocol (Noise Protocol Framework), not WireGuard — smaller ecosystem
  - - no automatic enrollment; certificates must be signed and distributed manually
  - - userspace-only (slightly higher latency than kernel WireGuard)

## Secret storage

- **[OpenBao](https://openbao.org/)** (selected)
  - Vault fork (CNCF sandbox, BSL 1.1 → Apache 2.0 after 5 years); single container, integrated Raft storage, built-in web UI
  - Bringup generates/reads secrets via KV v2 API (`bao_get_or_generate` pattern: GET first, generate and PUT on 404)
  - Services continue to consume per-UID bind-mounted rendered files — no HTTP client needed at runtime
  - Root token stored on persistent volume, single unseal key (1-of-1), auto-unseal on container restart via init container
  - Web UI at `openbao.s.local:80` for operators to view, rotate, and manually set secrets (no TLS, Docker network only)
  - ~150MB RAM, single container, no external database
  - + eliminates opaque `auto_secrets/` directory — secrets are queryable, auditable, and no longer scattered across the host
  - + idempotent generation: `bao_get_or_generate` returns existing value if present, making bringup safe to re-run
  - + web UI gives operator visibility into all secrets without host filesystem access
  - + `jq`-based JSON parsing/construction in bringup handles arbitrary secret values (quotes, backslashes, special chars)
  - + inject-once pattern with flag file (`shoggoth-injected`) migrates existing `auto_secrets/` and `private/` files into OpenBao on first boot
  - - seal/unseal ceremony adds operational complexity vs. a plain file store (mitigated by auto-unseal init container)
  - - root token used directly by bringup (no AppRole/restricted policy yet)
  - - BSL 1.1 license with delayed Apache 2.0 conversion (5-year wait)
- <https://github.com/hashicorp/vault> (BSL 1.1 → MPL 2.0 after 5 years)
  - + original project, largest ecosystem of clients, plugins, and integrations
  - + same KV v2 engine, same API — bringup scripts would be identical
  - + dynamic secrets (database credentials, PKI) for future use
  - - BSL 1.1 license with stricter field-of-use restrictions than OpenBao's BSL
  - - seal/unseal ceremony with same operational overhead as OpenBao, no lighter
  - - heavier resource usage (~200-300MB RAM minimum)
- <https://github.com/Infisical/infisical> (MIT core, Enterprise license)
  - + purpose-built secret management with native Kubernetes/Docker integration
  - + web UI, versioning, secret rotation, audit logs, team RBAC
  - + official CLI for `infisical fetch` in bringup scripts
  - - 3+ containers required (API, frontend, database) — ~4GB RAM minimum
  - - features like scanning, PKI, dynamic secrets are unused overhead for shoggoth
  - - runtime API dependency for service injection still has the HTTP client availability problem
- <https://github.com/ansible/ansible> (GPL-3.0)
  - + `ansible-vault` encrypts secrets at rest, simple CLI, no server
  - + can template per-UID secret files with `template` module
  - - no web UI, no API, no runtime access — purely a provisioning tool
  - - `ansible-vault` requires manual password entry or password file on disk
  - - doesn't solve the "where do generated secrets live" problem — just encrypts existing files
- Purpose-built secret store (~500 LOC, single binary)
  - + minimal footprint, exactly shoggoth's needs (generate, get, rotate, web UI)
  - + SQLite-backed, no external database, single container
  - + CLI for bringup (`secret-store generate`, `secret-store get`, `secret-store rotate`)
  - - custom code to maintain, test, and secure — reinventing a subset of what OpenBao provides
  - - no community, no audits, no security advisories
  - - feature scope would creep toward what OpenBao already provides (versioning, access control, audit)
- Per-UID bind mounts only (previous approach, no secret store)
  - + zero infrastructure — just files on disk with correct ownership
  - + no runtime dependency, no network calls, debuggable on host filesystem
  - - `auto_secrets/` directory is opaque — no way to query, audit, or rotate secrets without host access
  - - shared secrets between different-UID services need separate copies with correct ownership
  - - each new service requires UID knowledge and mkdir/chown/chmod boilerplate in bringup
  - - no web UI for operators to view or rotate secrets
