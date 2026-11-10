export KUBECONFIG?=$(CURDIR)/private/kubeconfig
K3S_TMP_DIR?=.tmp
K3S_TUNNEL_PID?=${K3S_TMP_DIR}/k3s-tunnel.pid

K3S_SSH=ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP}
K3S_API_PORT?=6443
K3S_TUNNEL_PORT?=6443


# installation
# -----
host_install_nixos:
	echo "Installing K3s on ${HOST} via NixOS..."
	mkdir -p ${K3S_TMP_DIR}
	export SHOGGOTH_DOMAIN="${DOMAIN}" SHOGGOTH_DNS_IP="${DNS_IP}" SHOGGOTH_REGISTRY_PORT="${REGISTRY_PORT}" \
		SHOGGOTH_WEB_EXT_PORT="${WEB_EXT_PORT}" SHOGGOTH_WG_PORT="${WG_PORT}" SHOGGOTH_WG_UI_PORT="${WG_UI_PORT}"; \
	envsubst '$${SHOGGOTH_DOMAIN}$${SHOGGOTH_DNS_IP}$${SHOGGOTH_REGISTRY_PORT}' < shoggoth/shoggoth.nix > ${K3S_TMP_DIR}/shoggoth.nix; \
	envsubst '$${SHOGGOTH_WEB_EXT_PORT}$${SHOGGOTH_WG_PORT}$${SHOGGOTH_WG_UI_PORT}$${SHOGGOTH_REGISTRY_PORT}' < shoggoth/ports.nix > ${K3S_TMP_DIR}/${INSTANCE}-ports.nix
	scp ${SSH_COMMON_ARGS} ${K3S_TMP_DIR}/shoggoth.nix ${K3S_TMP_DIR}/${INSTANCE}-ports.nix ${USER}@${HOST_IP}:/tmp/
	${K3S_SSH} 'sudo cp /tmp/shoggoth.nix /tmp/${INSTANCE}-ports.nix /etc/nixos/ && rm /tmp/shoggoth.nix /tmp/${INSTANCE}-ports.nix'
	${K3S_SSH} 'grep -q "${INSTANCE}-ports.nix" /etc/nixos/configuration.nix || { \
		echo ""; echo "Add this line to /etc/nixos/configuration.nix:"; \
		echo "  imports = [ ./shoggoth.nix ./${INSTANCE}-ports.nix ];"; \
		echo ""; echo "Then re-run: make host_install_nixos"; \
		exit 1; \
	}'
	${K3S_SSH} 'sudo nixos-rebuild switch'
	${MAKE} pull_registry_image

pull_registry_image:
	echo "Pulling registry image on ${HOST} (bypassing containerd mirror)..."
	${K3S_SSH} 'sudo k3s ctr images pull ghcr.io/project-zot/zot:v2.1.21'


# The whole control-node setup, in the order it has to happen: ansible-core
# itself, the `kubernetes` Python client its modules import, then the two
# collections.
control_setup:
	pipx install "ansible-core>=2.22.0"
	pipx inject ansible-core kubernetes
	ansible-galaxy collection install -r ansible/requirements.yml
	# get kubeconfig
	$(ANSIBLE)/kubeconfig.yml \
		-e shoggoth_kubeconfig=$(if $(filter /% ~%,$(KUBECONFIG)),$(KUBECONFIG),$(CURDIR)/$(KUBECONFIG)) --ask-become-pass


# connection & health
# -----

K3S_TUNNEL_LOG?=${K3S_TMP_DIR}/k3s-tunnel.log

