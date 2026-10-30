export KUBECONFIG?=private/kubeconfig
K3S_TMP_DIR?=.tmp
K3S_TUNNEL_PID?=${K3S_TMP_DIR}/k3s-tunnel.pid

K3S_SSH=ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP}
K3S_API_PORT?=6443
K3S_TUNNEL_PORT?=6443

K3S_HOST_PATHS := redmine-plugins:redmine/plugins workflow-scripts:workflow/scripts kestra-flows:workflow/kestra/flows argo:workflow/argo private:private

SHOGGOTH_HELM_TIMEOUT?=10m
SHOGGOTH_HELM_TIMEOUT_UNINSTALL?=2m

# The namespace every helm command addresses the release in. It is a
# variable of its own because the offline lint renders retarget it per
# render (see make/lint.mk): a lint render must not be mistakable for a
# real install.
SHOGGOTH_HELM_NAMESPACE ?= $(INSTANCE)

# The flags shared by every helm command that reads the chart: the release
# namespace and the per-instance configuration file.
SHOGGOTH_HELM_COMMON_ARGS = --namespace $(SHOGGOTH_HELM_NAMESPACE) --values $(SHOGGOTH_CONFIG)

SHOGGOTH_HELM_STATUS = helm status shoggoth-$(INSTANCE) --namespace $(SHOGGOTH_HELM_NAMESPACE)

# `--take-ownership` lets Helm label-and-claim pre-existing resources that
# match a chart resource's (kind, name, namespace) but lack the
# `app.kubernetes.io/managed-by: Helm` label. Without this flag, the first
# install against a cluster that already has unmanaged copies of chart-owned
# resources aborts with `invalid ownership metadata`. After adoption the flag
# is idempotent for subsequent installs.
SHOGGOTH_HELM_UPGRADE = helm upgrade --install shoggoth-$(INSTANCE) $(SHOGGOTH_CHART_ROOT) \
	$(SHOGGOTH_HELM_COMMON_ARGS) \
	--create-namespace \
	--take-ownership \
	--timeout $(SHOGGOTH_HELM_TIMEOUT)

SHOGGOTH_HELM_UNINSTALL = helm uninstall shoggoth-$(INSTANCE) --namespace $(SHOGGOTH_HELM_NAMESPACE) --wait


# installation
# -----
host_install_nixos:
	echo "Installing K3s on ${HOST} via NixOS..."
	${MAKE} sync
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

client_kubeconfig:
	echo "Fetching kubeconfig from ${HOST}..."
	mkdir -p private
	echo "[sudo] password for ${USER}:"
	${K3S_SSH} 'sudo cat /etc/rancher/k3s/k3s.yaml' > ${KUBECONFIG}
	chmod 600 ${KUBECONFIG}
	echo "Kubeconfig saved to ${KUBECONFIG}"


# connection & health
# -----

K3S_TUNNEL_LOG?=${K3S_TMP_DIR}/k3s-tunnel.log

tunnel_up:
	mkdir -p ${K3S_TMP_DIR}
	if [ -f ${K3S_TUNNEL_PID} ] && kill -0 $$(cat ${K3S_TUNNEL_PID}) 2>/dev/null; then \
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
#
# The escalation lives in `down_helm_cleanup`, which this target runs
# UNCONDITIONALLY — after the uninstall attempt, not only when it failed.
# It is the backstop for everything the hook does not cover (a controller
# that was already unhealthy, a hook that could not run, a release without
# the hook).
down: tunnel_up
	$(SHOGGOTH_HELM_UNINSTALL) --timeout $(SHOGGOTH_HELM_TIMEOUT_UNINSTALL) 2>&1 \
		|| echo "WARN: helm uninstall failed, forced cleanup is going to take care of this."
	${MAKE} --no-print-directory down_helm_cleanup
	${MAKE} --no-print-directory k3s_purge_orphans \
		|| echo "WARN: the orphan purge failed; the release is uninstalled regardless."

