{ config, pkgs, ... }:
{
  networking.firewall.allowedTCPPorts = [ ${SHOGGOTH_WG_UI_PORT} ${SHOGGOTH_REGISTRY_PORT} ${SHOGGOTH_WEB_EXT_PORT} ];
  networking.firewall.allowedUDPPorts = [ ${SHOGGOTH_WG_PORT} ];
  networking.firewall.interfaces.wg0 = {
    allowedTCPPorts = [ ${SHOGGOTH_WG_UI_PORT} ${SHOGGOTH_REGISTRY_PORT} ${SHOGGOTH_WEB_EXT_PORT} ];
  };
}