tunnel_up: ping
	mkdir -p ${K3S_TMP_DIR}
	if [ -f ${K3S_TUNNEL_PID} ] && kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null \
		&& curl -sk --connect-timeout 2 --max-time 3 https://127.0.0.1:${K3S_TUNNEL_PORT}/readyz >/dev/null 2>&1; then \
		echo "Tunnel already running (PID $$(cat ${K3S_TUNNEL_PID}))"; \
	else \
		pid=$$(cat ${K3S_TUNNEL_PID} 2>/dev/null || true); \
		if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null \
			&& tr '\0' ' ' < /proc/$$pid/cmdline 2>/dev/null | grep -q -- "-L [^ ]*${K3S_TUNNEL_PORT}:"; then \
			echo "Tunnel stale (PID $$pid alive but not serving the API) — replacing it"; \
			kill "$$pid" 2>/dev/null || true; \
			w=0; while kill -0 "$$pid" 2>/dev/null && [ "$$w" -lt 20 ]; do sleep 0.1; w=$$((w + 1)); done; \
		fi; \
		rm -f ${K3S_TUNNEL_PID}; \
		ssh ${SSH_COMMON_ARGS} -L ${K3S_TUNNEL_PORT}:127.0.0.1:${K3S_API_PORT} -N ${USER}@${HOST_IP} \
			-o ExitOnForwardFailure=yes 2>${K3S_TUNNEL_LOG} & \
		echo $$! > ${K3S_TUNNEL_PID}; \
		for i in 1 2 3 4 5 6 7 8 9 10; do \
			if ! kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null; then \
				echo "Error: Tunnel process died. Log:"; \
				cat ${K3S_TUNNEL_LOG}; \
				rm -f ${K3S_TUNNEL_PID}; exit 1; \
			fi; \
			if curl -sk --connect-timeout 1 --max-time 2 https://127.0.0.1:${K3S_TUNNEL_PORT}/readyz >/dev/null 2>&1; then \
				echo "Tunnel started (PID $$(cat ${K3S_TUNNEL_PID}))"; \
				exit 0; \
			fi; \
			sleep 0.5; \
		done; \
		echo "Error: Tunnel process alive but the API did not answer"; \
		echo "--- SSH log ---"; cat ${K3S_TUNNEL_LOG}; \
		echo "--- Host listen check ---"; \
		${K3S_SSH} 'sudo ss -tlnp | grep ${K3S_API_PORT} || echo "K3s API port ${K3S_API_PORT} not listening on host"'; \
		rm -f ${K3S_TUNNEL_PID}; exit 1; \
	fi

tunnel_down:
	if [ -f ${K3S_TUNNEL_PID} ] && kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null; then \
		kill $$(cat ${K3S_TUNNEL_PID}); \
		echo "Tunnel stopped (PID $$(cat ${K3S_TUNNEL_PID}))"; \
	else \
		echo "Tunnel not running"; \
	fi
	rm -f ${K3S_TUNNEL_PID} ${K3S_TUNNEL_LOG}


# maintenance
# -----

# Stop all shoggoth services: `helm uninstall` plus a purge of the namespace's
# Secrets. `helm uninstall` deletes workloads and ConfigMaps but deliberately
# leaves the data plane alone.
down: tunnel_up
	$(ANSIBLE)/down.yml

# Destroy an instance: namespace, cluster-scoped RBAC, host storage data.
purge: tunnel_up
	$(ANSIBLE)/purge.yml --ask-become-pass

# Start all shoggoth services.
up: tunnel_up
	$(ANSIBLE)/deploy.yml --ask-become-pass

# `make start|stop SERVICE=<x>` flips a single service toggle:
# `helm upgrade --reuse-values` with `--set services.<x>.enabled=<bool>`.

.PHONY: start
start: tunnel_up
	$(ANSIBLE)/service.yml -e shoggoth_service=$(SERVICE) -e shoggoth_service_action=enable
.PHONY: stop
stop: tunnel_up
	$(ANSIBLE)/service.yml -e shoggoth_service=$(SERVICE) -e shoggoth_service_action=disable

# Restart a single service: the two toggles above, in that order. The tunnel
# is a prerequisite of both, so it is up before either half runs.
.PHONY: restart_service
restart_service: tunnel_up
	${MAKE} stop
	${MAKE} start

log: tunnel_up
	test -n "$(SERVICE)" || (echo "Usage: make log SERVICE=<app-name> [CONTAINER=<name>]" && false)
	kubectl -n ${INSTANCE} describe pod -l app=$(SERVICE)
	kubectl -n ${INSTANCE} get pod -l app=$(SERVICE) -o wide 2>&1 || true
	kubectl -n ${INSTANCE} get pod -l app=$(SERVICE) -o json 2>/dev/null | jq -r '.items[] | .metadata.name as $$pod | ("Pod: " + $$pod), (.status.initContainerStatuses // [] | .[] | "  INIT [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)"), (.status.containerStatuses // [] | .[] | "  MAIN  [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)")' 2>&1 || true
	kubectl -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -100 | grep "${SERVICE}" || true
	echo "--- Logs (from host /var/log/pods) ---"
	${K3S_SSH} "sudo find /var/log/pods/ -path '*/${INSTANCE}_$(SERVICE)-[0-9a-z]*/*.log' -exec sh -c ' \
		dir=\$$(dirname \"\$$1\"); \
		cname=\$$(basename \"\$$dir\"); \
		pod=\$$(basename \"\$$(dirname \"\$$dir\")\"); \
		sz=\$$(wc -c < \"\$$1\"); \
		echo \"  [\$$cname] (\$${sz} bytes):\"; \
		if [ \"\$$sz\" -gt 0 ]; then tail -3000 \"\$$1\" 2>&1 | grep -v kube-probe; fi; \
		echo \"\"; \
		' _ {} \;" 2>&1 || echo "No pod logs found on host for $(SERVICE)"