# Strip controller finalizers from chart CRs that `helm uninstall --wait` is
# blocking on. Idempotent and non-destructive on the data plane. After this
# step, chart CRs become immediately deletable by helm uninstall / kubectl;
.PHONY: down_helm_cleanup
down_helm_cleanup: tunnel_up
	for kind in externalsecret pushsecret workflow workflowtemplate cronworkflow clusterworkflowtemplate workflowtaskset; do \
	    names=$$(kubectl get "$$kind" -n ${INSTANCE} -o json 2>/dev/null \
	        | jq -r --arg rel "shoggoth-$(INSTANCE)" --arg ns "${INSTANCE}" \
	          '.items[] | select((.metadata.deletionTimestamp != null) or (((.metadata.annotations["meta.helm.sh/release-name"]) // "") == $$rel and ((.metadata.annotations["meta.helm.sh/release-namespace"]) // "") == $$ns)) | .metadata.name' 2>/dev/null) ; \
	    if [ -z "$$names" ]; then continue; fi ; \
	    echo "  $$kind: $$names" ; \
	    for name in $$names; do \
	        if out=$$(kubectl patch -n ${INSTANCE} --type=merge \
	                -p '{"metadata":{"finalizers":[]}}' "$$kind/$$name" 2>&1); then \
	            : ; \
	        else \
	            case "$$out" in \
	                *"NotFound"*) : ;; \
	                *) echo "    ERROR: $$kind/$$name was not patched: $$out" ;; \
	            esac ; \
	        fi ; \
	    done ; \
	done ; \
	if $(SHOGGOTH_HELM_STATUS) >/dev/null 2>&1; then \
	    $(SHOGGOTH_HELM_UNINSTALL) --timeout 30s --no-hooks 2>&1; \
	fi

# Apply the CRDs straight from their upstream sources with kubectl.
#
# The CRDs are deliberately NOT in the chart. Helm's loader has no crds/ case,
# so anything placed there lands in Chart.Files, gets serialized into the
# release record, and blows past the 1048576-byte Secret.data cap — `helm
# upgrade --install` then fails before a single resource is created.
SHOGGOTH_ARGO_CRDS := $(CURDIR)/shoggoth/argo-workflows/manifests/base/crds/full/argoproj.io_*.yaml
SHOGGOTH_ESO_CRDS  := $(CURDIR)/shoggoth/external-secrets/deploy/crds/bundle.yaml
SHOGGOTH_CRD_FILES  = $(foreach f,$(wildcard $(SHOGGOTH_ARGO_CRDS)) $(SHOGGOTH_ESO_CRDS),-f $(f))

crds_apply: tunnel_up
	kubectl apply --server-side=true --force-conflicts $(SHOGGOTH_CRD_FILES) \
	    || { echo "FAIL: CRD apply failed"; exit 1; }
	kubectl wait --for=condition=Established --timeout=45s customresourcedefinition --all \
	    || echo "  WARN: not every CRD Established in 45s — check 'kubectl get crd'"

# Destroy an instance: namespace, PVCs, Secrets, host data, remote directory.
#
# Deliberately does NOT run `helm uninstall` first, unlike `make down`. The
# release's own record lives in the namespace, so deleting the namespace
# removes it anyway — and `helm uninstall` would additionally delete the
# cluster-scoped ClusterRoles/ClusterRoleBindings in argo-workflows.yaml,
# external-secrets.yaml and otelcol.yaml. This target deletes them
# explicitly instead (see below).
#
# The cluster-scoped RBAC is PER-INSTANCE (each name is prefixed with the
# release name — see shoggoth/chart/templates/otelcol.yaml), so the namespace
# deletion no longer takes them with it. They have to be deleted explicitly, or
# they leak: a cluster-scoped object is not garbage-collected with its
# namespace, and its ClusterRoleBinding would keep granting the (now-deleted)
# instance's ServiceAccount its permissions.
purge: tunnel_up
	test "${INSTANCE}" != "shoggoth"
	-kubectl delete namespace ${INSTANCE} --ignore-not-found=true --timeout=120s
	-kubectl delete clusterrole,clusterrolebinding \
		-l app.kubernetes.io/instance=shoggoth-$(INSTANCE) --ignore-not-found=true
	-${K3S_SSH} 'sudo rm -rf /var/lib/rancher/k3s/storage/${INSTANCE}'
	-${K3S_SSH} 'rm -rf ${REMOTE_PATH}/${INSTANCE}'

