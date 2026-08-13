K3S_KUBECONFIG?=private/kubeconfig
K3S_MANIFESTS?=shoggoth/k3s
K3S_ALL_MANIFESTS := $(shell find ${K3S_MANIFESTS} -name '*.yaml' | sort)
K3S_TMP_DIR?=.tmp
K3S_TUNNEL_PID?=${K3S_TMP_DIR}/k3s-tunnel.pid

K3S_SSH=ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP}
K3S_SCP_BASE=scp ${SSH_COMMON_ARGS}
K3S_API_PORT?=6443
K3S_TUNNEL_PORT?=6443
K3S_KUBECTL=KUBECONFIG=${K3S_KUBECONFIG} kubectl

K3S_APP_LABELS := $(shell grep -h 'app:' ${K3S_ALL_MANIFESTS} | grep -v 'k8s-app' | sed 's/.*app: *//' | sort -u)
K3S_DNS_NAMES := $(filter-out %-db,$(K3S_APP_LABELS))


# installation
# -----
k3s_host_install_nixos:
	@echo "Installing K3s on ${HOST} via NixOS..."
	${MAKE} sync
	@mkdir -p ${K3S_TMP_DIR}
	@export SHOGGOTH_DOMAIN="${DOMAIN}" SHOGGOTH_GITHUB_ORG="${GITHUB_ORG}"; \
	envsubst '$${SHOGGOTH_DOMAIN}$${SHOGGOTH_GITHUB_ORG}' < shoggoth/shoggoth.nix > ${K3S_TMP_DIR}/shoggoth.nix
	${K3S_SCP_BASE} ${K3S_TMP_DIR}/shoggoth.nix ${USER}@${HOST_IP}:/tmp/shoggoth.nix
	${K3S_SSH} 'sudo cp /tmp/shoggoth.nix /etc/nixos/shoggoth.nix && rm /tmp/shoggoth.nix'
	${K3S_SSH} 'grep -q "shoggoth.nix" /etc/nixos/configuration.nix || { \
		echo ""; echo "Add this line to /etc/nixos/configuration.nix:"; \
		echo "  imports = [ ./shoggoth.nix ];"; \
		echo ""; echo "Then re-run: make k3s_host_install_nixos"; \
		exit 1; \
	}'
	${K3S_SSH} 'sudo nixos-rebuild switch'
	@echo ""
	@echo "K3s installed. Next: make k3s_client_kubeconfig"

k3s_client_install_alpine:
	su -c 'apk add kubectl helm k9s'

k3s_client_kubeconfig:
	@echo "Fetching kubeconfig from ${HOST}..."
	mkdir -p private
	${K3S_SSH} 'sudo cp /etc/rancher/k3s/k3s.yaml /tmp/k3s.yaml && sudo chmod 644 /tmp/k3s.yaml'
	ssh ${SSH_COMMON_ARGS} ${USER}@${HOST_IP} 'cat /tmp/k3s.yaml' > ${K3S_KUBECONFIG}
	${K3S_SSH} 'sudo rm /tmp/k3s.yaml'
	chmod 600 ${K3S_KUBECONFIG}
	@echo "Kubeconfig saved to ${K3S_KUBECONFIG}"

k3s_client_namespaces: k3s_tunnel_up
	@echo "Applying namespaces from ${K3S_MANIFESTS}/namespaces.yaml..."
	${K3S_KUBECTL} apply -f ${K3S_MANIFESTS}/namespaces.yaml


# connection & health
# -----

K3S_TUNNEL_LOG?=${K3S_TMP_DIR}/k3s-tunnel.log

k3s_tunnel_up:
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

k3s_tunnel_down:
	@if [ -f ${K3S_TUNNEL_PID} ] && kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null; then \
		kill $$(cat ${K3S_TUNNEL_PID}); \
		echo "Tunnel stopped (PID $$(cat ${K3S_TUNNEL_PID}))"; \
	else \
		echo "Tunnel not running"; \
	fi
	@rm -f ${K3S_TUNNEL_PID} ${K3S_TUNNEL_LOG}


# maintenance
# -----
k3s_stop: k3s_tunnel_up
	@echo "=== Stopping all shoggoth K3s services ==="
	${K3S_KUBECTL} delete deployment -n shoggoth --all --ignore-not-found=true
	${K3S_KUBECTL} delete statefulset -n shoggoth --all --ignore-not-found=true
	${K3S_KUBECTL} delete daemonset -n shoggoth --all --ignore-not-found=true
	${K3S_KUBECTL} delete job -n shoggoth --all --ignore-not-found=true
	${K3S_KUBECTL} delete svc -n shoggoth --all --ignore-not-found=true
	${K3S_KUBECTL} delete configmap -n shoggoth --all --ignore-not-found=true
	${K3S_KUBECTL} delete secret -n shoggoth --all --ignore-not-found=true
	${K3S_KUBECTL} delete configmap coredns-custom -n kube-system --ignore-not-found=true
	@echo "Waiting for all pods to terminate..."
	-KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth wait --for=delete pod --all --timeout=60s 2>/dev/null || true
	-KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth delete pod --all --grace-period=0 --force 2>/dev/null || true
	-KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth wait --for=delete pod --all --timeout=30s 2>/dev/null || true
	@echo "All services stopped."

