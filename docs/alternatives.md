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