# Delete the External Secrets objects in the ${INSTANCE} namespace that the
# chart does not render any more. They are CRs, and no Helm release owns them,
# so no `helm uninstall` will ever remove them. The ESO store tokens are
# Secrets, not CRs, and are not touched here.
K3S_ORPHAN_RENDERED=${K3S_TMP_DIR}/orphans-rendered.txt
K3S_ORPHAN_LIVE=${K3S_TMP_DIR}/orphans-live.txt
K3S_ORPHAN_ORPHANS=${K3S_TMP_DIR}/orphans-orphans.txt
k3s_purge_orphans: tunnel_up
	mkdir -p $(K3S_TMP_DIR)
	helm template shoggoth-$(INSTANCE) $(SHOGGOTH_CHART_ROOT) \
		$(SHOGGOTH_HELM_COMMON_ARGS) \
		| yq -r 'select(.kind == "ExternalSecret" or .kind == "PushSecret" or .kind == "SecretStore") | .metadata.name' \
		| sed '/^---$$/d' | sort -u > $(K3S_ORPHAN_RENDERED)
	: > $(K3S_ORPHAN_LIVE)
	for kind in externalsecret pushsecret secretstore; do \
		kubectl get "$$kind" -n ${INSTANCE} -o name 2>/dev/null >> $(K3S_ORPHAN_LIVE) || true ; \
	done
	awk 'NR==FNR { rendered[$$1]=1; next } \
	     { name=$$1; sub(/^[^/]*\//, "", name); if (!(name in rendered)) print $$1 }' \
		$(K3S_ORPHAN_RENDERED) $(K3S_ORPHAN_LIVE) > $(K3S_ORPHAN_ORPHANS)
	# The empty-render guard is there because the comparison is "in the chart
	# or not": an empty rendered set would make every object in the namespace
	# look like an orphan.
	if [ -s $(K3S_ORPHAN_RENDERED) ] && [ -s $(K3S_ORPHAN_ORPHANS) ]; then \
		sed 's/^/  /' $(K3S_ORPHAN_ORPHANS); \
		xargs -r -n20 kubectl delete -n ${INSTANCE} --ignore-not-found=true < $(K3S_ORPHAN_ORPHANS); \
	fi

# Wipe OpenBao raft storage. Does NOT restart anything — run sync_restart after.
# Useful when the unseal key is lost or OpenBao data is corrupted.
wipe_openbao: tunnel_up
	-kubectl scale statefulset openbao -n ${INSTANCE} --replicas=0 2>/dev/null || true
	-kubectl wait --for=delete pod/openbao-0 -n ${INSTANCE} --timeout=60s 2>/dev/null || true
	PV_JSON=$$(kubectl get pv -o json | jq -r '.items[] | select(.spec.claimRef.name=="openbao-data" and .spec.claimRef.namespace=="${INSTANCE}")'); \
	PV_HOST_PATH=$$(printf '%s' "$${PV_JSON}" | jq -r '.spec.local.path // empty'); \
	PV_NAME=$$(printf '%s' "$${PV_JSON}" | jq -r '.metadata.name // empty'); \
	if [ -n "$${PV_HOST_PATH}" ]; then \
		${K3S_SSH} "sudo sh -c 'find \"$${PV_HOST_PATH}\" -mindepth 1 -exec rm -rf {} +'"; \
		WIPE_EXIT=$$?; \
		if [ $${WIPE_EXIT} -ne 0 ]; then \
			echo "ERROR: Host data wipe failed (exit code $${WIPE_EXIT}). Check sudo access."; \
			exit 1; \
		fi; \
	fi; \
	if [ -n "$${PV_NAME}" ]; then \
		kubectl patch pv "$${PV_NAME}" -p '{"metadata":{"finalizers":null}}' 2>/dev/null || true; \
		-kubectl delete pvc openbao-data -n ${INSTANCE} --ignore-not-found=true --wait=false 2>/dev/null; \
		-kubectl delete pv "$${PV_NAME}" --ignore-not-found=true --wait=false 2>/dev/null; \
		for i in 1 2 3 4 5 6 7 8 9 10; do \
			kubectl get pvc openbao-data -n ${INSTANCE} >/dev/null 2>&1 || break; \
			sleep 1; \
		done; \
	fi;
	-kubectl delete secret openbao-unseal-key -n ${INSTANCE} --ignore-not-found=true

