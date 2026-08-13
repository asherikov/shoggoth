# Shoggoth NixOS module for the host
#
# Copy this file to /etc/nixos/shoggoth.nix on the host and add the following
# line to /etc/nixos/configuration.nix:
#
#   imports = [ ./shoggoth.nix ];

{ config, pkgs, ... }:

let
  domain = "s.local";
  dockerProxyPort = "3128";
  k3sRegistries = pkgs.writeText "registries.yaml" ''
    mirrors:
      docker.io:
        rewrite:
          - "docker-registry.${domain}"
      docker-registry.${domain}:
        endpoint: "http://docker-registry.${domain}"
    configs:
      docker-registry.${domain}:
        http: true
  '';
in {
  services.k3s = {
    enable = true;
    role = "server";
    extraFlags = "--disable=traefik --disable=servicelb";
  };

  environment.systemPackages = with pkgs; [
    k3s
    kubectl
    kubernetes-helm
  ];

  # K3s API server is accessed via SSH tunnel, not exposed on the network.

  # K3s uses its own containerd instance. Configure containerd registries
  # so K3s pulls images through the shoggoth docker-cache proxy and treats
  # the internal registry as insecure (HTTP, self-signed CA).
  environment.etc."rancher/k3s/registries.yaml".source = k3sRegistries;

  virtualisation.docker.enable = true;
  virtualisation.docker.daemon.settings = {
    insecure-registries = [ "docker-registry.${domain}" ];
    proxies = {
      "http-proxy" = "http://localhost:${dockerProxyPort}";
      "https-proxy" = "http://localhost:${dockerProxyPort}";
      "no-proxy" = "*.${domain}";
    };
  };

  networking.hosts = {
    "127.0.0.1" = [ "docker-registry.${domain}" ];
  };

  networking.firewall.allowedTCPPorts = [ 443 ];
  networking.firewall.allowedUDPPorts = [ 51820 ];
  networking.firewall.trustedInterfaces = [ "cni0" ];
  networking.firewall.interfaces.wg0 = {
    allowedTCPPorts = [ 443 ];
  };
  networking.firewall.extraCommands = ''
    iptables -w -A FORWARD -i wg0 -o cni0 -j ACCEPT
  '';
}