# Start all shoggoth K3s services.
k3s_start: k3s_tunnel_up k3s_client_namespaces
	@echo "=== Starting all shoggoth K3s services ==="
	@export SHOGGOTH_DOMAIN="${DOMAIN}" SHOGGOTH_GITHUB_ORG="${GITHUB_ORG}"; \
	for f in ${K3S_ALL_MANIFESTS}; do \
		envsubst '$${SHOGGOTH_DOMAIN}$${SHOGGOTH_GITHUB_ORG}' < $$f | ${K3S_KUBECTL} apply -f - || exit 1; \
	done
	@echo "=== Restarting CoreDNS to pick up custom ConfigMap ==="
	@${K3S_KUBECTL} -n kube-system rollout restart deployment coredns 2>/dev/null || true

# Stop a single service by name: make k3s_stop_service SERVICE=web-external
# Deletes deployment, statefulset, and job with matching name, plus its service.
# Does not delete configmaps, secrets, or PVCs.
k3s_stop_service: k3s_tunnel_up
	@if [ -z "${SERVICE}" ]; then \
		echo "Usage: make k3s_stop_service SERVICE=<name>"; \
		echo "Available services: ${K3S_APP_LABELS}"; \
		exit 1; \
	fi
	@echo "=== Stopping service: ${SERVICE} ==="
	-${K3S_KUBECTL} delete deployment ${SERVICE} -n shoggoth --ignore-not-found=true
	-${K3S_KUBECTL} delete statefulset ${SERVICE} -n shoggoth --ignore-not-found=true
	-${K3S_KUBECTL} delete daemonset ${SERVICE} -n shoggoth --ignore-not-found=true
	-${K3S_KUBECTL} delete job ${SERVICE} -n shoggoth --ignore-not-found=true
	-${K3S_KUBECTL} delete svc ${SERVICE} -n shoggoth --ignore-not-found=true
	@echo "Waiting for ${SERVICE} pod to terminate..."
	-KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth wait --for=delete pod -l app=${SERVICE} --timeout=60s 2>/dev/null || true
	-KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth delete pod -l app=${SERVICE} --grace-period=0 --force 2>/dev/null || true
	@echo "Service ${SERVICE} stopped."

# Start a single service by name: make k3s_start_service SERVICE=web-external
# Applies only the manifest file(s) matching the service name.
k3s_start_service: k3s_tunnel_up
	@if [ -z "${SERVICE}" ]; then \
		echo "Usage: make k3s_start_service SERVICE=<name>"; \
		echo "Available services: ${K3S_APP_LABELS}"; \
		exit 1; \
	fi
	@echo "=== Starting service: ${SERVICE} ==="
	@export SHOGGOTH_DOMAIN="${DOMAIN}" SHOGGOTH_GITHUB_ORG="${GITHUB_ORG}"; \
	MANIFESTS="$$(grep -rl "app: ${SERVICE}" ${K3S_MANIFESTS} --include='*.yaml' | sort)"; \
	if [ -z "$$MANIFESTS" ]; then \
		echo "No manifest file found for service '${SERVICE}'"; \
		exit 1; \
	fi; \
	for f in $$MANIFESTS; do \
		echo "Applying $$f..."; \
		envsubst '$${SHOGGOTH_DOMAIN}$${SHOGGOTH_GITHUB_ORG}' < $$f | ${K3S_KUBECTL} apply -f - || exit 1; \
	done

# Restart a single service: make k3s_restart_service SERVICE=kestra
k3s_restart_service: k3s_tunnel_up
	${MAKE} k3s_stop_service SERVICE=${SERVICE}
	${MAKE} k3s_start_service SERVICE=${SERVICE}

# Sync host-side files that pods mount via hostPath but cannot be served
# via ConfigMap (binary content, .git, etc.).  Relies on `make sync` having
# rsynced the shoggoth/ tree to ~/shoggoth/ on the host.
K3S_HOST_PATHS := redmine-plugins:redmine/plugins workflow-scripts:workflow/scripts kestra-flows:workflow/kestra/flows