# Start all shoggoth services.
#
# Two passes, one after the other:
#   Pass 1 (CRDs, server-side)  -> `make crds_apply`, every run.
#   Pass 2 (everything else)    -> `helm upgrade --install`.
#
# The namespace is created by THIS target, with kubectl, before the helm call;
# the chart owns the namespace OBJECT — its metadata — not its existence (see
# shoggoth/chart/templates/namespace.yaml). The kubectl call is not
# belt-and-braces, it is load-bearing, and -create-namespace cannot replace it.
up: tunnel_up crds_apply
	kubectl get namespace $(INSTANCE) >/dev/null 2>&1 \
		|| kubectl create namespace $(INSTANCE) >/dev/null
	$(SHOGGOTH_HELM_UPGRADE)
	$(SHOGGOTH_HELM_STATUS) | head -12

# `make start|stop|restart_service SERVICE=<x>` runs `helm upgrade
# --reuse-values` with `--set services.<x>.enabled=<bool>` to flip a single
# toggle.

.PHONY: start
start: tunnel_up
	$(SHOGGOTH_HELM_UPGRADE) --set services.${SERVICE}.enabled=true --reuse-values --wait

.PHONY: stop
stop: tunnel_up
	$(SHOGGOTH_HELM_UPGRADE) --set services.${SERVICE}.enabled=false --reuse-values --wait


# Restart a single service: stop + start, both via Helm. Depends on tunnel_up
# so the underlying `start`/`stop` calls reach the API; those two run the
# version gate themselves, so it is not repeated here.
.PHONY: restart_service
restart_service: tunnel_up
	$(MAKE) stop SERVICE=${SERVICE}
	$(MAKE) start SERVICE=${SERVICE}


log: tunnel_up
	test -n "$(SERVICE)" ] || (echo "Usage: make log SERVICE=<app-name> [CONTAINER=<name>]" && false)
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
#   make shell APP=kestra                   # first pod labeled app=kestra
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

# Drop a tag from the local zot registry cache.
#
# Triggers on-demand re-sync from upstream on the next pull of IMAGE:TAG.
# zot is configured with anonymous access, so no credentials are required.
# Requires network reachability to registry.${DOMAIN}:${REGISTRY_PORT}.
# Example: make drop_image_tag IMAGE=library/nginx TAG=latest
drop_image_tag:
	if [ -z "${IMAGE}" ] || [ -z "${TAG}" ]; then \
		echo "Usage: make drop_image_tag IMAGE=<repo> TAG=<tag>"; \
		echo "Example: make drop_image_tag IMAGE=library/nginx TAG=latest"; \
		exit 1; \
	fi
	echo "=== Dropping tag ${IMAGE}:${TAG} from registry.${DOMAIN} ==="
	RESP_FILE=$$(mktemp); \
	HTTP_CODE=$$(curl -sS -o "$${RESP_FILE}" -w '%{http_code}' \
		-X DELETE \
		-H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json' \
		"http://registry.${DOMAIN}:${REGISTRY_PORT}/v2/${IMAGE}/manifests/${TAG}"); \
	CURL_EXIT=$$?; \
	if [ $${CURL_EXIT} -ne 0 ]; then \
		echo "  FAIL: curl exited with $${CURL_EXIT}" >&2; \
		rm -f "$${RESP_FILE}"; \
		exit 1; \
	fi; \
	case "$${HTTP_CODE}" in \
		202|200|204) echo "  OK: tag dropped (HTTP $${HTTP_CODE})" ;; \
		404) echo "  OK: tag not present (HTTP 404, idempotent)" ;; \
		*) echo "  FAIL: HTTP $${HTTP_CODE}" >&2; \
			[ -s "$${RESP_FILE}" ] && cat "$${RESP_FILE}" >&2; \
			rm -f "$${RESP_FILE}"; \
			exit 1 ;; \
	esac; \
	rm -f "$${RESP_FILE}"
	echo "Done. Next pull of ${IMAGE}:${TAG} will re-sync from upstream."
