# ADR 0001: Place most services behind VPN

Date: 2026-06-26

## Status

Accepted

## Context

Shoggoth runs a large number of services on a single host: Git server, CI
runners, project management, monitoring, caching proxies, LLM backends, secret
storage, LDAP, and more. These services must be accessible to the user from
multiple client machines, but should not be exposed to the local network or
the internet at large.

Three primary concerns drive this decision:

1. **Security.** Most shoggoth services do not enforce authentication
   robustly (or at all) in their default configuration. Gitea, Redmine, CDash,
   Grafana, phpldapadmin, OpenBao, and the various caching proxies are all
   designed for trusted network environments. Exposing them on the host's
   network interfaces would allow anyone on the same network to access source
   code, secrets, and administrative interfaces.

2. **DNS resolution for service hostnames.** Shoggoth services are addressed
   by DNS names under a private domain (e.g. `git.s.local`,
   `redmine.s.local`). Clients need a DNS resolver that knows how to resolve
   these names. Without a VPN, each client would need manual `/etc/hosts`
   entries or a split-horizon DNS configuration — error-prone and tedious to
   maintain across multiple machines. With a VPN, the VPN software can push
   DNS server configuration to clients automatically, and the Unbound DNS
   resolver running on the VPN network resolves all service hostnames as soon
   as the tunnel is established. No per-client DNS setup is needed beyond the
   VPN connection itself.

3. **Port conflicts with the host system.** Several shoggoth services use
   well-known ports — most notably SSH (port 22) for Gitea git operations
   over SSH. The host system typically already runs an SSH daemon on port 22.
   Binding additional services to host ports requires either remapping to
   non-standard ports (which complicates client configuration and breaks
   tool defaults) or binding to a separate network interface.

A VPN solves all three problems: the VPN software manages DNS server
assignment for clients, all inter-service and client-to-service traffic flows
over a virtual network interface with its own IP address so host port
conflicts disappear entirely, and services are reachable only by authenticated
VPN peers.

## Decision

Place all shoggoth services behind the WireGuard VPN, with the following
exceptions that remain accessible without VPN:

- `wireguard` (51820/udp) — the WireGuard VPN server itself; must be
  reachable before a client has a tunnel.
- `web-external` (443/tcp) — TLS-terminating reverse proxy that exposes the
  WireGuard web management UI by server IP address, so users can enroll new
  devices without an existing VPN connection.

Two services bind to localhost only on the host and are not exposed to the
network or the VPN:

- `docker-cache` (127.0.0.1:3128) — Docker registry caching proxy, used by
  the host's own Docker daemon.
- `docker-registry` (127.0.0.1:80) — private Docker registry, used by the
  host's own Docker daemon.

### Rationale

- **VPN-managed DNS eliminates extra DNS configuration.** The VPN software
  (wg-easy) assigns each client an IP address and can push DNS server
  configuration. Since the Unbound DNS resolver runs on the VPN network,
  clients automatically resolve shoggoth service hostnames (e.g.
  `git.s.local`, `redmine.s.local`) as soon as they connect — no manual
  `/etc/hosts` entries or split-horizon DNS setup is required on the client.

- **VPN access by IP eliminates port conflicts.** The VPN interface has its
  own IP address distinct from the host's physical interfaces. Services bind
  to the VPN IP (or to the Docker network that rides on top of it), so they
  never compete with host-bound services for the same port. Gitea's SSH
  listener on port 22, for example, runs on the VPN IP and does not conflict
  with the host's SSH daemon.

- **Minimal attack surface.** Only the WireGuard UDP port and the
  `web-external` HTTPS port are exposed on the host. Everything else is
  invisible to anyone not authenticated on the VPN.

## Consequences

- **VPN is a hard dependency.** Clients must be connected to the VPN to use
  any service except `wireguard` and `web-external`. If the VPN is down, all
  services are unreachable, including DNS resolution for service hostnames.

- **Onboarding requires the web-external proxy.** New devices must enroll via
  the `web-external` WireGuard web UI before they can access any other
  service.

- **Docker networking is simplified.** Services communicate over the shared
  `shoggoth` Docker network and do not need to publish ports to the host.
  The VPN container publishes its port and routes traffic to the Docker
  network.

- **Host firewall rules are minimal.** Only WireGuard UDP (51820) and
  `web-external` HTTPS (443) need to be opened on the host firewall.