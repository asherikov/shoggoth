# Service Alternatives

Chosen and alternative services for the Shoggoth stack (see `shoggoth/docker-compose.yml`).

## Reverse proxy / API gateway

- **[Angie](https://docker.angie.software/angie:latest)** (selected)
- <https://github.com/apache/apisix> (Apache 2.0)
  - + full API gateway with dynamic routing, load balancing, circuit breaking, health checks
  - + ai-proxy plugin for LLM request normalization (8+ providers, Ollama via openai-compatible)
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
  - OpenAI-compatible gateway for Ollama with Prometheus metrics and OTel tracing
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

## APT cache

- **[apt-cacher-ng](https://hub.docker.com/r/sameersbn/apt-cacher-ng)** (selected)
  - <https://github.com/sameersbn/docker-apt-cacher-ng>
  - <https://github.com/gnzsnz/apt-cacher-ng>

## PyPI cache

- **[proxpi](https://github.com/EpicWink/proxpi)** (selected)
  - <https://hub.docker.com/r/epicwink/proxpi>

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

- **[Ollama](https://hub.docker.com/r/ollama/ollama)** (selected)
  - <https://github.com/jameschrisa/Ollama_Tuning_Guide>
  - <https://deepwiki.com/ollama/ollama/4.6-quantization>
- <https://github.com/mudler/LocalAI>

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

- **[node_exporter](https://github.com/prometheus/node_exporter)** (selected)

### Container metrics

- **[cAdvisor](https://github.com/google/cadvisor)** (selected)

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
