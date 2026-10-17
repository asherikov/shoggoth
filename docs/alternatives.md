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

- **[Zot](https://github.com/project-zot/zot)** (selected, `ghcr.io/project-zot/zot:latest`)
  - OCI-native distribution server with pull-through sync extension
  - Single instance handles all upstream registries (docker.io, ghcr.io, gcr.io, quay.io, mcr.microsoft.com, codeberg.org, docker.gitea.com, docker.angie.software, public.ecr.aws) via `sync` extension with on-demand mode
  - Containerd and Docker talk to Zot via standard OCI distribution API (`/v2/`)
  - PVC-backed local storage for cached layers
  - + OCI-native — no MITM TLS interception, no CA certificate distribution needed
  - + compatible with both containerd (k3s) and Docker daemon as clients
  - + single instance replaces 9 upstream registry mirrors (one per registry)
  - + lightweight Go binary (~50MB image), minimal resource usage
  - + tag regex filters per upstream to limit cached artifacts
  - + no init containers or certificate generation required
  - - no built-in Prometheus metrics (OTel tracing available)
  - - sync extension caches on first pull (cold start for new images)
  - - containerd falls back to upstream registries if the cache is unavailable (see note below)
  - - `onDemand` sync mode can hang the docker client silently during the upstream fetch — no progress indication while the manifest/layer is being pulled from the source registry, so it is not clear whether the pull is stuck or downloading
  - - first pull of an image fans out to the configured upstream registries, leaking the requested image name (and pull time) to external parties via upstream access logs
  - - containerd pull timeout is enforced even when an active download is progressing — slow links truncate in-progress layer fetches
- ~~**[docker-registry-proxy](https://github.com/rpardini/docker-registry-proxy)**~~ (removed)
  - MITM HTTPS proxy that intercepts TLS connections to upstream registries
  - Replaced because: MITM approach is incompatible with containerd — containerd validates TLS certificates and OCI image layer integrity, causing pull failures when a proxy re-signs layers with a different CA
  - Required distributing a custom CA certificate to all clients (containerd, Docker daemon)
  - One nginx instance per upstream registry, plus init containers for certificate generation
  - Port 3128 (squid proxy convention)
  - Does not work with containerd (and kubernetes).
- <https://distribution.github.io/distribution/> (CNCF Distribution)
  - The reference OCI registry implementation; can be configured as pull-through cache with `proxy.remoteurl`
  - + CNCF graduated project, widely deployed, battle-tested
  - + native OCI distribution API — same as Zot, fully containerd-compatible
  - + Prometheus metrics endpoint built-in
  - - one instance per upstream registry (no multi-upstream sync extension)
  - - 9 separate deployments needed for shoggoth's upstream registries vs 1 for Zot
  - - pull-through mode is less actively maintained than Zot's sync extension
  - **Per-upstream setup pattern** (each upstream = its own Distribution instance):
    - one `config.yml` per upstream with `proxy.remoteurl: https://<upstream>`, optional `proxy.username` / `proxy.password`, a storage backend (`filesystem` / `s3` / `azure` / `gcs`), and an HTTP listener
    - per upstream: PVC + Service + ConfigMap + Deployment ≈ 4 k8s objects; shoggoth's 9 upstreams → ~36 objects vs ~6 for Zot
    - containerd mirror wiring: one `endpoint = ["https://registry-<n>.s.local:<port>"]` block per upstream in `shoggoth/k3s/registries.yaml` (one host port per upstream, 5000–5008)
    - the community Helm chart (`oci://registry-1.docker.io/library/registry` or `helm/charts`) and Kustomize bases can compress per-registry boilerplate, but the N-deployments shape remains
  - **Operational deltas vs Zot:**
    - - per-registry resource footprint (typical ~256 Mi memory request + 1 PVC each) → aggregate ~2 Gi memory + 9 PVCs vs Zot's 256 Mi + 1 PVC
    - - per-registry host networking: each upstream needs its own port, Service, NetworkPolicy, and TLS cert → N× the surface area
    - - per-upstream credentials must be embedded in `config.yml`; rate-limited upstreams (docker.io anonymous pulls, ghcr.io without a PAT) require explicit `proxy.username` / `proxy.password` or pulls fail with 401/429
    - - same information-leak shape as Zot's `onDemand` sync on cache miss — phoning home to the configured upstream; with N upstreams the noise surface during broad pulls is wider
    - - no equivalent of Zot's `cleanup-sync-staging` init container (Distribution does not maintain an internal sync staging tree)
    - - mirror config is client-side: each of shoggoth's pull clients (k3s containerd, outer Docker daemon, inner DinD daemon) needs 9 per-host entries — 27 total vs Zot's 3
  - **Migration cost from Zot:** the `urls: [...]` list in `shoggoth/k3s/registry.yaml` does not translate directly; the 9 sync entries become 9 separate `proxy.remoteurl` blocks, the `gc` / `retention` settings move to Distribution's `storage.gc` / `storage.maintenance` keys, and the `ui` / `search` extensions have no built-in equivalent (would need a separate UI deployment)
- <https://docs.docker.com/docker-hub/image-library/mirror/#run-a-registry-as-a-pull-through-cache>
  - Docker's official documentation on pull-through cache mirroring
  - Notes: "Only the central Hub can be mirrored" (applies to Docker Hub's built-in mirror feature, not to third-party registries)
- <https://github.com/spegel-org/spegel>
  - Peer-to-peer OCI registry for Kubernetes — nodes share cached images via P2P
  - + eliminates external registry dependency entirely — images distributed across cluster nodes
  - - requires multi-node Kubernetes cluster (k3s single-node has no peers)
  - - does not cache upstream registries — only redistributes already-pulled images
- <https://github.com/goharbor/harbor>
  - CNCF Incubating; full-featured container registry with pull-through replication
  - Pull-through cache supports Docker Hub, AWS ECR, GCR, GHCR, Quay, Harbor, JFrog Artifactory
  - Native OCI distribution API — fully containerd-compatible
  - + mature enterprise features: RBAC, vulnerability scanning (Trivy), image signing (Cosign/Notary), replication policies, retention policies, audit logs, project quotas
  - + Prometheus metrics endpoint built-in
  - + active CNCF project, broad community and adoption
  - - heavyweight footprint: Postgres + Trivy + Notary + jobservice + core + portal + log ≈ 6–8 containers per deployment
  - - overkill for shoggoth's cache-only use case — RBAC, scanning, signing are unused overhead
  - - one proxy cache per upstream per project — more granular than Zot but more moving parts than the single Zot instance
  - - higher resource usage: dedicated compute for scanner, jobservice, and Postgres
- <https://github.com/uber/kraken>
  - Uber's P2P Docker registry, production-proven at Uber scale
  - P2P image distribution — nodes share already-pulled images across peers
  - + eliminates external registry dependency for already-cached images
  - - requires multi-node cluster (same single-node limitation as Spegel)
  - - does not cache upstream registries — only redistributes already-pulled images
  - - upstream activity has slowed (originally uber/kraken); maintenance cadence is uncertain

### Containerd mirror fallback behavior

When a mirror endpoint is configured in `registries.yaml`, containerd tries the
mirror first. If all mirror endpoints fail, containerd unconditionally falls back
to the original upstream registry. This fallback is hardcoded in the CRI plugin
source and cannot be disabled via `registries.yaml` or `hosts.toml` configuration.

Consequence: if the Zot cache is down, containerd pulls directly from upstream
registries (docker.io, ghcr.io, etc.) bypassing the cache entirely. There is no
configuration-only way to make pulls fail when the cache is unavailable.

Possible mitigations (none currently implemented):
- Node firewall rules blocking outbound HTTPS to upstream registry IPs — fragile
  (IPs change), may break non-container traffic to those hosts
- DNS override pointing upstream registry domains to an unreachable address —
  breaks all DNS resolution for those domains, not just containerd

## Docker registry

- **[distribution](https://github.com/distribution/distribution)** (selected)
  - <https://distribution.github.io/distribution/>

## DNS

- **[standalone CoreDNS](https://coredns.io/)** (selected, `dns`)
  - Single CoreDNS deployment handles both domain aliasing and DNS blacklisting
  - `hosts` plugin loads StevenBlack/hosts blocklist (returns 0.0.0.0 for blocked domains)
  - `rewrite` plugin maps `*.s.local` → `*.shoggoth.svc.cluster.local`
  - `forward` plugin sends non-rewritten queries to K3s CoreDNS (10.43.0.10)
  - ClusterIP 10.43.0.53 (same IP WireGuard pushes to VPN clients via INIT_DNS)
  - + single DNS service replaces both Unbound and coredns-custom ConfigMap
  - + avoids modifying K3s CoreDNS (no `coredns-custom` ConfigMap in `kube-system`)
  - + `hosts` plugin uses hash-based O(1) lookups — handles 150k+ entries at sub-ms latency
  - + `hosts` plugin auto-reloads blocklist file every 1h (configurable)
  - + standalone lifecycle — `make down` cleanly removes from shoggoth namespace
  - - returns 0.0.0.0 for blocked domains (not NXDOMAIN) — client connects to null address
  - - blocklist downloaded during `make sync` (requires internet on build host)

### Blocklist sources

The `hosts` plugin reads standard hosts-format files (`0.0.0.0 domain.com`).
Any hosts-format blocklist works. Selected and alternatives:

- **[StevenBlack/hosts](https://github.com/StevenBlack/hosts)** (selected)
  - ~140-170k entries, daily updates, curated from multiple upstream sources
  - Unified hosts file with optional extensions (gambling, porn, social, fakenews)
  - Download: `https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts`
  - + most popular hosts blocklist (60k+ GitHub stars), well-maintained
  - + clean format, no comments in host entries, CoreDNS-compatible out of the box
- <https://github.com/badmojr/1Hosts>
  - ~110-130k entries, daily updates, three tiers (Lite/Pro/Xtra)
  - Download: `https://badmojr.github.io/1Hosts/hosts/Pro.txt` (hosts format)
  - + smaller than StevenBlack with similar coverage
  - + actively maintained, good false-positive response time
- <https://github.com/AdguardTeam/AdGuardSDNSFilter>
  - ~60-80k entries, daily/weekly updates
  - Download: `https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt` (AdBlock syntax — needs conversion)
  - + well-maintained by AdGuard team
  - - not hosts format; needs conversion script or hosts-format variant
- <https://block.energized.pro/>
  - Multiple tiers: Spark (~15MB), Blu (~35MB), Ultimate (~75MB)
  - Download: `https://block.energized.pro/{variant}/formats/hosts.txt`
  - + largest available blocklists, multiple granularity levels
  - - very large files may increase CoreDNS memory usage significantly
- <https://pgl.yoyo.org/adservers/>
  - ~3.5-4k entries, weekly updates, Peter Lowe's list
  - Download: `https://pgl.yoyo.org/adservers/serverlist.php?hostformat=hosts`
  - + minimal, low false-positive rate, good baseline
  - - much smaller than other options — limited coverage
- <https://oisd.nl/>
  - ~200k+ entries, multiple updates per week
  - Download: `https://small.oisd.nl/hosts.txt` or `https://big.oisd.nl/hosts.txt`
  - + very comprehensive, frequently updated
  - - hosts format availability may change (project shifted to other formats in 2024)

### ~~Unbound~~ (removed)

Previously provided DNS blacklisting and stub-zone forwarding to CoreDNS.
Replaced by standalone CoreDNS (`dns`) which handles both functions.

- <https://hub.docker.com/r/mvance/unbound/>
  - Was used for DNS blacklist (NXDOMAIN for ad domains) and stub-zone to CoreDNS
  - Replaced because: Unbound cannot do DNS name rewriting (DNAME-like wildcard CNAME synthesis in redirect zones produces wrong results), requiring a separate CoreDNS layer anyway — two DNS servers for what one can do

### Domain aliasing (historical)

Shoggoth services use `*.s.local` hostnames that must resolve to K8s internal
service names (`*.shoggoth.svc.cluster.local`). This requires DNS name
rewriting — transparently mapping `grafana.s.local` to
`grafana.shoggoth.svc.cluster.local` so VPN clients can reach internal K8s
services by their external hostnames.

Approaches evaluated (standalone CoreDNS selected, others rejected):

- coredns-custom ConfigMap (K3s built-in CoreDNS)
  - Rewrite rules injected into K3s CoreDNS via `coredns-custom` ConfigMap in `kube-system` namespace
  - + fewer moving parts (no extra pod)
  - - modifies K3s system component — couples shoggoth DNS to K3s CoreDNS lifecycle
  - - `make down` must explicitly clean up `coredns-custom` from `kube-system`
  - - K3s CoreDNS restart required after applying custom ConfigMap
- Unbound `local-data` / `local-zone` — not viable for name rewriting
  - `local-data` with per-subdomain A records (e.g., `grafana.s.local. A 10.43.0.xx`) works but returns IP addresses, not rewritten names; requires manual per-service entries and IP maintenance
  - `local-zone: "s.local." redirect` with wildcard CNAME (`*.s.local. CNAME *.shoggoth.svc.cluster.local.`) produces wrong results: Unbound uses DNAME-like concatenation (RFC 6672) in redirect zones, so `grafana.s.local` resolves to `grafana.s.local.shoggoth.svc.cluster.local.` instead of `grafana.shoggoth.svc.cluster.local.`
  - Confirmed by reading Unbound source (`local_data_answer()` at localzone.c:1522, wildcard synthesis at lines 1634-1644)
- Wildcard A record — not viable
  - Unbound `local-data: "*.s.local. A <IP>"` returns a single IP for all `*.s.local` queries
  - Eliminates DNS-level name rewriting entirely — requires Ingress Controller with per-service Ingress resources and TLS termination
  - Changes the routing model: all `*.s.local` queries resolve to the same IP, breaking direct K8s service access
- dnsmasq / Pi-hole — not viable for name rewriting
  - dnsmasq `address=/.s.local/<IP>` only returns A records (IP addresses), cannot rewrite DNS names
  - Same limitations as wildcard A record approach

### Dynamic DNS injection from Docker

Current setup uses static records rendered once by the bringup container.
Adding/removing services requires editing the template and re-deploying.
Dynamic injection would allow Docker containers to register their own DNS
records on start/stop.

- <https://github.com/nginx-proxy/docker-gen> + **[Dnsmasq](https://thekelleys.org.uk/dnsmasq/doc.html)**
  - docker-gen watches Docker events (`start`, `stop`, `die`), generates dnsmasq hosts file from Go template
  - dnsmasq auto-reloads via `--hostsdir=` (inotify-like, no restart or signal needed)
  - wildcard subdomains work natively: `address=/.s.local/172.20.0.80`
  - + lightest option (~50MB alpine image for dnsmasq, ~20MB for docker-gen)
  - + no restart, no signal, truly zero-downtime record updates
  - - no DNS name rewriting (only returns IP addresses)
  - - docker-gen template needs writing and maintaining
- <https://github.com/nginx-proxy/docker-gen> + CoreDNS
  - docker-gen generates hosts file for `hosts` plugin + rewrite rules for aliasing
  - + keeps CoreDNS, existing blacklist format, existing rewrite rules
  - - docker-gen template needs writing and maintaining
- <https://coredns.io/> + <https://etcd.io/> (etcd plugin)
  - CoreDNS reads DNS records from etcd dynamically; a bridge process writes records on container events
  - + truly dynamic — no file regeneration, no restarts, etcd API is simple (HTTP PUT/DELETE)
  - + CoreDNS is production-grade (used by Kubernetes)
  - - adds significant complexity (etcd cluster + CoreDNS + bridge process) for ~10 services
  - - no native Docker watcher — needs custom bridge
- <https://github.com/mageddo/dns-proxy-server>
  - standalone DNS server that natively reads Docker container names/hostnames
  - + zero configuration — auto-discovers containers, wildcard hostname support
  - + solves `host.docker` to Docker host IP, simple web UI
  - - Java-based (heavy JVM footprint), less mature, no ad-blocking, no DNSSEC
- **[Pi-hole](https://pi-hole.net/)** + docker-gen
  - Pi-hole runs dnsmasq/FTL under the hood; combines DNS serving + ad-blocking in one tool
  - docker-gen writes to `/etc/pihole/custom.list` (hosts format) or dnsmasq snippets to `/etc/dnsmasq.d/`, then SIGHUPs FTL
  - wildcard subdomains work natively: `address=/.s.local/172.20.0.80`
  - + built-in ad-blocking with web UI for DNS query log and blacklist inspection
  - + well-maintained Docker image (`pihole/pihole`)
  - + SIGHUP reloads custom.list without restart (not inotify-like, but fast)
  - - heavier than plain dnsmasq (PHP web UI, FTL daemon, lighttpd — ~100MB image)
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
- <https://github.com/smaramwbc/statewave> (Apache-2.0)
  - Postgres+pgvector + LiteLLM; compile-then-bundle determinism; signed ULID-addressable receipts (`POST /v1/receipts/{id}/replay`); sensitivity labels + policy engine; MCP connector + GitHub/Slack/Notion/Gmail/n8n/Zapier/Markdown connectors
  - + strongest **provenance and determinism** in this category — same query + same time → same bytes
  - + CPU-only API (no GPU), self-hosted on the user's Postgres
  - + REST + Python/TS SDKs; Apache-2.0; Docker Hub `statewavedev/statewave`
  - + receipt/replay model fits `shoggoth.py`'s existing `Update basic memory with any new information` post-step as an auditable decision log
  - - compile-then-bundle model reshapes the write path the agent uses today
  - - no UI surface advertised; relies on operator-built UI or external MCP clients
  - - separate Postgres+pgvector cluster adds an ops surface shoggoth doesn't have today
- <https://github.com/caura-ai/caura> (Apache-2.0, formerly MemClaw)
  - FastAPI + Postgres 16 + pgvector + optional Redis; native MCP at `/mcp` (Streamable HTTP); hybrid search (semantic + keyword + 2-hop knowledge graph); 8-status lifecycle (`candidate → staged → active`, plus `rejected/quarantined/stale/deprecated`); contradiction detection + crystallization; trust tiers and `scope_agent`/`team`/`org` governance
  - + by far the most sophisticated governance and contradiction control in this category
  - + native MCP at `/mcp` slots into LiteLLM the same way basic-memory does
  - + Apache-2.0, GHCR images, air-gappable
  - - fleet-shaped (cited production reference: 300+ agents, 26k+ memories); shoggoth currently has ~1 agent (`shoggoth.py`) — most fleet features are unused
  - - reads more naturally as the source-of-truth for *many* agents than as the persistent notes of one
  - - OSS UI is the MCP/API only; the graph/timeline inspectors live in the Cloud tier
- <https://docs.cognee.ai/> (Cognee, Apache-2.0)
  - Python SDK (`pip install cognee`) plus MCP server; first-party integrations with Claude Code, Cursor, Codex, LangGraph, OpenAI Agents SDK; hosted platform at `platform.cognee.ai`
  - + explicit MCP support slots cleanly into the LiteLLM MCP gateway shoggoth already runs
  - + Apache-2.0, well-marketed in the AI-agent-memory space
  - - OSS release does not advertise a bundled UI / visualization — same observation gap as basic-memory
  - - the hosted "platform" appears to gate the UI behind Cloud, which is not shoggoth's open pattern
  - - default ingest pipeline requires an LLM call — couples memory writes to model availability
- <https://github.com/letta-ai/letta> (Letta, Apache-2.0; former MemGPT)
  - Self-improving agent whose memory, identity, and capabilities evolve with experience; npm-installable agent (`@letta-ai/letta-code`); Postgres-backed server
  - + most established research pedigree in the agent-memory category
  - + Apache-2.0
  - - merges memory layer into the agent runtime — shoggoth's `basic-memory` is intentionally decoupled from the agent runtime
  - - homepage does not advertise a UI for inspecting memory contents — humans would still read memory via API
  - - npm-first delivery shape is a mismatch for shoggoth's Python/Kestra pipeline
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

### Context compression

Tools that shrink the token volume flowing into/out of the LLM gateway
(`shoggoth/k3s/litellm.yaml`, currently `ghcr.io/berriai/litellm:v1.102.0`)
or the agent runtime. Two integration shapes exist:

- **Sidecar / proxy in front of LiteLLM** — sits between every
  LiteLLM request and the upstream provider, so every model shoggoth
  routes through gets compressed uniformly. Affects *all* clients
  (Qwen Code, `shoggoth.py` agent, MCP servers, any future code agents).
- **Inline / SDK in the agent runtime** — wraps individual agent
  calls; more granular but requires code changes per call site and
  breaks when new agents (or other tools) are added.

shoggoth's value proposition (LiteLLM as the single LLM gateway) makes
the sidecar shape the natural integration point: one deployment covers
every consumer, no per-client rewiring, and the same compression policy
applies to ad-hoc operator sessions via `ANTHROPIC_BASE_URL` /
`OPENAI_BASE_URL` redirects through LiteLLM.

Common to all candidates: they claim 20–95% input token reductions and
~5–75% output token reductions, run locally, do not call out to
third-party models for compression, and preserve prompt-cache prefixes
(Anthropic `cache_control` markers are explicitly excluded from
rewriting). None are currently deployed in shoggoth — selection is a
follow-up decision.

- **[Headroom](https://github.com/headroomlabs-ai/headroom)** (selected, Apache 2.0)
  - <https://docs.litellm.ai/docs/proxy/headroom>
  - Deployed in-cluster as a headroom Service + Deployment in
    `shoggoth/k3s/headroom.yaml`; wired as a `pre_call` guardrail in
    `shoggoth/k3s/litellm.yaml` (in the `litellm-templates` ConfigMap
    under `guardrails:`). Model cache lives on a `headroom-cache` PVC
    (`/data` → `HF_HOME=/data/huggingface`) so the Kompress-v2-base
    + Rust-core embedder + ONNX runtime survive pod restarts.
    `default_on: false`; opt-in per key via the LiteLLM virtual-key
    admin API (`POST /key/generate` with `"guardrails":
    ["headroom-compression"]`) or per request via the
    `guardrails`/`litellm_metadata.guardrails` body field. Bypass:
    `x-headroom-bypass: true`. Headroom Service endpoints are awaited
    by the litellm `wait-bootstrap` init container before the litellm
    pod becomes Ready, so the first request after a cold start does
    not race headroom's model download.
  - **Image pin:** `ghcr.io/headroomlabs-ai/headroom:code-nonroot-4630602`
    (short-SHA pin to the most recent `code-nonroot` build;
    v0.41.0 / 2026-10-14). Headroom does **not** publish semver
    image tags — the docs' `<version>` placeholder form does not
    exist in the registry. Only moving aliases (`latest`, `nonroot`,
    `code-nonroot`, ...) and short-SHA pins are published. The
    `code` extra adds tree-sitter for AST-aware code compression,
    which is the dominant traffic shape for shoggoth's `shoggoth.py`
    agent (PR review, code generation, the `commands/pr_update.py`
    review-comment flow). To upgrade, bump the SHA in
    `shoggoth/k3s/headroom.yaml` and rerun `make up`; the latest
    SHA is on
    <https://github.com/headroomlabs-ai/headroom/pkgs/container/headroom>.
  - Local compression layer for tool outputs, logs, files, RAG chunks, and
    conversation history; ships as a Python library, a TS SDK, a CLI, a
    drop-in `headroom proxy --port 8787`, an MCP server
    (`headroom_compress`, `headroom_retrieve`, `headroom_stats`), and a
    `headroom wrap` command for Claude Code / Codex / Grok / Copilot CLI
    / Cursor / Aider / OpenCode / Cline / Continue / Goose / OpenHands
  - Native LiteLLM integration as a **`pre_call` guardrail**
    (<https://docs.litellm.ai/docs/proxy/headroom>): registered in
    `config.yaml` under `guardrails:`, gated per-key, per-request, or
    globally via `default_on: true`; LiteLLM v1.92.x+ is required, and
    shoggoth already runs v1.102.0
  - Rust core + Python wrapper; published image
    `ghcr.io/headroomlabs-ai/headroom:latest` (Dockerfile in repo); one
    line for the sidecar: `headroom proxy --host 0.0.0.0 --port 8787`
  - Compression pipeline: `CacheAligner` (never rewrites
    `cache_control`-marked messages) → `ContentRouter` (Magika-based
    type detection) → `SmartCrusher` (JSON) / `CodeCompressor`
    (tree-sitter AST for 8 languages) / `Kompress-v2-base` (HF text
    model) → CCR (Compress-Cache-Retrieve) so the model can re-pull
    original bytes via `headroom_retrieve`
  - **Live-zone compression** keeps the frozen prefix byte-identical so
    Anthropic prompt-cache hits still apply; **output shaper**
    (`HEADROOM_OUTPUT_SHAPER=1`) appends a terseness hint to the system
    prompt and downshifts `reasoning_effort` / `thinking.budget_tokens`
    on routine turns (claimed ~31.7% output-token reduction)
  - Cross-agent shared memory across Claude / Codex / Gemini / Grok with
    dedup; `headroom learn` mines failed sessions and writes
    corrections to `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `GROK.md`
  - **Reported benchmarks** (run with
    `uv run python benchmarks/index_proof_table.py --seed 20260902`):
    21% on 100-result code search, 57% on SRE incident debugging, 42%
    on codebase exploration, 30% on GitHub issue triage; p50 0.21 ms
    on 10K-token JSON, 1.4 ms at 100K tokens
  - **Reported accuracy**: GSM8K unchanged, TruthfulQA +0.03 (within
    CI), SQuAD v2 97% at 19% compression, BFCL 97% at 32% compression
  - + most active project in the space (~75.5k stars, 3.3k commits,
    Apache 2.0) with first-class LiteLLM support — wire-up matches
    shoggoth's existing guardrail pattern in
    `shoggoth/k3s/litellm.yaml`
  - + one sidecar covers every consumer of `litellm.s.local`; per-key
    opt-in via the existing LiteLLM virtual-key admin API; per-request
    bypass via `x-headroom-bypass: true` for debugging
  - + output shaper reduces the verbose Qwen Code / Claude Code replies
    that dominate shoggoth's `pr_update.py` review-comment turnaround
  - + multi-agent memory + `headroom learn` would slot into the
    `Update basic memory with any new information` post-step that
    `shoggoth.py` already runs after every agent invocation
  - + Kompress-v2-base is HF-published, ONNX-runtime backed, can run
    CPU-only; no GPU requirement
  - - **bypass header is one-way**: `x-headroom-bypass: true` only
    turns compression *off*; cannot enable it inline — must go through
    the guardrail/key/metadata path
  - - **`user`/`system` messages are not compressed by default** — the
    default deployment compresses effectively none of Claude Code's
    `user`-role traffic. Opt-in via
    `HEADROOM_COMPRESS_USER_MESSAGES=1` on the Headroom container
    itself (not in LiteLLM's `config.yaml`), and the option is in a
    single env var shared by every model — coarse for the
    mixed-traffic shape shoggoth has
  - - **Anthropic `cache_control` messages are never compressed** —
    `CacheAligner` rejects them with no override; any request that
    uses prompt caching pays the full prefix cost (still reduces
    everything outside the cache, but the win is bounded by what is
    *not* cache-controlled)
  - - **`pre_call` only** — responses are never compressed; output
    shaping is the separate `HEADROOM_OUTPUT_SHAPER=1` flag
  - - adds one network hop per request to the sidecar, visible as an
    extra `pre-call` step in the LiteLLM Admin UI
  - - `default_on: true` would compress every request, including the
    ad-hoc operator sessions routed through the same proxy — rollout
    docs recommend per-key opt-in; per-key issuance is an additional
    admin workflow
  - - repo is large (Rust + Python + TS workspaces); HF model and
    ONNX runtime add ~hundreds of MB to the sidecar image and require
    AVX2 on x86
- <https://github.com/fkiene/llmtrim> (MPL-2.0)
  - Local MITM-style proxy (modeled on mitmproxy) that compresses
    LLM API requests before they reach providers (OpenAI, Anthropic,
    Google, DeepSeek, Mistral, xAI, Moonshot, Zhipu, Qwen,
    OpenRouter, any OpenAI-compatible endpoint)
  - **No LiteLLM integration** — sits in front of the provider, not in
    front of LiteLLM. Integrations are: a local proxy honoured by
    `HTTPS_PROXY`, language libraries (Rust crate, Python `pip install
    llmtrim`, Ruby gem, Kotlin, SwiftPM, `@llmtrim/js` WASM), an MCP
    server (`llmtrim_compress`, `llmtrim_compress_text`, `llmtrim_stats`),
    a CLI (`llmtrim status`, `llmtrim send`, `llmtrim compress`),
    and a desktop tray app
  - Installs a name-constrained private CA in `~/.llmtrim/` (cannot
    intercept bank/email traffic) and sets `HTTPS_PROXY` in the shell
    environment; daemonised at login
  - **10 compressor stages**: tool-output (lossless template fold +
    log/diff/grep windowing), cache discipline (stabilise invariant
    prefix), BM25+RM3+TextTiling retrieval, tree-sitter
    `skeletonization` (14 languages), JSON minify / TOON / CSV
    encoding, JSON sample down-sampling, dedup, output control
    (Chain-of-Draft + terse instruction + anti-overthink), static
    tool selection + description trim, image downscaling
  - **No model calls** — compression is deterministic and quality-gated
    (each stage re-measured with the provider's real tokenizer; reverted
    if it does not save tokens). Worst case is zero savings — cannot
    raise the bill or break a request
  - **Reported savings** (live A/B across 112 cases): input −31%,
    output −74%, round-trip cost −66% (projected −57% at GPT-4o rates,
    −59% at Claude Sonnet rates). Reported accuracy preserved on
    TruthfulQA, SQuAD v2, BFCL; GSM8K shows −8pp at −71% cost
  - + no model calls in the compression path — no GPU, no HF model,
    no ONNX runtime, single Rust binary, ~5 ms/call
  - + name-constrained private CA (cannot intercept bank/email
    traffic) is a meaningful security improvement over an unrestricted
    MITM proxy
  - + wide library coverage (Rust/Python/Ruby/Kotlin/Swift/JS-WASM)
    means a future shoggoth service written in any of these can embed
    compression directly
  - + published image `ghcr.io/fkiene/llmtrim` (`docker run -d -p
    43117:43117 -v llmtrim-state:/data ghcr.io/fkiene/llmtrim`)
  - + deterministic + quality-gated pipeline means the worst-case
    behaviour is "no change" — safer to enable globally than ML-based
    compressors
  - - **no LiteLLM integration** — to use it with shoggoth, every
    consumer (LiteLLM, MCP clients, the `shoggoth.py` agent, operator
    sessions) would point at llmtrim instead of LiteLLM, and the
    `shoggoth/k3s/litellm.yaml` model-list / virtual-key / cost-tracking
    stack stops being the single source of truth
  - - **MITM CA model** is incompatible with the Anthropic prompt-cache
    design (which relies on the SDK sending exact bytes to the
    endpoint) — would need a separate `anthropic-` shim that bypasses
    the proxy for `cache_control`-marked requests, or accept losing
    prompt caching entirely
  - - **MPL-2.0 file-level copyleft**: the only file affected by
    linking is llmtrim itself; shoggoth's own code is unaffected, but
    it is the only entry in this list with a copyleft licence
  - - smaller project (~245 stars, 690 commits) than Headroom; single
    author, narrower ecosystem
  - - per-developer `HTTPS_PROXY` + CA-trust setup is an additional
    step on every workstation and inside every container (MCP server
    pods, runner pods) that wants to use it — fails closed if the env
    var is not set
- <https://docs.litellm.ai/docs/proxy/headroom> (same project as
  headroom above — first-class LiteLLM guardrail; the relevant
  integration details are folded into the headroom entry)
  - + the only one in this list with first-class LiteLLM guardrail
    support — wire-up matches the existing `guardrails:` mechanism
    in `shoggoth/k3s/litellm.yaml`
  - - same caveats as the headroom entry (no override for
    `cache_control` exclusion, `user`/`system` default exclusion,
    `pre_call`-only, one-way bypass header)
  - - `default_on: true` would compress every request through the
    proxy; the LiteLLM docs recommend per-key opt-in for rollout,
    which adds an admin workflow on top of the existing
    virtual-key issuance

### Other approaches considered

- **LiteLLM built-in callbacks** — LiteLLM ships a `Presidio` /
  `Lakera` / `prompt_injection` callback layer and an OTel exporter
  but no built-in token compressor. The closest built-in is the
  `success_callback` / `failure_callback` hooks in
  `litellm_settings.callbacks` (already used by shoggoth for
  `prometheus` and `otel` in `shoggoth/k3s/litellm.yaml`); these
  observe calls but do not transform the request payload
- **Anthropic prompt caching alone** — shoggoth's LiteLLM config
  does not currently use Anthropic `cache_control` markers; adding
  them reduces cost on repeated prefixes but is a different
  mechanism (it does not reduce the bytes that flow into the model,
  it just bills them at a lower rate). Orthogonal to compression —
  Headroom and llmtrim both preserve cache hits
- **Per-agent `compress(messages)` SDK calls in
  `shoggoth/workflow/scripts/shoggoth/agent.py`** — would work
  for the one agent that lives in that file, but every other
  consumer (LiteLLM-routed MCP servers, ad-hoc operator sessions,
  any future code agent) is bypassed. The sidecar shape is the
  single-deployment option; the inline shape is a per-call-site
  refactor
- <https://github.com/microsoft/llmlingua> — Microsoft's prompt
  compressor; research-grade (paper: "LLMLingua: Compressing
  Prompts for Accelerated Inference of Large Language Models"),
  no first-class proxy, no maintenance cadence suitable for a
  production deployment
- <https://github.com/ajndkr/token-compressor> /
  <https://github.com/mangiucugna/json_reducer> — ad-hoc one-off
  compressors without proxy/SDK/MCP shapes; not viable as
  shoggoth infrastructure
- **Provider-side context management** (Anthropic's
  `cache_control` / OpenAI's automatic prefix caching) — reduces
  *cost* but not *bytes sent*; the LLM still spends the same
  forward-pass compute on the same input. Compressors reduce
  both; the two approaches are complementary, not substitutes
- **Self-hosted LLM via LocalAI + reduced context window** —
  `shoggoth/k3s/localai.yaml` already runs LocalAI as the
  embedding / fallback provider; shrinking the model's
  `max_context_len` is a hard cap, not a compressor, and applies
  only to LocalAI-routed requests (not the Ollama / Anthropic
  cloud traffic that dominates `shoggoth-default`)

## Software forge

- **[Gitea](https://gitea.io)** (selected)
- <https://forgejo.org/compare-to-gitea/>

## CI runner

- **[Gitea Runner](https://docs.gitea.com/runner/)** (selected)
  - Runs jobs as ephemeral kubernetes pods (`kubernetes://` labels) — each workflow run is its own pod, scheduled by the cluster; no DinD socket, no privileged container, no per-registry host bind-mount on the runner
  - Job pod template in `shoggoth/k3s/gitea.yaml` (`gitea-runner-templates` ConfigMap) ships the shared `git-cred-bootstrap` sidecar + `init-git-cred-perms` initContainer from `slave-dind` / `slave-term` so ssh-agent + git-credential-cache are available inside every job pod via `SSH_AUTH_SOCK=/shoggoth/git-cred/ssh_auth_sock`
  - Job pods get `app.kubernetes.io/managed-by: gitea-runner` and the runner's UUID label so the idle cleanup pass can remove orphans; the runner ServiceAccount holds only the `gitea-runner-bootstrap-wait` Role (`get` on the `gitea-runner-token` secret + externalsecret, used by the `wait-bootstrap` initContainer) — no `pods` / `pods/exec` / `pods/log` verbs, no access to other secrets. The runner reaches its own `gitea-runner-token` via a Secret volume mount, not the Kubernetes API; a runner-pod compromise cannot wipe `ssh-slave-private-key` or `openldap-admin-password`
  - + eliminates the privileged DinD that the Gitea runner previously required (DinD stays in place for Kestra's Docker task runner)
  - + containerd pulls job images via the existing Zot mirror wiring — first cold pull still fans out to upstream registries (see Zot cons), but the runner pod itself is unaffected
  - + job pods share the same persistent `/cache` as `slave-term` and `slave-dind` (the single `shoggoth-shared-cache` PVC in `shoggoth/k3s/shared-cache.yaml`) — pip wheels, ccache, and `CCWS_CACHE` state survive between runs; actions/cache steps reach the runner's cache server via the headless `gitea-runner` Service in this namespace
  - + job pod runs with the project's standard hardening: `runAsNonRoot: true`, `runAsUser: 1000`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault` — same shape as `external-secrets.yaml` and `reloader.yaml`
  - - docker actions, `docker://` steps, and job-container `volumes` / `credentials` are unsupported; such workflows would need a separate DinD runner
  - - secrets/job logs visible to anyone with `get pods` / `get pods/log` in the namespace while a run is active (no DinD boundary)
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

- **[Argo Workflows](https://github.com/argoproj/argo-workflows)** (selected)
  - + native k8s pods for task execution (no DinD dependency)
  - + eliminated need for DinD socket (security benefit)
  - + simpler architecture for k8s-native workloads
  - + built-in webhook events endpoint (`POST /api/v1/events/{namespace}/{discriminator}`)
  - + `WorkflowEventBinding` CRDs with expr-lang selectors for event routing
  - + supports `hostNetwork: true` and `hostPath` volumes (equivalent to Docker task runner)
  - + `CronWorkflow` CRD for scheduled tasks
  - + `withItems` for iterating over lists (equivalent to Kestra's ForEach)
  - - narrower plugin ecosystem compared to Kestra
  - - no file-based flow loading (flows submitted via API/CRDs)
  - - no built-in webhook signature verification (needs proxy for Gitea's X-Gitea-Signature)
  - - no event deduplication (at-least-once delivery)
  - - webhook event queue default: 16 events, 4 workers (configurable)
- [Kestra](https://kestra.io/docs) (superseded by Argo Workflows)
  - <https://github.com/kestra-io/kestra>
  - + rich plugin ecosystem, webhook triggers, file-based flow loading, OTel
  - + Docker task runner with host socket access (networkMode: host)
  - - k8s task runner requires Enterprise edition (plugin-ee-kubernetes)
  - - Docker task runner needs DinD socket (security concern, requires host volume mount)
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

## Backup

shoggoth is single-node k3s with stateful PVCs (Gitea repos, Redmine DB+files,
OpenBao KV, Kestra state, basic-memory markdown) and several databases behind
them. A single-layer backup is fragile — the practical posture is layered:
storage-layer snapshots for fast restore, cluster-level for resources+secrets,
app-aware dumps for Gitea/Redmine, and at least one encrypted off-host copy.

### Storage-layer (PV snapshots)

- **[Longhorn](https://github.com/longhorn/longhorn)** (recommended)
  - In-cluster CSI for k3s; per-volume snapshots + S3/NFS backup target; fast per-volume restore
  - + one tool handles provisioning and snapshot/restore — fewer moving parts than a CSI + Velero split
  - + rwx/single-node mode fits shoggoth's topology
  - + snapshots ship to any S3-compatible bucket (B2, Wasabi, MinIO, S3), independent of Velero
  - - backs up PVs only — does not cover Deployments, ConfigMaps, secrets, or Kestra flows
  - - storage class `local-path` is **not** snapshot-capable; migrating PVCs onto a Longhorn StorageClass is required to cover everything
- <https://github.com/openebs/openebs> (Apache-2.0)
  - Mayastor and cstor engines; snapshot+clone stories; more engine choice than Longhorn
  - - more moving parts for the same outcome; smaller community
- <https://github.com/kastenhq/kasten-k10> (free ≤10 nodes / commercial)
  - + richer policy-based orchestration across apps
  - - closed-source platform for the paid tier; smaller on-prem footprint than Velero

### Cluster-level (resources + secrets)

- **[Velero](https://github.com/vmware-tanzu/velero)** (recommended)
  - + exports Deployments, ConfigMaps, Secrets, CRDs, and PVs (via Restic integration or CSI snapshots)
  - + `velero restore --from-backup` is a single command; Kestra flows and Grafana dashboard provisioning become recoverable from versioned archives
  - - **license fence**: confirm current Velero license and release activity after the Vmware/Broadcom ownership change before committing
- <https://github.com/kanisterio/kanister> (Apache-2.0)
  - Generic blueprints for app-aware snapshot/restore
  - - smaller ecosystem; per-app blueprint authoring

### File-level (mounted volumes)

- **[Restic](https://github.com/restic/restic)** (recommended)
  - + client-side encrypted, deduplicated, S3-compatible (B2, Wasabi, Storj, S3, MinIO, Hetzner Storage Box)
  - + single static binary; fits the existing `slave:noble` Argo task runner pattern
  - + automatic pruning/retention; bucket credentials stored in OpenBao
  - + natively orchestratable as new tasks in `main_shoggoth_maintenance.yml`
  - - no GUI; CLI + Restic REST server only
- <https://github.com/kopia/kopia> (Apache-2.0)
  - + GUI client; same encryption/dedup story as Restic
  - - less Docker-deployment culture than Restic
- <https://github.com/borgbackup/borg> (BSD-3-Clause)
  - + battle-tested deduplication; mature ecosystem
  - - newer B2/Wasabi paths less field-tested than Restic's
- <https://github.com/borgbackup/borgmatic> (GPL-3.0)
  - Configuration wrapper for Borg with sensible retention defaults
  - - thin layer over Borg; value is mostly configurability

### Database-aware

- **[pgBackRest](https://github.com/pgbackrest/pgbackrest)** / [Barman](https://github.com/EnterpriseDB/barman)
  - + dedicated Postgres PITR + WAL streaming; reliable incremental restores
  - - confirm DB engine (Postgres / MySQL / MariaDB / SQLite) for Gitea / Redmine / CDash before picking
- <https://github.com/MariaDB/MariaBackup> / [Percona XtraBackup](https://github.com/percona/percona-xtrabackup)
  - Hot backups for MySQL/MariaDB; equivalent PITR story for those engines
- <https://github.com/benbjohnson/litestream> (MIT)
  - + continuous WAL streaming of SQLite to S3/NFS — ideal for any SQLite DB inside shoggoth (CDash and similar)
  - + near-zero overhead; runs as a sidecar
  - - SQLite only; amd64 upstream default
- [`gitea dump`](https://docs.gitea.io/en-us/command-line/#dump)
  - Version-aware repos + DB + avatars tar; ships in Gitea itself, no extra dependency
  - + simplest single-shot backup for the Gitea pod; pairs naturally with Restic inside a Kestra task
- [Redmine backup recipe](https://www.redmine.org/projects/redmine/wiki/RedmineBackup)
  - DB dump + files tar; same pattern as `gitea dump`

### Off-host targets

shoggoth is single-node; an off-host copy is what makes a snapshot an actual backup.

- **[Backblaze B2](https://www.backblaze.com/b2/)** (recommended default)
  - + cheapest S3-compat with long-tail storage tiers (`B2 Cloud Storage`)
  - + Cloudflare Bandwidth Alliance egress — free restore behind Restic
  - + compatible with Restic, Kopia, Borg (`s3` endpoint), Longhorn backup target, Velero backup target
- <https://wasabi.com/> — no egress fees, predictable pricing, S3-compat
- <https://www.storj.io/> — decentralized, EU residency, S3-compat
- <https://www.hetzner.com/storage/storage-box> — cheap EU B2-style box (rsync, borg, sftp, sshfs)
- <https://rsync.net/> — managed borg space; durable, restore-friendly
- <https://www.tarsnap.com/> — managed encrypted borg-style archive
- <https://www.borgbase.com/> — managed borg space with a small UI
- <https://min.io/> — self-hostable S3-compat target; in-house bucket but **not** off-host by itself

### Layered posture for shoggoth

The intended posture (no tool selected today):

1. Longhorn to snapshot PVCs and ship snapshots to B2/Wasabi
2. Velero for cluster resources (Deployments, ConfigMaps, Secrets, Kestra flows, Grafana provisioning)
3. Restic orchestrated as Kestra tasks using `slave:noble` — daily `gitea dump` + DB dumps + `/shoggoth/data` files tar to B2
4. Litestream on any SQLite PVC (CDash and similar)
5. Periodic second off-host copy to Hetzner Storage Box (cheap)
6. OpenBao unseal keys stored out-of-band (paper or second human); OpenBao has `bao operator raft snapshot` for the consensus store

Open items:
- migrate `local-path` PVCs onto a Longhorn StorageClass so all PVs become snapshot-capable
- confirm DB engine for Gitea/Redmine/CDash before locking the DB-aware tool
- store Restic/Velero bucket credentials in OpenBao, not in plain env on the workflow runner

## Notifications

shoggoth's services already produce a rich event stream (Kestra executions,
Gitea PR/CI webhooks, Redmine issues, OpenBao unseal events, basic-memory
write events), but humans currently have no aggregated view — failures
only land in OTel/Loki traces. The Kestra flow skill's `## Example prompts`
list (originally embedded in `shoggoth/k3s/web-internal.yaml` as the
`kestra-flow-skill.md` ConfigMap data key, now removed since the migration
to Argo Workflows) contains a
string used as an LLM-prompt example —
`"Add a Slack notification task to this existing flow when any task fails"` —
that effectively describes the same notification gap, but no `# TODO`
comment or work-item exists in the codebase behind it; this exact gap
remains open with no in-tree tracker.

The practical posture is a **notification router** (Apprise) feeding
**one chat platform** as the primary destination, with the chat platform
selected per the team's threading/search needs.

### Notification routers (fan-out)

- **[Apprise](https://github.com/caronc/apprise)** (MIT, recommended)
  - + Python + CLI; 100+ targets (Zulip, Slack, Discord, Mattermost, Teams, XMPP, email, SMS, push, webhooks)
  - + one CLI call (`apprise -t SUBJECT -b BODY --tag shoggoth`) fits the existing `slave:noble` Argo runner
  - + destinations declared in `apprise.yml`, stored in OpenBao; chat-platform choice is *not* baked into workflow code
  - - no GUI; CLI only
- <https://ntfy.sh/> (Apache-2.0)
  - + HTTP pub-sub only, very lightweight, mobile + web clients
  - - no thread/archive model; useful as a *secondary* destination (mobile push), not as the primary chat
- <https://gotify.net/> (MIT)
  - + server + Android client + CLI
  - - one-way push; same shape as ntfy with a smaller ecosystem

### Self-hosted chat platforms (CI/DevOps notifications)

- **[Zulip](https://zulip.com)** (Apache-2.0, recommended)
  - + Apache-2.0, Docker deploy (`zulip/docker-zulip`), admin console; mature bot API and incoming webhooks
  - + **stream × topic** two-axis model is purpose-built for CI notifications: one stream per source service (`#argo`, `#gitea-pr`, `#gitea-ci`, `#redmine`, `#openbao`, `#basic-memory`, `#backup`), one topic per incident
  - + strong search across projects; mobile + desktop clients; full message history
  - - integrations catalog covers GitHub/GitLab/Jenkins/Sentry/etc. but **no first-party Gitea integration** — wiring goes through Apprise + generic webhooks
- <https://mattermost.com/> (MIT server, proprietary mobile SDKs)
  - + Slack-compatible UI; familiar to most operators; voice channels in recent versions
  - + Apache-2.0-equivalent server, Docker deploy, broad integrations
  - - mobile clients use proprietary SDKs
  - - threading is bolted on to Slack channels; less native than Zulip's stream × topic for CI
- <https://element.io/> (Apache-2.0)
  - + Matrix is federated (server-to-server); E2EE by default for rooms; rich mobile clients
  - + bridging to Slack/Discord/XMPP/IRC available
  - - persistent thread/search UX is not as first-class as Zulip
- <https://rocket.chat/> (SSPL newest / MIT older)
  - + Slack-compatible + customer-support livechat built in (omnichannel routing)
  - - SSPL license restricts SaaS distribution; redeploy-the-license checks apply

### XMPP / Jabber (federated alternative)

- <https://prosody.im/> (MIT/X11, recommended among XMPP servers)
  - + MIT; ships in Debian/Ubuntu; lightweight Lua module API
  - + mature XEP support: MUC (rooms), MAM (history), PubSub (fan-out), OMEMO (E2EE), MUC sub (room topics)
  - + actively maintained (13.x line shipped 2026); most-used Prosody deployment is `meet.jit.si` (Jitsi)
  - + same Prosody instance can back Jitsi Meet if shoggoth later adds audio/video
  - - channel-style threading in the Zulip sense is built from MUC + MUC sub; workable but not first-class
  - - mobile clients vary (Conversations, Gajim, Dino, Monal) — pick-and-test rather than get one polished triple
  - - Apprise reaches XMPP through a server-side XMPP bot, not a plain incoming webhook
- <https://github.com/igniterealtime/Openfire> (Apache 2.0)
  - + Java XMPP server; admin web UI; long-running enterprise deployments
  - - Java overhead; community-maintained only; less momentum than Prosody
- <https://github.com/processone/ejabberd> (GPL-2.0)
  - + Erlang XMPP server; very high concurrency; commercial MongooseIM fork exists
  - - GPL-2.0 (not MIT/Apache); MongooseIM is AGPL-3.0
- <https://github.com/tigase/tigase-server> (AGPL-3.0)
  - + Java XMPP server with cluster-friendly design
  - - AGPL-3.0; heavier configuration footprint

### IM backend (embeddable chat, not a Slack replacement)

- <https://github.com/juggleim/im-server> (Apache-2.0)
  - + Go IM backend with multi-tenant `appkey` design; WebSocket + Protobuf long-connection; SDKs for Web/iOS/Android/PC/Flutter/HarmonyOS
  - + Apache-2.0, Docker Compose shipped, admin console on `:8090`, MySQL or MongoDB storage
  - - **not a Slack-substitute notification target** — it's an embeddable chat *engine* for an external app
  - - no first-party Slack-style incoming webhook; sending requires the server-side REST API on `:9001`
  - - desktop SDK (`imsdk-pc`) is not open-sourced yet
  - - cluster/horizontal scaling (>1 node) is gated behind the paid *Professional Edition*

### Live meetings (complementary)

- <https://jitsi.org/> (Apache-2.0)
  - + Jitsi Meet + Jitsi Videobridge + Jicofo + Jigasi; web + mobile; no account required
  - + complements a chat platform for "let's hop on a call about this incident"
  - - text chat is in-call only — **not** a persistent channel model; pair with a chat platform rather than replace
  - - uses Prosody internally as the XMPP signaling server (`meet.jit.si`)

### Layered posture for shoggoth

The intended posture (no chat platform deployed today):

1. **Apprise** as the fan-out router inside each Argo task runner (`slave:noble`); tag config in OpenBao as `shoggoth/apprise/tags.yml`
2. **Zulip** as the first destination — one stream per source service, one topic per incident; this addresses the notification gap noted above (no in-tree TODO exists at `web-internal.yaml:669` — only an LLM-prompt example string that was once in the deleted `kestra-flow-skill.md` ConfigMap data key).
3. **`shoggoth.py` extension** — new step `setup_notification_streams` (alongside `setup_argo_webhooks`) to provision the stream set on first bringup
4. **`main_shoggoth_maintenance.yml` extension** — a final task running `apprise --tag shoggoth ...` after each maintenance step; same runner, same OTel env block, no edits to existing tasks
5. **`onFailure` notification tasks** on `gitea-pr-update`, `gitea-ci-failure`, `redmine-task-processor` — flows where the deleted kestra-flow-skill example prompt expected a Slack-style task
6. Apprise tag config in OpenBao (`shoggoth/apprise/tags.yml`) keeps the chat-platform choice swappable: switching to Mattermost or XMPP later is a one-config change, not a refactor of Argo Workflows

Open items:
- pick Zulip vs Mattermost vs XMPP+Prosody based on team preference; Apprise stays as the bus regardless
- confirm whether local LDAP users need to be mirrored into the chat platform; OpenBao already stores the admin token per service so the same flow can be reused
- if audio/video incident response is later wanted, pair Prosody (XMPP) with Jitsi Meet — Prosody can serve both the chat and the Jitsi signaling

# Agentic Workflows

## Code indexing

- **<https://github.com/DeusData/codebase-memory-mcp>** [selected]
  - - lacks transparency
- <https://github.com/safishamsi/graphify>
  - - wants LLM
- <https://github.com/vitali87/code-graph-rag>
  - - lacks in language support
- <https://github.com/tirth8205/code-review-graph>
  - + embeddings
  - - lacks in language support
  - - broken visualization
- LSP
  - - compile commands are needed for C++

## Container orchestration

- **[Docker Compose](https://docs.docker.com/compose/)** (selected, current)

### Lightweight Kubernetes / container orchestration platforms

Motivation: simplify initial deployment, automate service grouping and isolation
via namespaces and network policies, leverage k8s-native tooling for monitoring
(Prometheus Operator, OTel k8s receivers), inspection (kubectl, k9s), updates
(GitOps, Helm), and control (declarative manifests, rolling updates). The
existing OTel/Grafana/Loki/Tempo/VictoriaMetrics stack and OpenBao are already
k8s-compatible and would transition cleanly.

- <https://github.com/k3s-io/k3s/> (Apache 2.0) — **recommended**
  - Single binary (<100 MB), fully CNCF-conformant Kubernetes; SQLite default datastore (embedded etcd for HA)
  - Batteries-included: Traefik ingress, CoreDNS, local-path-provisioner, Klipper load balancer, Metrics Server, Helm controller — all bundled and swappable
  - One-line install: `curl -sfL https://get.k3s.io | sh -`; auto-deploys manifests from `/var/lib/rancher/k3s/server/manifests/`
  - Seamless single-node to multi-node: add workers with `curl -sfL https://get.k3s.io | K3S_URL=https://server:6443 K3S_TOKEN=xxx sh -`
  - ~512 MB RAM footprint; x86_64, arm64, armhf, s390x
  - 33K+ stars, Apache 2.0, backed by Rancher/SUSE; largest community of any lightweight k8s — most troubleshooting answers exist
  - Helm controller deploys charts as CRDs (`HelmChart` custom resources) — no separate Helm installation needed
  - + Traefik ingress replaces Angie for HTTP routing/TLS; CoreDNS replaces Unbound for in-cluster DNS
  - + local-path-provisioner provides dynamic PVCs from host directories — replaces Docker named volumes
  - + namespaces provide natural service grouping (infra, monitoring, ci, ai, pm, auth)
  - + network policies (Kube-router) enable isolation between service groups
  - + native secret management via K8s secrets + External Secrets Operator for OpenBao integration
  - + OTel collector runs as a DaemonSet with k8sclusterreceiver + kubeletstatsreceiver — replaces Docker-specific receivers
  - + Helm charts exist for Grafana, Loki, Tempo, VictoriaMetrics, Gitea, Redmine, PostgreSQL, OpenBao — reduces custom manifests
  - + GitOps (Argo CD, Flux) for automated sync from Git to cluster — replaces rsync + compose up
  - + rolling updates and health-based rollout are native — no more `pull && up -d` restarts everything
  - + k9s, kubectl, lens provide rich inspection and control of running workloads
  - - Traefik ingress has different config model than Angie — web-internal API gateway logic (token injection, WebDAV, PyPI cache) needs migration to Ingress annotations + Traefik middleware or a sidecar
  - - slave-dind needs rethinking: buildkit for image builds, or DinD as a privileged pod with emptyDir
  - - wg-easy needs host-network pod or NodePort; VPN-to-cluster-DNS routing requires CoreDNS NodeLocal cache or externalDNS
  - - docker-cache and docker-registry become less useful with k8s-native image pulling (but still valuable for build cache)
  - - bringup container → k8s Job or Helm hook; init ordering via Jobs with `ttlSecondsAfterFinished`
  - - 32 containers on a single node is well within k3s capacity, but resource requests/limits need careful tuning

- <https://github.com/k0sproject/k0s> (Apache 2.0-based, NOASSERTION license)
  - Single zero-dependency binary; CNCF certified; Konnectivity for controller-worker tunnel
  - Intentionally minimal: CoreDNS + Metrics Server + Kube-Router CNI only; no bundled ingress, storage, or LB
  - k0sctl provides one-command multi-node deploy, upgrade, backup, restore
  - 1 GB RAM minimum; x86-64, ARM64, ARMv7, RISC-V
  - 6.4K stars; active community with office hours
  - + cleanest k8s base — full control over every component, no surprise bundled addons
  - + k0sctl is the best multi-node lifecycle tool among all options
  - + Konnectivity means workers don't need exposed API server ports — good for VPN-only setups
  - + control plane isolation by default (separate from workload pods)
  - - no bundled ingress or storage provisioner — must install Traefik/NGINX + local-path manually
  - - more upfront work than k3s for equivalent functionality
  - - smaller community means fewer troubleshooting resources for edge cases
  - - NOASSERTION license should be reviewed before adoption (appears Apache 2.0-based with modifications)
  - - no Helm controller — Helm must be installed separately

- <https://github.com/canonical/microk8s> (Apache 2.0)
  - Snap-packaged Kubernetes; 30+ curated addons (DNS, ingress, storage, dashboard, Prometheus, Grafana, MetalLB, cert-manager, GPU, Helm, registry, Istio, Linkerd, Knative)
  - One-line install: `sudo snap install microk8s --classic`; addons via `microk8s enable <name>`
  - ~500–800 MB RAM (snap overhead); primarily Ubuntu/x86_64
  - 9.3K stars; backed by Canonical
  - + richest addon ecosystem — monitoring stack (Prometheus + Grafana) and dashboard in two commands
  - + GPU addon for NVIDIA operator — relevant for LocalAI Vulkan/GPU workloads
  - + registry addon for private Docker registry — replaces standalone docker-registry service
  - + automatic snap updates (can be disabled)
  - - snap dependency: not available on all distributions, adds overhead, auto-updates can be disruptive
  - - snap is Ubuntu-centric; on non-Ubuntu hosts requires snapd installation
  - - multi-node clustering via addon is less seamless than k3s or k0s
  - - snap auto-updates can break cluster compatibility with add-on versions
  - - snap confinement may interfere with host path mounts for persistent storage

- <https://github.com/hashicorp/nomad> (BUSL-1.1)
  - Single binary scheduler for containers, VMs, and raw executables; embedded BoltDB state store
  - Built-in web UI for job/allocation/node inspection; Prometheus-compatible metrics
  - First-class Vault integration for secrets (template blocks, dynamic secrets, lease renewal)
  - Scales to 10K+ nodes; multi-region, multi-cloud federation
  - 16.8K stars; created 2015; actively maintained
  - + built-in web UI — no separate dashboard installation needed
  - + handles mixed workloads (containers + VMs + raw binaries) — most flexible orchestrator
  - + native Vault/OpenBao integration via `vault` blocks in job specs — cleanest secret injection model
  - + optimistically concurrent scheduler — high throughput for batch workloads (CI, Kestra flows)
  - - HCL job format is a completely different paradigm from Kubernetes YAML/Helm — significant learning curve
  - - no bundled DNS, ingress, storage, or load balancer — requires Consul + Fabio or external components
  - - full stack (Nomad + Consul + Vault) adds complexity and resource overhead
  - - BUSL-1.1 license restricts competitive use — not OSI-approved open source
  - - smaller ecosystem of Helm charts and community manifests compared to k8s
  - - shoggoth's existing tools (OTel, Grafana, VictoriaMetrics) have first-class k8s support but Nomad integration is less documented
  - - migrating from Nomad back to k8s (or vice versa) is harder than between k8s distributions

- <https://github.com/portainer/kubesolo> (MIT)
  - Ultra-lightweight single-node Kubernetes; <200 MB RAM; SQLite via Kine; custom NodeSetter scheduler (no standard scheduler)
  - Docker-compatible API (d2k) translates Docker API calls to Kubernetes — potential migration bridge
  - One-line install; ARM, ARM64, x86_64, RISC-V; local-path storage built-in
  - 531 stars; created May 2025; backed by Portainer
  - + lowest RAM footprint of any k8s distribution — suitable for resource-constrained hosts
  - + d2k Docker-compatible API could ease migration from Docker Compose
  - + Portainer Edge integration for remote management
  - - strictly single-node — no HA, no multi-node scaling path
  - - very young project (2 months old at time of writing) — limited production track record
  - - no bundled ingress controller or monitoring stack
  - - small community — limited troubleshooting resources
  - - no Helm addon catalog; standard Helm works but no curated charts
  - - custom NodeSetter scheduler replaces standard scheduler — some k8s features may behave differently

---

## Cluster visualization

Tools that produce static or interactive diagrams of the shoggoth stack
from the manifests in `shoggoth/k3s/*.yaml` (and the Argo CRD submodule
in `shoggoth/argo-workflows/manifests/base/crds/full/`). The choice is
between two operating modes:

- **Offline (manifest files only)** — read YAML, no cluster access needed.
  Output is reproducible and reviewable as part of a PR. Matches the
  existing `docs/architecture.svg` / `docs/bringup.svg` pattern (both
  are manifest snapshots, hand-rendered).
- **Live (kubeconfig / cluster API)** — query the running cluster, show
  actual state including drift. Cannot run in CI without a live cluster;
  output is not reproducible.

A leading offline candidate is KubeDiagrams (see below). Selection
pending — this document is research only.

### Offline (manifest files)

- <https://github.com/philippemerle/KubeDiagrams> (Apache 2.0)
  - + the de-facto manifest visualizer — 2.7k stars, actively maintained
  - + reads Kubernetes manifest files, kustomize output, Helm charts,
    helmfile descriptors, OR a live cluster (hybrid input)
  - + output formats cover the entire proposal set: SVG, PNG, Mermaid,
    Graphviz DOT/dot_json, draw.io (editable), D2, PDF, GIF/JPEG/TIFF
  - + 51 built-in K8s kinds — Deployment, StatefulSet, Job/CronJob,
    Service, Ingress, ConfigMap, Secret, ServiceAccount, Role/ClusterRole
    + Bindings, HPA/VPA, NetworkPolicy, PDB, PVC/PV, Node, Namespace,
    HPA, ResourceQuota, LimitRange, PriorityClass, RuntimeClass, CRD,
    Validating/MutatingWebhookConfiguration, APIService — covers
    everything the shoggoth stack uses
  - + first-class CRD support with vendored example configs for the
    shoggoth-relevant CRDs: Argo (`Workflow`, `WorkflowTemplate`,
    `Application`, `Rollout`, `EventBus`, `EventSource`, `Sensor`),
    External Secrets (`ExternalSecret`, `SecretStore`), Prometheus
    kube-prometheus-stack, cert-manager, Knative, OpenShift, k0s,
    LeaderWorkerSet
  - + four edge kinds with distinct visual styles: `REFERENCE` (solid
    black, direct refs like `serviceAccountName` / `volumes` /
    `envFrom`), `SELECTOR` (dashed black, label/field selectors like
    `Service.spec.selector` and `NetworkPolicy.podSelector`),
    `OWNER` (dotted black, `ownerReferences` chain),
    `COMMUNICATION` (solid brown, NetworkPolicy ingress/egress)
  - + auto-clustering by namespace, then by `app.kubernetes.io/instance` /
    `helm.sh/chart` / `app.kubernetes.io/name` / `tier` / `component`
    — matches the shoggoth stack's labeling convention
  - + declarative custom diagrams — add clusters / nodes / edges in
    `.kd` files (see `examples/wordpress/custom_diagram.kd`); useful
    for adding shoggoth-specific annotations on top of the auto-graph
  - + multiple install paths: `pip install KubeDiagrams`,
    Docker (`philippemerle/kubediagrams`), Podman, Nix shell
    (`nix shell github:philippemerle/KubeDiagrams`), kubectl plugin
    (symlink `bin/kubectl-diagrams` into `$PATH` → `kubectl diagrams`),
    GitHub Action (`uses: philippemerle/KubeDiagrams@main`), and a
    JetBrains IDE plugin
  - + online demo at <https://kubediagrams.lille.inria.fr/> for previewing
    without installing
  - + supports stdin pipe (`kube-diagrams -` reads manifests from stdin)
    — useful for `kubectl kustomize path/ | kube-diagrams - -o out.svg`
    and `helmfile template -f helmfile.yaml | kube-diagrams - -o out.svg`
  - + interactive viewer from `dot_json` output: zoom, tooltips, filter,
    drag, save as PNG/JPG
  - + namespace filter (`-n`) and `--without-namespace` for cross-NS views
  - + the `.dot_json` output has tooltips on every cluster/node/edge
  - - pulls in `mingrammer/diagrams` as a Python dependency for the
    icon library, plus `graphviz2drawio` for draw.io output — heavier
    Python footprint than a from-scratch script
  - - requires `dot` (Graphviz) binary at runtime; the system has
    `/usr/bin/dot` available
  - - default output is PNG, not SVG — needs `-f svg` for SVG
  - - clustering by Helm chart can over-cluster when many resources
    share the same chart but live in different namespaces
  - - declarative `.kd` files use a custom DSL with limited docs;
    shoggoth-specific annotations would be a learning investment
- <https://github.com/dcasati/kubernetes-PlantUML> (MIT)
  - + uses PlantUML stencils for K8s resources — visually distinct
    style, embedded into VS Code rendering pipeline
  - + renders to any format PlantUML supports (PNG, SVG, PDF, LaTeX)
  - + reads manifest files directly (offline)
  - - no built-in support for CRDs (Argo, External Secrets, etc.) —
    would need a custom mapping
  - - requires PlantUML server or CLI as a render dependency
    (extra infrastructure beyond `dot`)
  - - 280 stars, less momentum than KubeDiagrams; last meaningful
    activity 2+ years ago
  - - VS Code extension model doesn't fit the Makefile-driven build
    pipeline; would need a separate CI integration
- <https://github.com/viralpoetry/kubesurveyor> (MIT)
  - + Python script, Graphviz SVG output
  - + namespace-grouped visualization
  - - 90 stars, last updated Sep 2024 — low maintenance momentum
  - - no CRD support for shoggoth-relevant kinds (Argo, ExtSec)
  - - no Helm / kustomize / hybrid input — manifests only
  - - output format is limited to Graphviz SVG (no Mermaid, draw.io,
    or other modern formats)
- <https://github.com/Gihchathur/kubemotion> (MIT)
  - + TypeScript / web app that takes YAML / Helm chart input and
    produces an Argo CD-style resource view
  - + actively maintained (~28 days ago at fetch time)
  - - Argo CD-style output is opinionated — assumes the user wants
    the same visual vocabulary as the Argo CD web UI
  - - requires `helm` + `node` runtime
  - - 31 stars, small user base
  - - limited CRD support compared to KubeDiagrams
- <https://github.com/jeremykross/konstellate> — **deprecated**; last commit Jul 2019, effectively unmaintained (Electron GUI, 1.5k stars at peak)
- <https://github.com/same7ammar/kube-composer> (MIT)
  - + YAML builder with dynamic visualization (web app)
  - + 482 stars
  - - GUI builder, not a visualizer — the diagram is a side effect of
    building YAML, not the primary output
  - - no CLI mode; would need to be wrapped to fit the Makefile pipeline
  - - no CRD mapping
- <https://github.com/holooooo/kube-topo> — **deprecated**; last commit Jun 2020, abandoned (4 stars, web page only)

### Live cluster (kubeconfig / API)

- <https://github.com/mkimuram/k8sviz> (MIT)
  - + Go binary, Graphviz output (DOT, PNG)
  - + uses Kubernetes community icons for the node shapes — visually
    consistent with the broader K8s ecosystem
  - + simple CLI: `k8sviz -k kubeconfig -n namespace -o out.dot`
  - - despite the name, this is a LIVE-CLUSTER tool — does NOT read
    static manifest files; queries the cluster via the discovery API
  - - output is DOT / PNG, not SVG by default (needs `dot -Tsvg` for SVG)
  - - limited CRD support — recognizes built-in kinds only
  - - 338 stars but only one maintainer, low activity
- <https://github.com/derailed/k9s> (Apache 2.0)
  - + 30k+ stars, the de-facto terminal UI for K8s
  - + TUI for live cluster inspection (pods, services, logs, exec)
  - - terminal UI, not a static diagram generator — doesn't produce
    the file artifacts needed for `docs/cluster.svg` review
  - - no manifest-file mode (operates on live cluster only)
- <https://github.com/vmware-tanzu/octant> (Apache 2.0)
  - + 6k stars, well-established web-based K8s dashboard
  - + plugin-based CRD support via the `octant-plugin` ecosystem
  - + resource graphs and event timelines
  - - web UI only — requires a browser to view, not a static artifact
  - - heavy dependency footprint for what should be a CI step
  - - heavyweight for the shoggoth project, which has no web-UI for
    infra today
- <https://github.com/lensapp/lens> (MIT)
  - + 23k+ stars, the most popular K8s desktop IDE
  - + comprehensive K8s management (live cluster, logs, exec, etc.)
  - - Electron desktop app — not a static diagram generator
  - - overkill for "produce one diagram from manifests"
- <https://github.com/hjacobs/kube-ops-view> — **deprecated**; moved to Codeberg Oct 2020, effectively unmaintained, license is GPL-3.0 (incompatible with the shoggoth repo's permissive licensing)
- <https://github.com/benc-uk/kubeview> (MIT)
  - + graphical graph representation of K8s resources and their
    relationships
  - + real-time updates via Server-Sent Events (SSE) — the graph
    refreshes without polling
  - + color-coded health (Red / Green / Grey), pod logs, events,
    search and filtering
  - + three install paths: standalone binary, container, OR
    in-cluster via the shipped Helm chart (sets up read-only RBAC
    automatically; uses the pod's ServiceAccount to talk to the
    API server)
  - + JSON API endpoints exposed for tooling reuse: `/api/namespaces`,
    `/api/fetch/{namespace}?clientID={cid}`, `/api/logs/{ns}/{pod}`,
    `/api/status`, and `/updates?clientID={cid}` for the SSE stream
  - + no built-in auth on the UI itself — relies on K8s API server
    RBAC (so it composes cleanly with shoggoth's existing
    namespace-scoped RBAC)
  - - live cluster only — no manifest-file mode
  - - web UI, not a static artifact
  - - the 8000 default port requires Ingress or `kubectl port-forward`
  - - note: a separate project `bencoates/kubeview` (~300 stars,
    less active) appears in some registries; that is a different
    tool — the actively maintained one is `benc-uk/kubeview`
- <https://github.com/steveteuber/kubectl-graph> (MIT)
  - + `kubectl` plugin distributed via krew — install with
    `kubectl krew install graph`, invoke as `kubectl graph ...`
  - + multiple output formats tuned to different consumers:
    Graphviz DOT (renderable to SVG via `dot -Tsvg`), Mermaid
    (for GitHub / Gitea Markdown rendering), Cypher for Neo4j
    (graph DB queries), and AQL for ArangoDB
  - + resolves resource dependencies before printing the graph —
    walks the reference graph (Pod → SA, Service → Pod labels,
    etc.) the same way KubeDiagrams does
  - + concise shorthand: `kubectl graph all -n N` dumps the
    whole-namespace reference graph in one line
  - - live cluster only — no manifest-file mode
  - - one-maintainer project, narrow scope (~low star count)
  - - output is stdout text (DOT / Mermaid / Cypher / AQL) — needs
    `dot`, a Mermaid renderer, or a graph DB downstream to view
- <https://github.com/derailed/popeye> (MIT)
  - + K8s cluster sanitizer / auditor
  - + text-based report with severity grading
  - - not a diagram generator — produces a sanity report, not a
    visual graph
  - - live cluster only
- <https://github.com/cilium/k8s-iptables-diagram> (Apache 2.0)
  - + visualizes kube-proxy iptables rules as a directed graph
  - - specific to kube-proxy internals — not a general cluster
    visualizer
  - - live cluster only (operates on `iptables-save` output)

### In-cluster services (Helm-deployed web UIs / dashboards)

Tools deployed as Services inside the cluster and exposed as web UIs.
Unlike the "Live cluster" section above (which runs on the operator's
laptop / outside the cluster), these require a `helm install` or
`kubectl apply`, consume cluster resources (a Deployment + Service
+ RBAC), and provide a team-shared browser view of cluster state.
Useful for debugging in-session but not for producing static diagrams
in the docs/ tree.

- <https://github.com/kubernetes-sigs/headlamp> (Apache 2.0)
  - + Kubernetes SIG project — the official lineage successor to
    `kubernetes/dashboard` (which was archived Jan 21, 2026)
  - + 7.5k stars, actively maintained (kubernetes-sigs org)
  - + in-cluster deployment via the shipped `charts/headlamp/` Helm
    chart, OR `kubectl apply -f kubernetes-headlamp.yaml` (the
    repo also ships `kubernetes-headlamp-ingress-sample.yaml` and
    a `monitoring.yaml` with a ServiceMonitor)
  - + extensible via the separate `headlamp-k8s/plugins` repo —
    graph / topology / CRD views are first-class plugins
  - + multi-cluster support (one UI, many kubeconfigs)
  - + RBAC-scoped write actions (create / update / delete) honour
    the user's K8s permissions — same auth model as shoggoth's
    existing namespace-scoped RBAC
  - - web UI only, not a static artifact — doesn't fit the
    shoggoth project's "diagram checked into docs/" workflow
  - - in-cluster deployment adds Deployment / Service / RBAC
    overhead — needs Ingress or `kubectl port-forward` to reach
  - - no offline / manifest-file mode
  - - **no diagram export** — graph / topology views are
    interactive in the browser only; no built-in SVG / PNG /
    Mermaid save
- <https://github.com/kubernetes/dashboard> — **archived** Jan 21, 2026;
  README redirects to Headlamp. Removed from the live list above.
- <https://github.com/skooner-k8s/skooner> (Apache 2.0)
  - + CNCF Sandbox project (formerly k8dash)
  - + simple `kubectl apply -f kubernetes-skooner.yaml` install —
    no Helm chart required (lower barrier than chart publishers)
  - + OIDC support built in
  - + real-time updates without page refresh
  - - 1.4k stars, moderate activity — not a high-velocity project
  - - management UI (CRUD on resources) — not a graph / topology
    visualizer
  - - no offline / manifest-file mode
- <https://github.com/skyhook-io/radar> (Apache 2.0)
  - + 3.8k stars, actively maintained (Skyhook, YC W23)
  - + multiple install paths: laptop binary (`curl -fsSL
    https://get.radarhq.io | sh`), Homebrew, krew
    (`kubectl krew install radar`), Scoop, AND Helm chart for
    in-cluster mode (`helm install radar skyhook/radar -n radar
    --create-namespace`)
  - + explicit "Topology" view (interactive ELK.js graph, live
    SSE), plus Resources, Traffic, Timeline, Helm, GitOps,
    Cluster Audit, Upgrade Impact, RBAC visibility, Cost, and
    Network Path views
  - + built-in MCP server so AI agents (Claude Code, Codex,
    Cursor, GitHub Copilot) can inspect the cluster via
    token-optimized data instead of raw `kubectl` YAML
  - + in-cluster Helm install requires no in-pod agents / CRDs /
    service-mesh sidecars
  - - in-cluster mode still requires a Deployment + Service +
    port 9280
  - - no offline / manifest-file mode
  - - **no diagram export** — Topology view is interactive only
    (ELK.js with keyboard shortcuts for fit / zoom / pan); no
    built-in SVG / PNG / Mermaid save
- <https://github.com/kubevious/kubevious> (Apache 2.0)
  - + app-centric validation + dependency graph ("Kubernetes
    without disasters" tagline)
  - + 1.8k stars, active; ships both a CLI for manifest validation
    AND a Dashboard for runtime introspection
  - + Docker images at `hub.docker.com/r/kubevious/kubevious`
  - - two products maintained together — heavier surface to
    evaluate vs single-purpose dashboards
  - - install steps not in the README front page
  - - no offline / manifest-file mode
  - - **no diagram export** — dependency graph is interactive in
    the dashboard only; no built-in SVG / PNG / Mermaid save
- <https://github.com/KusionStack/karpor> (Apache 2.0)
  - + 1.7k stars, branded as a "Kubernetes Visualization Tool"
  - + multi-cluster topology + resource graphs + Kubernetes-native
    observability in one UI
  - + actively developed
  - - less known in the western K8s ecosystem
  - - no offline / manifest-file mode
  - - in-cluster install path not documented in the front-page
    README
  - - **no diagram export** — Resource Topology is interactive
    only; no built-in SVG / PNG / Mermaid save
- <https://github.com/kiali/kiali> (Apache 2.0)
  - + best-in-class service-mesh graph for Istio — dedicated
    `/graph/namespaces?namespaces=X` view
  - + in-cluster via the Kiali Operator + Kiali CR
  - - only useful if the cluster runs Istio — shoggoth doesn't
    (no Istio manifests in `shoggoth/k3s/`), so this would be a
    one-off dependency
  - - no offline / manifest-file mode
  - - **no diagram export** — Graph page is interactive; README
    documents "Kiosk mode" for embedding the view in an iframe
    but no built-in SVG / PNG save-as (the Kiali frontend source
    has a "Save as image" button in some versions, but it's not
    documented in the README and is not part of the public API)
- <https://github.com/intelops/kubviz> (Apache 2.0)
  - + 42 stars, fresh (Feb 2025) — actively developed
  - + DevSecOps + GitOps + container-registry + SBOM (CycloneDX
    JSON) + vulnerability scanning (Trivy) in one tool
  - + multi-cluster via a NATS JetStream bridge between the
    client and the agent
  - - small community
  - - focused on the security / SBOM story; adding it for
    graph-viz alone is overkill
  - - heavy deployment (NATS + Clickhouse + Grafana + optional
    Postgres for persistence)
  - - no offline / manifest-file mode
- <https://github.com/oslabs-beta/spyglass> (MIT)
  - + wraps Kubeview + Prometheus + Grafana + Kubecost in one
    web UI
  - + React + Material UI frontend
  - - 126 stars, low community
  - - bundle-of-other-tools — adds multiple in-cluster
    dependencies (Prometheus + Grafana + Kubecost + Kubeview) for
    one dashboard
  - - no offline / manifest-file mode
  - - **no diagram export** — relies on the underlying
    Kubeview's interactive graph (no SVG / PNG); the embedded
    Grafana panels support PNG export within Grafana, but the
    cluster-topology view does not

### Hybrid (offline + live)

- <https://github.com/philippemerle/KubeDiagrams> (Apache 2.0)
  - + the only hybrid tool with first-class offline AND live input —
    same CLI works on manifests OR `kubectl get -o yaml | kube-diagrams -`
  - + could power both the static `docs/cluster.svg` (offline,
    reproducible, committed) AND a one-shot `make clusterviz-live`
    (drift view, release-time check)
  - + see full feature list in the "Offline" section above — same tool
- <https://github.com/tracettronic/kubeviewr> (MIT)
  - + reads manifest files OR queries live cluster
  - + interactive web-based output
  - - 30 stars, small user base
  - - interactive web — not a static artifact; doesn't fit the
    shoggoth project's Makefile-driven build pipeline

### Notes

- The Argo CRD submodule
  (`shoggoth/argo-workflows/manifests/base/crds/full/argoproj.io_*.yaml`)
  is rsync'd to the host by `make sync` (see
  `shoggoth/argo-workflows/manifests/base/crds/full/`). Any chosen
  tool needs to scan both `shoggoth/k3s/*.yaml` and the CRD submodule
  to graph Argo `Workflow` / `WorkflowTemplate` / `EventBus` /
  `EventSource` / `Sensor` objects.
- KubeDiagrams is the only tool with vendored, ready-to-use config
  examples for both shoggoth-relevant CRD families (Argo, External
  Secrets). Using any other tool would require writing custom CRD
  mapping from scratch.
