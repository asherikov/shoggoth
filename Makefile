JOBS?=4
INSTANCE?=shoggoth
# Per-instance configuration. This YAML file is the source of truth: `make up`
# hands it to Helm with --values, and the chart derives every SHOGGOTT_* pod
# environment variable from it. Nothing is injected with --set, and no value
# below is defined twice -- they are read out of this one file.
SHOGGOTH_CONFIG?=shoggoth/${INSTANCE}.yaml
# Chart root. Defined here, above `-include make/*.mk`, rather than in
# make/k3s.mk next to the rest of the Helm plumbing: a rule's prerequisite list
# is expanded while the makefile is read, and `make/*.mk` is included
# alphabetically, so chore.mk is parsed before k3s.mk. A variable chore.mk
# names in a prerequisite is therefore still undefined at that point. Recipes
# have no such constraint — they expand when they run.
SHOGGOTH_CHART_ROOT?=$(CURDIR)/shoggoth/chart
# Reads one value from $(SHOGGOTH_CONFIG). For composing URLs and hostnames inside
# recipes; never for handing a value to another tool as a CLI argument.
SHOGGOTH_CFG = $(shell yq -r '$(1)' $(SHOGGOTH_CONFIG) 2>/dev/null)
DOMAIN?=$(call SHOGGOTH_CFG,.identity.domain)
WEB_EXT_PORT?=$(call SHOGGOTH_CFG,.network.webExternal.hostPort)
WG_PORT?=$(call SHOGGOTH_CFG,.network.wireguard.udpPort)
DNS_IP?=$(call SHOGGOTH_CFG,.identity.dnsIp)
REGISTRY_PORT?=$(call SHOGGOTH_CFG,.network.registry.hostPort)
WG_UI_PORT?=$(call SHOGGOTH_CFG,.network.wireguard.uiPort)
# No Make variable exists for .identity.githubOrg, .ai.*, .gitea.serverTokenFileRef
# or the LDAP base DN. The chart reads those from $(SHOGGOTH_CONFIG) at render
# time, and `make host_install_nixos` -- the only recipe needing a value outside
# Helm -- consumes exactly the six above. A Make-level copy would be a second
# definition of one value, which is what the SHOGGOTT_CONFIG comment above
# forbids.
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

# sync_restart — stop the running release, refresh on-disk artefacts, install fresh.
#
# Each sub-step is intentionally wrapped so a failure halts the sequence rather
# than letting the next step paint over an inconsistent cluster state. The
# original recipe ran ``${MAKE} down_if_running`` without checking its exit;
# that swallowed ``helm uninstall --wait``-timeouts and let `make up` reinstall
# the chart on top of a half-deleted release, which is what wedged this cluster
# after the chart started shipping per-instance ExternalSecret/PushSecret
# resources (the ESO controller holds their finalizers). halt-on-failure here
# is one half of the fix; the FORCE=1 path inside `make down` is the other.
#
# `sync_restart` propagates ``FORCE=1`` to ``down_if_running`` because the
# intent of "restart from a clean state" already implies tolerating the
# finalizer-wedge fallback; without that, the recipe would still leave the
# cluster stuck at the helm uninstall timeout in the common case the chart has
# freshly acquired ESO CRs. Operators who want the strict path can run
# `make down_if_running` directly.
sync_restart: tunnel_up
	echo "=== sync_restart: step 1/3 — down_if_running (FORCE=1 propagated) ==="
	${MAKE} down_if_running FORCE=1 || { \
	    echo "FAIL: make down_if_running exited $$? — sync_restart halted."; \
	    echo "      Even with FORCE=1 force-cleanup, helm uninstall did not complete."; \
	    echo "      Likely cause: a CR has a finalizer outside the seven kinds"; \
	    echo "      force-helm-cleanup scrubs (externalsecret, pushsecret, workflow,"; \
	    echo "      workflowtemplate, cronworkflow, clusterworkflowtemplate, workflowtaskset)."; \
	    echo "      Run these diagnostics to find it:"; \
	    echo "        kubectl api-resources --namespaced=false | head"; \
	    echo "        kubectl get <kind> -n $${INSTANCE:-shoggoth} -o json \\"; \
	    echo "          | jq -r '.items[] | select(.metadata.finalizers != null) \\"; \
	    echo "              | \"\\(.kind)/\\(.metadata.name)  finalizers=\\(.metadata.finalizers)\"' | head"; \
	    echo "      Then patch the listed finalizers to [] and retry:"; \
	    echo "        make down FORCE=1   # falls through to force-helm-cleanup"; \
	    echo "      Destructive last resort: 'make purge' then 'make up'."; \
	    exit 1; \
	}
	echo "=== sync_restart: step 2/3 — sync ==="
	${MAKE} sync || { \
	    echo "FAIL: make sync exited $$? — sync_restart halted after uninstall."; \
	    echo "      The release IS uninstalled at this point; a partial rsync left"; \
	    echo "      the host's repo tree older than the dev container's. Run 'make sync'"; \
	    echo "      by hand, then 'make up'."; \
	    exit 1; \
	}
	echo "=== sync_restart: step 3/3 — up ==="
	${MAKE} up || { \
	    echo "FAIL: make up exited $$? — sync_restart halted after sync."; \
	    echo "      Repo tree on host is current but no release is installed. Run 'make up'"; \
	    echo "      by hand to install, or 'make status' to inspect cluster state."; \
	    exit 1; \
	}

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
	${MAKE} down_if_running
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
