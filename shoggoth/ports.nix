{ config, pkgs, ... }:
{
  networking.firewall.allowedTCPPorts = [ ${WG_UI_PORT} ${REGISTRY_PORT} ${WEB_EXT_PORT} ];
  networking.firewall.allowedUDPPorts = [ ${WG_PORT} ];
  networking.firewall.interfaces.wg0 = {
    allowedTCPPorts = [ ${WG_UI_PORT} ${REGISTRY_PORT} ${WEB_EXT_PORT} ];
  };
}
