export KUBECONFIG?=private/kubeconfig
K3S_MANIFESTS?=shoggoth/k3s
K3S_ALL_MANIFESTS := $(shell find ${K3S_MANIFESTS} -name '*.yaml' | sort)
K3S_TMP_DIR?=.tmp
K3S_TUNNEL_PID?=${K3S_TMP_DIR}/k3s-tunnel.pid

K3S_SSH=ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP}
K3S_API_PORT?=6443
K3S_TUNNEL_PORT?=6443

K3S_APP_LABELS := $(shell grep -h 'app:' ${K3S_ALL_MANIFESTS} | grep -v 'k8s-app' | sed 's/.*app: *//' | sort -u)
K3S_HOST_PATHS := redmine-plugins:redmine/plugins workflow-scripts:workflow/scripts kestra-flows:workflow/kestra/flows private:private


# installation
# -----
host_install_nixos:
	@echo "Installing K3s on ${HOST} via NixOS..."
	${MAKE} sync
	@mkdir -p ${K3S_TMP_DIR}
	@export SHOGGOTH_DOMAIN="${DOMAIN}" DNS_IP="${DNS_IP}" REGISTRY_PORT="${REGISTRY_PORT}" \
		WEB_EXT_PORT="${WEB_EXT_PORT}" WG_PORT="${WG_PORT}" WG_UI_PORT="${WG_UI_PORT}"; \
	envsubst '$${SHOGGOTH_DOMAIN}$${DNS_IP}$${REGISTRY_PORT}' < shoggoth/shoggoth.nix > ${K3S_TMP_DIR}/shoggoth.nix; \
	envsubst '$${WEB_EXT_PORT}$${WG_PORT}$${WG_UI_PORT}$${REGISTRY_PORT}' < shoggoth/ports.nix > ${K3S_TMP_DIR}/${INSTANCE}-ports.nix
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
	@echo "Pulling registry image on ${HOST} (bypassing containerd mirror)..."
	${K3S_SSH} 'sudo k3s ctr images pull ghcr.io/project-zot/zot:latest'

client_install_alpine:
	su -c 'apk add kubectl helm k9s'

client_kubeconfig:
	@echo "Fetching kubeconfig from ${HOST}..."
	mkdir -p private
	@echo "[sudo] password for ${USER}:"
	${K3S_SSH} 'sudo cat /etc/rancher/k3s/k3s.yaml' > ${KUBECONFIG}
	chmod 600 ${KUBECONFIG}
	@echo "Kubeconfig saved to ${KUBECONFIG}"


# connection & health
# -----

K3S_TUNNEL_LOG?=${K3S_TMP_DIR}/k3s-tunnel.log

tunnel_up:
	@mkdir -p ${K3S_TMP_DIR}
	@if [ -f ${K3S_TUNNEL_PID} ] && kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null; then \
		echo "Tunnel already running (PID $$(cat ${K3S_TUNNEL_PID}))"; \
	else \
		ssh ${SSH_COMMON_ARGS} -L ${K3S_TUNNEL_PORT}:127.0.0.1:${K3S_API_PORT} -N ${USER}@${HOST_IP} \
			-o ExitOnForwardFailure=yes 2>${K3S_TUNNEL_LOG} & \
		echo $$! > ${K3S_TUNNEL_PID}; \
		for i in 1 2 3 4 5 6 7 8 9 10; do \
			if ! kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null; then \
				echo "Error: Tunnel process died. Log:"; \
				cat ${K3S_TUNNEL_LOG}; \
				rm -f ${K3S_TUNNEL_PID}; exit 1; \
			fi; \
			if curl -sk --connect-timeout 1 https://127.0.0.1:${K3S_TUNNEL_PORT}/readyz >/dev/null 2>&1; then \
				echo "Tunnel started (PID $$(cat ${K3S_TUNNEL_PID}))"; \
				exit 0; \
			fi; \
			sleep 0.5; \
		done; \
		echo "Error: Tunnel process alive but API not reachable after 5s"; \
		echo "--- SSH log ---"; cat ${K3S_TUNNEL_LOG}; \
		echo "--- Host listen check ---"; \
		${K3S_SSH} 'sudo ss -tlnp | grep ${K3S_API_PORT} || echo "K3s API port ${K3S_API_PORT} not listening on host"'; \
		rm -f ${K3S_TUNNEL_PID}; exit 1; \
	fi

