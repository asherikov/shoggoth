# Shoggoth NixOS module for the host (common part)
#
# Copy this file to /etc/nixos/shoggoth.nix on the host and add the following
# lines to /etc/nixos/configuration.nix:
#
#   imports = [ ./shoggoth.nix ./<instance>-ports.nix ];
#
# The <instance>-ports.nix file is generated when the host is installed.

{ config, pkgs, ... }:

let
  domain = "${SHOGGOTH_DOMAIN}";
  registryPort = "${SHOGGOTH_REGISTRY_PORT}";
  # Pinned k3s node IP. Assigned to `lo` further down, which is what makes it
  # survive a network change — see the comment there for why loopback.
  k3sNodeIP = "10.99.0.1";
  k3sRegistries = pkgs.writeText "registries.yaml" ''
    mirrors:
      docker.io:
        endpoint:
          - "http://localhost:${registryPort}"
      ghcr.io:
        endpoint:
          - "http://localhost:${registryPort}"
      gcr.io:
        endpoint:
          - "http://localhost:${registryPort}"
      quay.io:
        endpoint:
          - "http://localhost:${registryPort}"
      mcr.microsoft.com:
        endpoint:
          - "http://localhost:${registryPort}"
      codeberg.org:
        endpoint:
          - "http://localhost:${registryPort}"
      docker.gitea.com:
        endpoint:
          - "http://localhost:${registryPort}"
      docker.angie.software:
        endpoint:
          - "http://localhost:${registryPort}"
      public.ecr.aws:
        endpoint:
          - "http://localhost:${registryPort}"
      registry.${domain}:
        endpoint:
          - "http://localhost:${registryPort}"
    configs:
      "localhost:${registryPort}":
        tls:
          insecure_skip_verify: true
      registry.${domain}:
        tls:
          insecure_skip_verify: true
  '';
in {
  # IMPORTANT: shoggoth assumes the k3s version is at least
  # 1.34.
  #
  # Features that are safe to rely on (all GA by 1.34):
  #
  #   * Pod-level ``spec.ttlSecondsAfterFinished`` (added
  #     1.23, GA 1.29).
  #   * Container-level ``restartPolicy`` for native sidecar
  #     ordering (added 1.28, GA 1.33).
  #   * ``Job.spec.ttlSecondsAfterFinished`` (since 1.12).
  services.k3s = {
    enable = true;
    role = "server";
    #   --advertise-address=127.0.0.1 was the way to make the endpoint immune
    #   to network changes, and it is no longer available. Since k8s 1.37 the
    #   apiserver refuses to start when the public address is loopback and the
    #   endpoint reconciler is the default `lease` ("cannot use public IP
    #   127.0.0.1 with endpoint reconciler").
    #
    #   The design is the pin: --node-ip=10.99.0.1, set below. One flag feeds
    #   the apiserver's advertise address, the kubelet's --node-ip, etcd's
    #   advertise address and the cert SANs, so nothing drifts and no network
    #   change can move the endpoint. A pinned address that disappears is
    #   unrecoverable until somebody edits the flag, which is why the pin sits
    #   on `lo` and not on a NIC — `lo` is the only interface guaranteed to be
    #   there, and network-addresses-lo.service below places the address on it
    #   whatever the network is doing. The kubelet is the only component that
    #   checks the interface at all (validateNodeIP walks net.InterfaceAddrs()
    #   on every node-status sync, not just at boot) and `lo` satisfies it.
    #
    #   What a loopback address costs is reachability from off-box, which is
    #   the intended shape: 6443 is the tunnel's job and ports.nix keeps it off
    #   the wire, so nothing gains an address a client elsewhere could dial.
    nodeIP = k3sNodeIP;
    extraFlags = [
      "--disable=traefik"
      "--disable=servicelb"
      "--kube-proxy-arg=healthz-bind-address=127.0.0.1:10256"
      "--tls-san=127.0.0.1"
    ];
  };

  # The address k3sNodeIP points at. It has to exist on an interface: the
  # kubelet's validateNodeIP walks net.InterfaceAddrs() and refuses to start
  # without it, and it does that on every node-status sync rather than only at
  # boot. `lo` is the one interface that is always there, which is what makes
  # the pin survive the host changing networks.
  #
  # 10.99.0.1 is outside every range this tree already uses — 10.42.0.0/16
  # (pods), 10.43.0.0/16 (services), 10.8.0.0/24 (wireguard) and the docker
  # bridges — and 127.0.0.0/8 and 169.254.0.0/16 are refused outright.
  networking.interfaces.lo.ipv4.addresses = [
    {
      address = k3sNodeIP;
      prefixLength = 32;
    }
  ];

  # `network-addresses-lo.service` is the unit that runs the `ip addr replace`
  # above, and by default nothing wants it: NixOS starts these units from a
  # generated udev rule on device-add, which means at boot.
  # `wants` and not `requires`, so a failure to place the address is k3s's
  # problem to report and not systemd's reason to leave the API server down.
  systemd.services.k3s = {
    after = [ "network-addresses-lo.service" ];
    wants = [ "network-addresses-lo.service" ];
  };

  environment.systemPackages = with pkgs; [
    k3s
    kubectl
    kubernetes-helm
  ];

  # K3s API server is accessed via SSH tunnel, not exposed on the network.

  # K3s uses its own containerd instance. Configure containerd registries
  # so K3s pulls images through the shoggoth container-cache proxy and treats
  # the internal registry as insecure (HTTP, self-signed CA).
  environment.etc."rancher/k3s/registries.yaml".source = k3sRegistries;

  virtualisation.docker.enable = true;
  virtualisation.docker.daemon.settings = {
    insecure-registries = [ "localhost" "localhost:${registryPort}" "registry.${domain}" ];
    registry-mirrors = [ "http://localhost:${registryPort}" ];
  };

  networking.firewall.trustedInterfaces = [ "cni0" ];
  networking.firewall.extraCommands = ''
    iptables -w -A FORWARD -i wg0 -o cni0 -j ACCEPT
  '';
}
