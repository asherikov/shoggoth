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

# placeholder variable, currently HOST_IP is used directly instead.
HOST?=host.${DOMAIN}
HOST_IP?=$(shell getent hosts ${HOST} | cut -f 1 -d ' ')

SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null

# Ansible looks for ansible.cfg in $PWD, then ~/.ansible.cfg, then
# /etc/ansible/ansible.cfg. It does not look next to the playbook, so
# `ansible-playbook ansible/deploy.yml`.
#
# The value ends in the `ansible` directory so each call site appends its
# playbook to it. The two flags sit ahead of that: every playbook in the tree
# is `hosts: instances`, and `--limit $(INSTANCE)` together with
# `-e ansible_host=$(HOST_IP)` is the one-instance-on-one-host targeting all
# eight call sites pass. Spelling it here keeps it in one place.
# `--ask-become-pass` deliberately is NOT shared — it prompts every play it
# touches (see ansible/README.md). Recursive (`=`), so `INSTANCE=` and
# `HOST_IP=` on the command line still reach it.
ANSIBLE = ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook \
          --limit $(INSTANCE) -e ansible_host=$(HOST_IP) ansible

help:
	grep -hv "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\."

-include make/*.mk
-include private/*.mk


# Tear the instance down and bring it back up from scratch.
restart: tunnel_up
	${MAKE} down
	${MAKE} up

ssh:
	ssh ${SSH_COMMON_ARGS} "${USER}@${HOST_IP}"

sshkey:
	ssh-copy-id ${SSH_COMMON_ARGS} -i "${HOME}/.ssh/id_rsa.pub" "${USER}@${HOST_IP}"

shutdown:
	-${MAKE} out
	${MAKE} down
	${K3S_SSH} 'exec su -l -c "shutdown -P now"'

reboot:
	-${MAKE} out
	${MAKE} down
	${K3S_SSH} 'exec su -l -c "shutdown -r now"'

hosts:
	# DOMAIN/HOST_IP travel through the environment, not --domain/--host-ip
	# flags: setup-client.sh already reads DOMAIN from the environment
	# (line 13), and passing config as CLI args is what this layout avoids.
	DOMAIN="${DOMAIN}" HOST_IP="${HOST_IP}" ./shoggoth/setup-client.sh --update-hosts

ping:
	ping -c 1 "${HOST_IP}"

home:
	firefox http://${DOMAIN}

test:
	echo "Requires VPN connectivity"
	echo "======================================================="
	echo ">>>>>>>>>>>> registry (zot)"
	curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "http://registry.${DOMAIN}:${REGISTRY_PORT}/v2/" | grep -q '^200' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> DNS"
	host ${DOMAIN} dns.${DOMAIN}
	echo "======================================================="
	echo ">>>>>>>>>>>> localai"
	${MAKE} ai_models
	echo "======================================================="
	echo ">>>>>>>>>>>> apt-proxy"
	curl http://apt-cache.${DOMAIN}/acng-report.html --output /dev/null


personal_conf:
	./shoggoth/setup-client.sh \
		--client-conf ${HOME}/.config/shoggoth \
		--domain "${DOMAIN}" --host-ip "${HOST_IP}" \
		--ai-token ${AI_TOKEN} \
		--ssh-config