tunnel_down:
	@if [ -f ${K3S_TUNNEL_PID} ] && kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null; then \
		kill $$(cat ${K3S_TUNNEL_PID}); \
		echo "Tunnel stopped (PID $$(cat ${K3S_TUNNEL_PID}))"; \
	else \
		echo "Tunnel not running"; \
	fi
	@rm -f ${K3S_TUNNEL_PID} ${K3S_TUNNEL_LOG}


# maintenance
# -----
down: tunnel_up
	@echo "=== Stopping all shoggoth K3s workloads (preserving secrets) ==="
	kubectl delete deployment,statefulset,daemonset,job,svc,configmap -n ${INSTANCE} --all --ignore-not-found=true
	@echo "Waiting for all pods to terminate..."
	-kubectl -n ${INSTANCE} wait --for=delete pod --all --timeout=60s 2>/dev/null || true
	-kubectl -n ${INSTANCE} delete pod --all --grace-period=0 --force 2>/dev/null || true
	-kubectl -n ${INSTANCE} wait --for=delete pod --all --timeout=30s 2>/dev/null || true
	@echo "All workloads stopped."

purge: tunnel_up
	@if [ "${INSTANCE}" = "shoggoth" ]; then \
		echo "ERROR: Refusing to purge the default 'shoggoth' instance."; \
		echo "       Set INSTANCE=<name> to purge a specific instance."; \
		exit 1; \
	fi
	@echo "=== Purging shoggoth instance '${INSTANCE}' (namespace, PVCs, host data) ==="
	@echo "Deleting namespace ${INSTANCE} (all workloads, secrets, PVCs)..."
	-kubectl delete namespace ${INSTANCE} --ignore-not-found=true --timeout=120s
	@echo "Removing hostPath data..."
	-${K3S_SSH} 'sudo rm -rf /var/lib/rancher/k3s/storage/${INSTANCE}'
	@echo "Removing remote deployment directory..."
	-${K3S_SSH} 'rm -rf ${REMOTE_PATH}/${INSTANCE}'
	@echo "Instance '${INSTANCE}' purged."

# Wipe OpenBao raft storage. Does NOT restart anything — run sync_restart after.
# Useful when the unseal key is lost or OpenBao data is corrupted.
wipe_openbao: tunnel_up
	@echo "=== Wiping OpenBao raft storage (namespace: ${INSTANCE}) ==="
	@echo "Scaling down openbao StatefulSet..."
	@-kubectl scale statefulset openbao -n ${INSTANCE} --replicas=0 2>/dev/null || true
	@echo "Waiting for openbao-0 to terminate..."
	@-kubectl wait --for=delete pod/openbao-0 -n ${INSTANCE} --timeout=60s 2>/dev/null || true
	@PV_JSON=$$(kubectl get pv -o json | jq -r '.items[] | select(.spec.claimRef.name=="openbao-data" and .spec.claimRef.namespace=="${INSTANCE}")'); \
	PV_HOST_PATH=$$(printf '%s' "$${PV_JSON}" | jq -r '.spec.local.path // empty'); \
	PV_NAME=$$(printf '%s' "$${PV_JSON}" | jq -r '.metadata.name // empty'); \
	if [ -n "$${PV_HOST_PATH}" ]; then \
		echo "Wiping host data: $${PV_HOST_PATH}"; \
		${K3S_SSH} "sudo sh -c 'find \"$${PV_HOST_PATH}\" -mindepth 1 -exec rm -rf {} +'"; \
		WIPE_EXIT=$$?; \
		if [ $${WIPE_EXIT} -ne 0 ]; then \
			echo "ERROR: Host data wipe failed (exit code $${WIPE_EXIT}). Check sudo access."; \
			exit 1; \
		fi; \
		echo "Host data wiped."; \
	else \
		echo "No PV host path found, skipping host data wipe."; \
	fi; \
	if [ -n "$${PV_NAME}" ]; then \
		echo "Removing PV finalizer: $${PV_NAME}"; \
		kubectl patch pv "$${PV_NAME}" -p '{"metadata":{"finalizers":null}}' 2>/dev/null || true; \
		echo "Deleting PVC and PV..."; \
		-kubectl delete pvc openbao-data -n ${INSTANCE} --ignore-not-found=true --wait=false 2>/dev/null; \
		-kubectl delete pv "$${PV_NAME}" --ignore-not-found=true --wait=false 2>/dev/null; \
		for i in 1 2 3 4 5 6 7 8 9 10; do \
			kubectl get pvc openbao-data -n ${INSTANCE} >/dev/null 2>&1 || break; \
			sleep 1; \
		done; \
	fi
	@echo "Deleting unseal key secret..."
	@-kubectl delete secret openbao-unseal-key -n ${INSTANCE} --ignore-not-found=true
	@echo "Deleting failed bringup job..."
	@-kubectl delete job bringup -n ${INSTANCE} --ignore-not-found=true
	@echo "Waiting for bringup pod to terminate..."
	@-kubectl wait --for=delete pod -l job-name=bringup -n ${INSTANCE} --timeout=60s 2>/dev/null || true
	@echo "Done. Run 'make sync_restart' to re-deploy."

