WG_IF?=wg0
WG_CONF?=./private/${WG_IF}.conf

in:
	sed -i "s/Endpoint = [0-9.]*:/Endpoint = ${HOST_IP}:/" ${WG_CONF}
	(command -v sudo && sudo wg-quick up ${WG_CONF}) || su -c "wg-quick up ${WG_CONF}"

out:
	(command -v sudo && sudo wg-quick down ${WG_CONF}) || su -c "wg-quick down ${WG_CONF}"