k3s_sync_host_paths:
	@echo "=== Syncing private secrets to host ==="
	${K3S_SCP_BASE} -r shoggoth/private ${USER}@${HOST_IP}:/tmp/shoggoth-private
	${K3S_SSH} 'sudo mkdir -p /var/lib/rancher/k3s/storage/shoggoth/private && sudo cp -a /tmp/shoggoth-private/. /var/lib/rancher/k3s/storage/shoggoth/private/ && rm -rf /tmp/shoggoth-private'
	@echo "=== Syncing Unbound blacklist ==="
	${K3S_SSH} 'sudo mkdir -p /var/lib/rancher/k3s/storage/shoggoth/unbound-blacklists'
	${K3S_SCP_BASE} shoggoth/unbound/dns-zone-blacklist/unbound/unbound-nxdomain.blacklist ${USER}@${HOST_IP}:/tmp/shoggoth-unbound-nxdomain.blacklist
	${K3S_SSH} 'sudo cp /tmp/shoggoth-unbound-nxdomain.blacklist /var/lib/rancher/k3s/storage/shoggoth/unbound-blacklists/unbound-nxdomain.blacklist && rm /tmp/shoggoth-unbound-nxdomain.blacklist'
	@echo "=== Syncing hostPath content ==="
	@cmds=""; \
	for entry in ${K3S_HOST_PATHS}; do \
		DST=$$(echo "$$entry" | cut -d: -f1); \
		SRC=$$(echo "$$entry" | cut -d: -f2); \
		cmds="$$cmds mkdir -p /var/lib/rancher/k3s/storage/shoggoth/$$DST && cp -a /home/${USER}/shoggoth/$$SRC/. /var/lib/rancher/k3s/storage/shoggoth/$$DST/ &&"; \
	done; \
	cmds="$${cmds% &&}"; \
	${K3S_SSH} "sudo bash -c '$$cmds'"

k3s_restart: k3s_tunnel_up
	${MAKE} k3s_stop
	@echo "Ensuring openbao pod is fully terminated before cleanup..."
	@KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth wait --for=delete pod -l app=openbao --timeout=30s 2>/dev/null || true
	@REMAINING=$$(KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth get pods -l app=openbao -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); \
	if [ -n "$$REMAINING" ]; then \
		echo "openbao pod still alive, force deleting: $$REMAINING"; \
		KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth delete pod $$REMAINING --grace-period=0 --force 2>/dev/null || true; \
		sleep 5; \
	fi
	${MAKE} sync
	${MAKE} k3s_sync_host_paths
	${MAKE} k3s_start


k3s_log: k3s_tunnel_up
	@if [ -z "$(POD)" ]; then echo "Usage: make k3s_log POD=<app-name> [CONTAINER=<name>]"; exit 1; fi
	@echo "=== $(POD) ==="
	@echo ""
	@echo "--- Status ---"
	@KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth get pod -l app=$(POD) -o wide 2>&1 || true
	@echo ""
	@echo "--- Container states ---"
	@KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth get pod -l app=$(POD) -o json 2>/dev/null | jq -r '.items[] | .metadata.name as $$pod | ("Pod: " + $$pod), (.status.initContainerStatuses // [] | .[] | "  INIT [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)"), (.status.containerStatuses // [] | .[] | "  MAIN  [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)")' 2>&1 || true
	@echo ""
	@echo "--- Logs (from host /var/log/pods) ---"
	@${K3S_SSH} "sudo find /var/log/pods/ -path '*/shoggoth_$(POD)-[0-9a-z]*/*.log' -exec sh -c ' \
		dir=\$$(dirname \"\$$1\"); \
		cname=\$$(basename \"\$$dir\"); \
		pod=\$$(basename \"\$$(dirname \"\$$dir\")\"); \
		sz=\$$(wc -c < \"\$$1\"); \
		echo \"  [\$$cname] (\$${sz} bytes):\"; \
		if [ \"\$$sz\" -gt 0 ]; then tail -1500 \"\$$1\" 2>&1; fi; \
		echo \"\"; \
	' _ {} \;" 2>&1 || echo "No pod logs found on host for $(POD)"

k3s_status: k3s_tunnel_up
	@echo "=== K3s shoggoth services ==="
	${K3S_KUBECTL} get pods,svc,deployment,statefulset,job,pvc -n shoggoth -o wide
	@echo ""
	@echo "=== Recent events ==="
	@KUBECONFIG=${K3S_KUBECONFIG} kubectl -n shoggoth get events --sort-by=.lastTimestamp 2>&1 | tail -20 || true
	@echo ""
	@echo "=== CoreDNS ==="
	${K3S_KUBECTL} get pods -n kube-system -l k8s-app=kube-dns
	@echo ""
	@echo "=== Pod readiness ==="
	@${K3S_KUBECTL} get pods -n shoggoth \
		-o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,STATUS:.status.phase \
		2>&1 || true