# Start all shoggoth K3s services.
up: tunnel_up
	@if ! printf '%s' "${INSTANCE}" | grep -qE '^[a-z0-9][a-z0-9-]*[a-z0-9]$$|^[a-z0-9]$$'; then \
		echo "ERROR: INSTANCE must be lowercase alphanumeric with hyphens (RFC 1123), got: '${INSTANCE}'"; exit 1; \
	fi
	@if [ $$(printf '%s' "${INSTANCE}" | wc -c) -gt 54 ]; then \
		echo "ERROR: '${INSTANCE}' exceeds 53 chars (namespace limit)"; exit 1; \
	fi
	@echo "=== Starting all shoggoth K3s services (namespace: ${INSTANCE}) ==="
	@export SHOGGOTH_DOMAIN="${DOMAIN}" SHOGGOTH_GITHUB_ORG="${GITHUB_ORG}" \
		SHOGGOTH_NAMESPACE="${INSTANCE}" \
		SHOGGOTH_INSTANCE_DIR="${INSTANCE}" \
		DNS_IP="${DNS_IP}" \
		WEB_EXT_PORT="${WEB_EXT_PORT}" WG_PORT="${WG_PORT}" \
		REGISTRY_PORT="${REGISTRY_PORT}" \
		WG_UI_PORT="${WG_UI_PORT}"; \
		for f in ${K3S_ALL_MANIFESTS}; do \
			envsubst '$${SHOGGOTH_DOMAIN}$${SHOGGOTH_GITHUB_ORG}$${SHOGGOTH_NAMESPACE}$${SHOGGOTH_INSTANCE_DIR}$${DNS_IP}$${WEB_EXT_PORT}$${WG_PORT}$${REGISTRY_PORT}$${WG_UI_PORT}' < $$f; \
			printf "\n---\n"; \
		done | kubectl apply -f -

# Stop a single service by name: make stop SERVICE=web-external
# Deletes deployment, statefulset, and job with matching name, plus its service.
# Does not delete configmaps, secrets, or PVCs.
stop: tunnel_up
	@if [ -z "${SERVICE}" ]; then \
		echo "Usage: make stop SERVICE=<name>"; \
		echo "Available services: ${K3S_APP_LABELS}"; \
		exit 1; \
	fi
	@echo "=== Stopping service: ${SERVICE} ==="
	-kubectl delete deployment,statefulset,daemonset,job,svc ${SERVICE} -n ${INSTANCE} --ignore-not-found=true
	@echo "Waiting for ${SERVICE} pod to terminate..."
	-kubectl -n ${INSTANCE} wait --for=delete pod -l app=${SERVICE} --timeout=60s 2>/dev/null || true
	-kubectl -n ${INSTANCE} delete pod -l app=${SERVICE} --grace-period=0 --force 2>/dev/null || true
	@echo "Service ${SERVICE} stopped."

