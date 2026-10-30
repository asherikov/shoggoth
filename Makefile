JOBS?=4
INSTANCE?=shoggoth
SHOGGOTH_CONFIG?=shoggoth/${INSTANCE}.yaml
SHOGGOTH_CHART_ROOT?=$(CURDIR)/shoggoth/chart

SHOGGOTH_CFG = $(shell yq -r '$(1)' $(SHOGGOTH_CONFIG) 2>/dev/null)
DOMAIN?=$(call SHOGGOTH_CFG,.identity.domain)
WEB_EXT_PORT?=$(call SHOGGOTH_CFG,.network.webExternal.hostPort)
WG_PORT?=$(call SHOGGOTH_CFG,.network.wireguard.udpPort)
DNS_IP?=$(call SHOGGOTH_CFG,.identity.dnsIp)
REGISTRY_PORT?=$(call SHOGGOTH_CFG,.network.registry.hostPort)
WG_UI_PORT?=$(call SHOGGOTH_CFG,.network.wireguard.uiPort)

HOST?=host.${DOMAIN}
HOST_IP?=$(shell getent hosts ${HOST} | cut -f 1 -d ' ')
GITEA_TOKEN?=$(shell cat shoggoth/private/gitea-server-token.txt)
API_HOST=api.${DOMAIN}
REMOTE_PATH?=~/

SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null


help:
	grep -v "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\." | grep -v ".mk:$$"

-include make/*.mk
-include private/*.mk

sync:
	rsync -e "ssh ${SSH_COMMON_ARGS}" -rl shoggoth ${USER}@${HOST_IP}:${REMOTE_PATH}/${INSTANCE} || true
	K3S_DIR="${INSTANCE}"; \
	cmds="mkdir -p /var/lib/rancher/k3s/storage/$${K3S_DIR}/coredns-blacklists"; \
	cmds="$$cmds && cp ./dns/hosts-blacklist/hosts /var/lib/rancher/k3s/storage/$${K3S_DIR}/coredns-blacklists/blocklist.hosts"; \
	cmds="$$cmds && mkdir -p /var/lib/rancher/k3s/storage/$${K3S_DIR}/scripts"; \
	cmds="$$cmds && cp ./setup-client.sh /var/lib/rancher/k3s/storage/$${K3S_DIR}/scripts/setup-client.sh"; \
	for entry in ${K3S_HOST_PATHS}; do \
		DST=$$(echo "$$entry" | cut -d: -f1); \
		SRC=$$(echo "$$entry" | cut -d: -f2); \
		cmds="$$cmds && rsync -a --delete ./$$SRC/ /var/lib/rancher/k3s/storage/$${K3S_DIR}/$$DST/"; \
	done; \
	${K3S_SSH} "sh -c \"cd ${REMOTE_PATH}/${INSTANCE}/shoggoth && sudo bash -c '$$cmds'\""

sync_restart: tunnel_up
	${MAKE} down
	${MAKE} sync
	${MAKE} up

mount:
	mkdir -p mountpoint
	sshfs ${USER}@${HOST_IP}:${REMOTE_PATH} ./mountpoint

umount:
	fusermount3 -u mountpoint

ssh:
	ssh ${SSH_COMMON_ARGS} "${USER}@${HOST_IP}"

sshkey:
	ssh-copy-id ${SSH_COMMON_ARGS} -i "${HOME}/.ssh/id_rsa.pub" "${USER}@${HOST_IP}"

ssh_exec:
	ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP} 'cd ${REMOTE_PATH}/${INSTANCE}/shoggoth && ${CMD}'

shutdown:
	-${MAKE} out
	! $(SHOGGOTH_HELM_STATUS) >/dev/null 2>&1 || ${MAKE} down
	${MAKE} ssh_exec CMD='exec su -l -c "shutdown -P now"'

hosts:
	# DOMAIN/HOST_IP travel through the environment, not --domain/--host-ip
	# flags: setup-client.sh already reads DOMAIN from the environment
	# (line 13), and passing config as CLI args is what this layout avoids.
	DOMAIN="${DOMAIN}" HOST_IP="${HOST_IP}" ./shoggoth/setup-client.sh --update-hosts

ping:
	ping "${HOST_IP}"

home:
	firefox http://${DOMAIN}

test:
	echo "Requires VPN connectivity"
	echo "======================================================="
	echo ">>>>>>>>>>>> registry (zot)"
	curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "http://registry.${DOMAIN}:${REGISTRY_PORT}/v2/" | grep -q '^200' && echo "OK" || echo "FAIL"
	curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "http://registry.${DOMAIN}:${REGISTRY_PORT}/v2/_catalog" | grep -q '^200' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> DNS"
	host ${DOMAIN} dns.${DOMAIN}
	echo "======================================================="
	echo ">>>>>>>>>>>> api gateway /gitea redirect"
	curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://${API_HOST}/gitea | grep -q '^301 http://${API_HOST}/gitea/' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> api gateway /gitea/"
	curl -s -o /dev/null -w '%{http_code}' http://${API_HOST}/gitea/ | grep -q '^200' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> api gateway /gitea/api/v1/version"
	curl -s -o /dev/null -w '%{http_code}' http://${API_HOST}/gitea/api/v1/version | grep -q '^200' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> api gateway /redmine redirect"
	curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://${API_HOST}/redmine | grep -q '^301 http://${API_HOST}/redmine/' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> api gateway /redmine/"
	curl -s -o /dev/null -w '%{http_code}' http://${API_HOST}/redmine/ | grep -q '^200' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> localai"
	${MAKE} ai_embed
	${MAKE} ai_models
	echo ""
	echo "======================================================="
	echo ">>>>>>>>>>>> apt-proxy"
	curl http://apt-cache.${DOMAIN}/acng-report.html --output /dev/null


personal_conf:
	./shoggoth/setup-client.sh \
		--client-conf ${HOME}/.config/shoggoth \
		--domain "${DOMAIN}" --host-ip "${HOST_IP}" \
		--ai-token ${AI_TOKEN} \
		--ssh-config
