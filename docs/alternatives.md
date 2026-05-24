# Service Alternatives

Chosen and alternative services for the Shoggoth stack (see `shoggoth/docker-compose.yml`).

## Web server / reverse proxy

- **[Angie](https://docker.angie.software/angie:latest)** (selected)

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

### LLM proxy

- **[LiteLLM](https://github.com/BerriAI/litellm)** (selected)
  - OpenAI-compatible gateway for Ollama with Prometheus metrics and OTel tracing
  - Also serves as MCP gateway for observability on MCP tool calls

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