# Start a single service by name: make start SERVICE=web-external
# Applies only the manifest file(s) matching the service name.
start: tunnel_up
	@if [ -z "${SERVICE}" ]; then \
		echo "Usage: make start SERVICE=<name>"; \
		echo "Available services: ${K3S_APP_LABELS}"; \
		exit 1; \
	fi
	@echo "=== Starting service: ${SERVICE} ==="
	@export SHOGGOTH_DOMAIN="${DOMAIN}" SHOGGOTH_GITHUB_ORG="${GITHUB_ORG}" \
		SHOGGOTH_NAMESPACE="${INSTANCE}" \
		SHOGGOTH_INSTANCE_DIR="${INSTANCE}" \
		DNS_IP="${DNS_IP}" \
		WEB_EXT_PORT="${WEB_EXT_PORT}" WG_PORT="${WG_PORT}" \
		REGISTRY_PORT="${REGISTRY_PORT}" \
		WG_UI_PORT="${WG_UI_PORT}"; \
	MANIFESTS="$$(grep -rl "app: ${SERVICE}" ${K3S_MANIFESTS} --include='*.yaml' | sort)"; \
	if [ -z "$$MANIFESTS" ]; then \
		echo "No manifest file found for service '${SERVICE}'"; \
		exit 1; \
	fi; \
	for f in $$MANIFESTS; do \
		echo "Applying $$f..."; \
		envsubst '$${SHOGGOTH_DOMAIN}$${SHOGGOTH_GITHUB_ORG}$${SHOGGOTH_NAMESPACE}$${SHOGGOTH_INSTANCE_DIR}$${DNS_IP}$${WEB_EXT_PORT}$${WG_PORT}$${REGISTRY_PORT}$${WG_UI_PORT}' < $$f | kubectl apply -f - || exit 1; \
	done

# Restart a single service: make restart_service SERVICE=kestra
restart_service: tunnel_up
	${MAKE} stop SERVICE=${SERVICE}
	${MAKE} start SERVICE=${SERVICE}

log: tunnel_up
	@if [ -z "$(SERVICE)" ]; then echo "Usage: make log SERVICE=<app-name> [CONTAINER=<name>]"; exit 1; fi
	@echo "=== $(SERVICE) ==="
	@echo ""
	@kubectl -n shoggoth describe pod -l job-name=$(SERVICE)
	@echo "--- Status ---"
	@kubectl -n ${INSTANCE} get pod -l app=$(SERVICE) -o wide 2>&1 || true
	@echo ""
	@echo "--- Container states ---"
	@kubectl -n ${INSTANCE} get pod -l app=$(SERVICE) -o json 2>/dev/null | jq -r '.items[] | .metadata.name as $$pod | ("Pod: " + $$pod), (.status.initContainerStatuses // [] | .[] | "  INIT [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)"), (.status.containerStatuses // [] | .[] | "  MAIN  [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)")' 2>&1 || true
	@echo ""
	@echo "=== Recent events ==="
	@kubectl -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -100 | grep "${SERVICE}" || true
	@echo ""
	@echo "--- Logs (from host /var/log/pods) ---"
	@${K3S_SSH} "sudo find /var/log/pods/ -path '*/${INSTANCE}_$(SERVICE)-[0-9a-z]*/*.log' -exec sh -c ' \
		dir=\$$(dirname \"\$$1\"); \
		cname=\$$(basename \"\$$dir\"); \
		pod=\$$(basename \"\$$(dirname \"\$$dir\")\"); \
		sz=\$$(wc -c < \"\$$1\"); \
		echo \"  [\$$cname] (\$${sz} bytes):\"; \
		if [ \"\$$sz\" -gt 0 ]; then tail -3000 \"\$$1\" 2>&1; fi; \
		echo \"\"; \
	' _ {} \;" 2>&1 || echo "No pod logs found on host for $(SERVICE)"

status: tunnel_up
	@echo "=== K3s shoggoth services (namespace: ${INSTANCE}) ==="
	kubectl get pods,svc,deployment,statefulset,job,pvc -n ${INSTANCE} -o wide
	@echo ""
	@echo "=== Recent events ==="
	@kubectl -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -20 || true
	@echo ""
	@echo "=== CoreDNS ==="
	kubectl get pods -n kube-system -l k8s-app=kube-dns
	@echo ""
	@echo "=== Pod readiness ==="
	@kubectl get pods -n ${INSTANCE} \
		-o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,STATUS:.status.phase \
		2>&1 || true