# Open an interactive shell in a shoggoth pod, or run a command inside it.
# Default pod is the first match of label app=slave-dind.
#
# Usage:
#   make shell                              # interactive sh in slave-dind
#   make shell APP=slave-term               # first pod labeled app=slave-term
#   make shell POD=slave-dind-0-abcde       # explicit pod name (overrides APP)
#   make shell CMD="ls -la /shoggoth"       # run a command instead of an interactive shell
#   make shell APP=gitea CMD="id"           # combine APP and CMD
shell: tunnel_up
	APP_VAL=$(if $(APP),$(APP),slave-dind); \
	CMD_VAL=$(if $(CMD),$(CMD),sh); \
	if [ -n "${POD}" ]; then \
		POD_VAL="${POD}"; \
		echo "=== Using POD=$${POD_VAL} (namespace: ${INSTANCE}) ==="; \
	else \
		POD_VAL=$$(kubectl get pod -n ${INSTANCE} -l app=$${APP_VAL} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
		if [ -z "$${POD_VAL}" ]; then \
			echo "FAIL: no pod found for label app=$${APP_VAL} in namespace ${INSTANCE}"; \
			echo "      Available pods:"; \
			kubectl get pod -n ${INSTANCE} --no-headers -o custom-columns=NAME:.metadata.name,APP:.metadata.labels.app 2>/dev/null | sed 's/^/        /'; \
			exit 1; \
		fi; \
		echo "=== Resolved app=$${APP_VAL} -> pod $${POD_VAL} (namespace: ${INSTANCE}) ==="; \
	fi; \
	TTY_FLAGS="-i"; \
	if [ -t 0 ]; then TTY_FLAGS="-it"; fi; \
	exec kubectl exec -n ${INSTANCE} $${TTY_FLAGS} $${POD_VAL} -- $${CMD_VAL}

status: tunnel_up
	kubectl -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -20 || true
	CA="$$(kubectl get validatingwebhookconfiguration externalsecret-validate -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null)"; \
	if [ -n "$$CA" ]; then printf "caBundle injected:    yes\n"; else printf "caBundle injected:    no\n"; fi
	CERT=$$(kubectl -n ${INSTANCE} get secret external-secrets-webhook -o name 2>/dev/null | wc -l); \
	if [ "$$CERT" -gt 0 ]; then printf "cert Secret present:  yes\n"; else printf "cert Secret present:  no\n"; fi
	printf "SecretStores:         %s\n" "$$(kubectl get secretstore -n ${INSTANCE} --no-headers 2>/dev/null | wc -l)"
	printf "ExternalSecrets:      %s\n" "$$(kubectl get externalsecret -n ${INSTANCE} --no-headers 2>/dev/null | wc -l)"
	echo "=== CoreDNS ==="
	kubectl get pods -n kube-system -l k8s-app=kube-dns
	echo "=== K3s shoggoth services (namespace: ${INSTANCE}) ==="
	kubectl get pods,svc,deployment,statefulset,job,pvc -n ${INSTANCE} -o wide
	echo "=== Pod readiness ==="
	kubectl get pods -n ${INSTANCE} \
	    -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,STATUS:.status.phase \
        2>&1 || true

# Drop a tag from the local registry cache.
#
# Triggers on-demand re-sync from upstream on the next pull of IMAGE:TAG.
# zot is configured with anonymous access, so no credentials are required.
# Requires network reachability to registry.${DOMAIN}:${REGISTRY_PORT}.
# Example: make drop_image_tag IMAGE=library/nginx TAG=latest
drop_image_tag:
	$(ANSIBLE)/drop_image_tag.yml -e shoggoth_image=$(IMAGE) -e shoggoth_tag=$(TAG)
