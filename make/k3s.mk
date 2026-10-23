export KUBECONFIG?=private/kubeconfig
K3S_TMP_DIR?=.tmp
K3S_TUNNEL_PID?=${K3S_TMP_DIR}/k3s-tunnel.pid

K3S_SSH=ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP}
K3S_API_PORT?=6443
K3S_TUNNEL_PORT?=6443

K3S_HOST_PATHS := redmine-plugins:redmine/plugins workflow-scripts:workflow/scripts kestra-flows:workflow/kestra/flows argo:workflow/argo private:private

# Helm chart variables. All shipped here so the chart's timeouts and
# `--values` plumbing are visible in one place
# rather than scattered across what used to be make/chart-helpers.mk. The
# chart's *location* (SHOGGOTH_CHART_ROOT) is defined in the Makefile instead:
# make/chore.mk names it in a prerequisite list, and a prerequisite list is
# expanded at parse time — this file is included after chore.mk, so a
# definition here would still be empty where chore.mk needs it.
# Vendored CRDs live OUTSIDE the chart, on purpose. Helm's loader puts
# everything under a chart's crds/ into Chart.Files, which is serialized as
# json:"files" into the release record; Helm stores that record as
# base64(gzip(json)) in the sh.helm.release.v1.* Secret and kube-apiserver
# caps Secret.data at 1048576 bytes. The CRDs exceed that with room to spare —
# a chart holding only them and a trivial template measures 1748240 bytes
# gzipped, no controllers included — so shipping them inside the chart makes
# `helm upgrade --install` fail with
# `Secret ... data: Too long: may not be more than 1048576 bytes`.
# `make crds_apply` applies them with kubectl instead (further down in this
# file), which carries the actual measurements.
# Helm install / upgrade timeout. OpenBao waiting on an unseal and Gitea
# waiting on host-key materialisation both legitimately take minutes, so
# upgrade-timeout is generous by default.
SHOGGOTH_HELM_TIMEOUT?=10m
# Helm uninstall timeout. ``make down`` / ``make sync_restart`` call
# ``helm uninstall --wait`` and get stuck when an ESO finalizer is held
# by a controller that's not running (the new chart just added 21
# ExternalSecret/PushSecret resources, so the controller has a long
# reconcile ahead on first install and a long teardown on uninstall).
# A stuck uninstall is never a legitimate "OpenBao is busy" — it is
# always a finalizer wedge that needs manual intervention, so the timeout
# is short: surface the wedge quickly rather than burning 10 minutes.
# Override on the command line with
#   make sync_restart SHOGGOTH_HELM_TIMEOUT_UNINSTALL=5m
# or hit it head-on with the unblock path: force-patch the finalizers to []
# before uninstalling.
SHOGGOTH_HELM_TIMEOUT_UNINSTALL?=2m

# Passed to every helm invocation. $(SHOGGOTH_CONFIG) is the per-instance
# configuration; service on/off toggles are passed via `--set` from each
# target's recipe (and Helm 3.13+ `--reuse-values` carries the rest of the
# state in the release Secret between calls).
SHOGGOTH_HELM_VALUES = --values $(SHOGGOTH_CONFIG)

# Values for the offline render target (`helm_lint`). This is just the
# instance config: the target only runs `helm lint` / `helm template`, which
# render to a file and never apply anything, so there is nothing here for a
# throwaway override to protect. A chart/test/values-lint.yaml used to sit
# here overriding identity.instanceDir "so lint never touches a real
# deployment's data" — that rationale was wrong (a hostPath is an inert string
# until something applies the manifest, and the target does not); the file is
# gone. The release name and namespace it passes (`shoggoth-lint`/`lint`)
# are what distinguish lint output from a real install.
SHOGGOTH_HELM_VALUES_LINT = $(SHOGGOTH_HELM_VALUES)


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
# Stop all shoggoth services.
#
# Now `helm uninstall`. The legacy target deleted workloads and ConfigMaps but
# deliberately preserved Secrets and PVCs, and that contract survives because:
#   - every PVC and every chart-owned Secret carries
#     `helm.sh/resource-policy: keep`, so Helm skips them;
#   - Secrets the External Secrets operator materialises at runtime are not in
#     the release manifest at all, so uninstall never sees them;
#   - the namespace is not in the release manifest (--create-namespace), so
#     uninstalling does not cascade-delete the namespace and take them anyway.
# What is left behind is exactly what legacy `down` left behind, plus the
# cluster-scoped CRDs (which legacy also left).
#
# Finalizer-wedge fallback. `helm uninstall --wait` blocks until every chart
# resource is gone; chart CRs (ExternalSecret, PushSecret, Argo Workflow,
# WorkflowTemplate, CronWorkflow, ClusterWorkflowTemplate, WorkflowTaskSet)
# all carry finalizers owned by their respective controllers. If any
# controller is in CrashLoopBackOff, mid-reconcile, or simply slow, the
# uninstall hits `context deadline exceeded` after `$(SHOGGOTH_HELM_TIMEOUT_UNINSTALL)`
# (2m by default) with the resources still present. That is what was wedging
# `make sync_restart` after the chart started shipping per-instance
# ExternalSecret/PushSecret resources.
#
# `make purge` is the destructive counterpart: it deletes the namespace.
#
# `down` escalation, controlled by `FORCE=1`:
#   - on timeout, drop the controller-held finalizers on every chart CR
#     (the CRs themselves go away as part of the resulting delete; only the
#     finalizer field is touched — the underlying K8s `Secret` data is NOT
#     cleared, because each chart-owned Secret still carries
#     `helm.sh/resource-policy: keep`);
#   - re-issue `helm uninstall` with a 30 s `--wait`; usually returns in <2 s
#     because the controller no longer blocks the delete;
#   - if even that fails, print the diagnosis the operator needs
#     (`kubectl get <kind> -n <ns> -o name | xargs ... patch ... finalizers=[]`)
#     instead of silently corrupting state.
#
# Default behaviour stays strict: `down` without `FORCE=1` errors out with a
# suggestion, so an operator who hit this by accident still gets a clear
# "you have stuck finalizers; here is how to see them" message.
down: tunnel_up
	echo "=== Uninstalling release shoggoth-$(INSTANCE) from namespace ${INSTANCE} ==="
	echo "    PVCs and Secrets are kept (helm.sh/resource-policy: keep)."
# Two release-gone paths need different handling:
#   1. `helm status` says no release exists at all → nothing to do, exit 0.
#      (typical: user called `make down` twice, or `make sync_restart` ran
#      after a prior failed run.)
#   2. `helm status` says release exists → proceed to uninstall attempt.
# The guard, the uninstall and the FORCE fallback below are ONE recipe line, and
# the backslashes are load-bearing: make runs each line in its own shell, so a
# variable the status check sets is empty on the next line, and an `exit` only
# leaves the shell that runs it. Only a genuinely failed uninstall may return
# non-zero — `sync_restart` halts on any non-zero exit from `make down`.
	if ! helm status shoggoth-$(INSTANCE) --namespace $(INSTANCE) >/dev/null 2>&1; then \
	    echo "  (release shoggoth-$(INSTANCE) not installed in namespace ${INSTANCE}; nothing to stop)"; \
	    exit 0; \
	fi; \
	if helm uninstall shoggoth-$(INSTANCE) --namespace $(INSTANCE) \
	    --wait --timeout $(SHOGGOTH_HELM_TIMEOUT_UNINSTALL) 2>&1; then \
	    echo "=== All workloads stopped; PVCs, Secrets and CRDs preserved ==="; \
	else \
	    rc=$$?; \
	    echo ""; \
	    echo "WARN: 'helm uninstall --wait --timeout $(SHOGGOTH_HELM_TIMEOUT_UNINSTALL)' failed (rc=$$rc)."; \
	    echo "      Stuck chart CRs (ExternalSecret, PushSecret, Argo Workflow/*) carry"; \
	    echo "      controller-owned finalizers that their controllers can't drain."; \
	    if [ "$(FORCE)" = "1" ]; then \
	        echo "FORCE=1 set — running force-cleanup fallback."; \
	        ${MAKE} --no-print-directory force-helm-cleanup ; \
	        echo "Retrying helm uninstall with a 30s wait..."; \
	        if helm uninstall shoggoth-$(INSTANCE) --namespace $(INSTANCE) \
	                --wait --timeout 30s 2>&1; then \
	            echo "=== Force-cleanup succeeded; all workloads stopped ==="; \
	        else \
	            rc2=$$?; \
	            if ! helm status shoggoth-$(INSTANCE) --namespace $(INSTANCE) >/dev/null 2>&1; then \
	                echo "=== Force-cleanup succeeded; release record was cleared by the prior attempt ==="; \
	            else \
	                echo ""; \
	                echo "ERROR: helm uninstall still failed after force-cleanup (rc=$$rc2)."; \
	                echo "       (Helm 3.x deletes the release record BEFORE --wait completes; a"; \
	                echo "        prior incomplete uninstall may have already cleared it. Re-run"; \
	                echo "        FORCE=1 after the chart CRs are GCed, or use 'make purge'.)"; \
	                echo "       Inspect:"; \
	                echo "         kubectl get externalsecret,pushsecret,workflow,workflowtemplate,cronworkflow \\"; \
	                echo "             -n ${INSTANCE} -o name"; \
	                echo "       For a destructive reset, use 'make purge' (deletes the namespace)."; \
	                exit $$rc2; \
	            fi; \
	        fi ; \
	    else \
	        echo "      Re-run with FORCE=1 to auto-clear finalizers, or do it by hand:"; \
	        echo "        kubectl get externalsecret,pushsecret,workflow,workflowtemplate,cronworkflow \\"; \
	        echo "            -n ${INSTANCE} -o name \\"; \
	        echo "          | xargs -r -n50 kubectl patch -n ${INSTANCE} --type=merge \\"; \
	        echo "              -p '{\"metadata\":{\"finalizers\":[]}}'"; \
	        echo "        helm uninstall shoggoth-$(INSTANCE) -n ${INSTANCE} --wait --timeout 30s"; \
	        echo "      Override the timeout with SHOGGOTH_HELM_TIMEOUT_UNINSTALL=<duration> make down."; \
	        exit $$rc; \
	    fi ; \
	fi

# force-helm-cleanup — strip controller finalizers from chart CRs that
# `helm uninstall --wait` is blocking on. Idempotent and non-destructive on
# the data plane: only the `metadata.finalizers` field is patched away; the
# CRs themselves, K8s Secrets/PVCs/configmaps, and OpenBao raft data are all
# untouched. After this helper returns, chart CRs become immediately
# deletable by helm uninstall / kubectl.
#
# Targets the seven CR kinds observed to carry finalizers in a shoggoth
# install (verified against argo-workflows v3 / external-secrets v2.11.0):
#   - externalsecret / pushsecret      (ESO controller)
#   - workflow / workflowtemplate / cronworkflow / clusterworkflowtemplate
#                                    (argo-workflows controller)
#   - workflowtaskset                  (argo-workflows controller, for
#                                      `Workflow`-parallelism bookkeeping)
# Each is patched with `-p '{"metadata":{"finalizers":[]}}'`. JSON-merge
# replaces arrays wholesale, so this works on every state of the field:
#   - finalizers not present           → field is created as `[]`
#   - finalizers already `[]`          → no-op
#   - finalizers carries N entries     → array is replaced with `[]`
# The earlier draft used ``{"metadata":{"finalizers":null}}``, which the
# JSON-merge spec treats as "leave the field alone" rather than "replace
# with null", so a non-empty ``finalizers`` list survived the patch —
# which is what was wedging ``helm uninstall --wait`` on the
# newly-chartified ESO CRs. Verified against an actual stuck
# ``ExternalSecret/ssh-slave-private-key`` whose
# ``metadata.finalizers == ["externalsecrets.external-secrets.io/externalsecret-cleanup"]``
# before the fix and ``[]`` after.
#
# `-n50 -P4` keeps individual `kubectl patch` arguments bounded so the
# command line stays well under ARG_MAX, and runs four patches in
# parallel; on a 21-CR release this finishes in under a second.
.PHONY: force-helm-cleanup
force-helm-cleanup: tunnel_up
	echo "=== Force-clearing chart-CR finalizers (non-destructive on data plane) ==="
	for kind in externalsecret pushsecret workflow workflowtemplate cronworkflow clusterworkflowtemplate workflowtaskset; do \
	    names=$$(kubectl get "$$kind" -n ${INSTANCE} -o name 2>/dev/null) ; \
	    if [ -z "$$names" ]; then continue; fi ; \
	    echo "  $$kind: $$names" ; \
	    echo "$$names" | xargs -r -n50 -P4 kubectl patch -n ${INSTANCE} --type=merge \
	        -p '{"metadata":{"finalizers":[]}}' 2>&1 | grep -Ev 'not found|no resources' || true ; \
	done
	echo "=== Finalizers cleared; chart CRs are now deletable ==="

# Idempotent variant of `down` for callers whose semantics are
# "converge to fresh state, regardless of what's deployed." The strict
# behaviour of `make down` — error out if there is no release — is
# correct for direct use (you may have meant `make purge`); it is the
# wrong contract for `make sync_restart` and `make shutdown`, both of
# which run on a freshly-installed or freshly-purged cluster where
# `helm status` legitimately returns non-zero. Using `-` to swallow all
# errors would mask real helm/connectivity failures, so this checks
# status first and only invokes `make down` when a release actually
# exists.
#
# `make down`'s own status guard still runs (it is what makes the error
# message match); this wrapper just turns that error into a no-op for
# the two callers that need it.
.PHONY: down_if_running
down_if_running: tunnel_up
	if helm status shoggoth-$(INSTANCE) --namespace $(INSTANCE) >/dev/null 2>&1; then \
		${MAKE} down; \
	else \
		echo "No release 'shoggoth-$(INSTANCE)' in namespace '$(INSTANCE)'; nothing to stop."; \
	fi

# Apply the CRDs straight from their upstream sources with kubectl.
#
# No vendored copy, no generator script: kubectl reads both sources natively —
# the Argo CRDs are one-file-per-CRD in the submodule, and the External Secrets
# bundle is a single multi-document file that `kubectl apply` splits itself.
# This previously copied 33 files into shoggoth/crds/ via crd_populate.py,
# which also stamped `helm.sh/hook: crd-install` onto every one of them.
#
# Those annotations are dead weight: they only mattered for Helm <= 3.6
# installing a chart's crds/ directory, and the CRDs are deliberately NOT in
# the chart. Helm's loader has no crds/ case, so anything placed there lands in
# Chart.Files, gets serialized into the release record, and blows past the
# 1048576-byte Secret.data cap — `helm upgrade --install` then fails before a
# single resource is created.
#
# This is arithmetic, not a Helm-version quirk, so no upgrade rescues it: a
# chart holding only these CRDs and one trivial template measures 1748240
# bytes gzipped — 699664 OVER the cap, with no controllers included. Four Argo
# CRDs are 2.6-2.9 MB each on their own, and Argo upstream ships no compact
# variant (`manifests/base/crds/` contains only `full/`). Subcharts do not
# help either: a subchart's templates land in the *parent's* release record.
#
# The glob matters: `kubectl apply -f <dir>` would also pick up the
# kustomization.yaml sitting alongside the CRDs, which is not a resource.
#
# Server-side apply is required by upstream, not just advisable: five of the
# eight Argo CRDs exceed the 262144-byte last-applied-configuration annotation
# cap that a client-side `kubectl apply` would have to write, and Argo's own
# `manifests/base/crds/full/README.md` mandates it for the same reason.
#
# Hence SHOGGOTH_CRD_FILES, which repeats the flag per file. `kubectl` takes exactly
# one argument per -f, and make does not glob an assignment: SHOGGOTH_ARGO_CRDS holds
# the literal pattern until the shell expands it inside the recipe, by which
# point the written-down pairing of `-f <first>` has already happened. The seven
# remaining Argo paths then arrive as positional arguments and the command dies
# with "error: Unexpected args: [...]" before a single CRD is applied. Resolving
# the glob in make ($(wildcard)) is what makes the repeated flag expressible.
SHOGGOTH_ARGO_CRDS := $(CURDIR)/shoggoth/argo-workflows/manifests/base/crds/full/argoproj.io_*.yaml
SHOGGOTH_ESO_CRDS  := $(CURDIR)/shoggoth/external-secrets/deploy/crds/bundle.yaml
SHOGGOTH_CRD_FILES  = $(foreach f,$(wildcard $(SHOGGOTH_ARGO_CRDS)) $(SHOGGOTH_ESO_CRDS),-f $(f))

crds_apply: tunnel_up
	test -f "$(SHOGGOTH_ESO_CRDS)" || { echo "FAIL: $(SHOGGOTH_ESO_CRDS) not found"; exit 1; }
	ls $(SHOGGOTH_ARGO_CRDS) >/dev/null 2>&1 || { echo "FAIL: no CRDs at $(SHOGGOTH_ARGO_CRDS)"; exit 1; }
	echo "=== Applying CRDs (server-side) ==="
	kubectl apply --server-side=true --force-conflicts $(SHOGGOTH_CRD_FILES) \
		|| { echo "FAIL: CRD apply failed"; exit 1; }
	echo "Waiting for CRDs to become Established..."
# Wait on every CRD in the cluster, not just the 33 this target applies:
# `kubectl wait` needs no names, so extracting them (a yq parse of 14 MB of
# YAML) bought only a per-CRD warning message — while costing a 45s timeout
# PER CRD, serially, so ~24 minutes when nothing establishes. This is one
# parallel wait, one 45s budget. The trade: a CRD wedged by an unrelated
# release now warns without naming itself, so the message points at the list.
	kubectl wait --for=condition=Established --timeout=45s customresourcedefinition --all \
		|| echo "  WARN: not every CRD Established in 45s — check 'kubectl get crd'"

# Destroy an instance: namespace, PVCs, Secrets, host data, remote directory.
#
# Deliberately does NOT run `helm uninstall` first, unlike `make down`. The
# release's own record lives in the namespace, so deleting the namespace
# removes it anyway — and `helm uninstall` would additionally delete the
# cluster-scoped ClusterRoles/ClusterRoleBindings in argo-workflows.yaml and
# otelcol.yaml. Those carry upstream names rather than per-instance names, so a
# second instance on the same cluster is using the same objects. Legacy `purge`
# left them alone; so does this.
purge: tunnel_up
	if [ "${INSTANCE}" = "shoggoth" ]; then \
		echo "ERROR: Refusing to purge the default 'shoggoth' instance."; \
		echo "       Set INSTANCE=<name> to purge a specific instance."; \
		exit 1; \
	fi
	echo "=== Purging shoggoth instance '${INSTANCE}' (namespace, PVCs, host data) ==="
	echo "Deleting namespace ${INSTANCE} (all workloads, secrets, PVCs)..."
	-kubectl delete namespace ${INSTANCE} --ignore-not-found=true --timeout=120s
	echo "Removing hostPath data..."
	-${K3S_SSH} 'sudo rm -rf /var/lib/rancher/k3s/storage/${INSTANCE}'
	echo "Removing remote deployment directory..."
	-${K3S_SSH} 'rm -rf ${REMOTE_PATH}/${INSTANCE}'
	echo "Instance '${INSTANCE}' purged."

# Purge all Kubernetes Secrets in the ${INSTANCE} namespace.
# Removes OpenBao-seeded secrets, TLS cert secrets, the unseal-key backup,
# and the bootstrap marker. PVC data and the OpenBao raft are untouched —
k3s_purge_secrets: tunnel_up
	echo "=== Purging all Kubernetes Secrets in namespace '${INSTANCE}' ==="
	-kubectl delete secret --all -n ${INSTANCE} --ignore-not-found=true
	echo "All Secrets in '${INSTANCE}' purged."

# Check existence and contents of a specific Kubernetes Secret in ${INSTANCE}.
# Prints namespace, type, age, data keys, and decoded values of every key.
# Exits 0 if the Secret exists, 1 if missing, 2 on kubectl/parse errors.
#
# Usage: make k3s_check_secret SECRET=<name>
# Example: make k3s_check_secret SECRET=redmine-slave-token
K3S_CHECK_SECRET_JQ_SUMMARY = "\n" + "  namespace: " + $$ns + "\n" + "  name:      " + .metadata.name + "\n" + "  type:      " + (.type // "<unknown>") + "\n" + "  age:       " + (.metadata.creationTimestamp // "unknown" | sub("T"; " ") | sub("Z"; "")) + "\n" + "  data keys: " + (if (.data // {}) | length == 0 then "(none)" else (.data | keys | join(", ")) end) + "\n\n"
K3S_CHECK_SECRET_JQ_VALUES = .data // {} | to_entries[] | "  [\(.key)] (decoded; stored in K8s as \(.value | length) base64 chars):\n    \(.value | @base64d)"
k3s_check_secret: tunnel_up
	if [ -z "${SECRET}" ]; then \
		echo "Usage: make k3s_check_secret SECRET=<name>"; \
		echo "Example: make k3s_check_secret SECRET=redmine-slave-token"; \
		exit 2; \
	fi
	echo "=== Checking Secret '${SECRET}' in namespace '${INSTANCE}' ==="
	RESP_FILE=$$(mktemp); \
	HTTP_CODE=$$(kubectl get secret "${SECRET}" -n "${INSTANCE}" -o json --request-timeout=10s > "$${RESP_FILE}" 2>${K3S_TMP_DIR}/k3s-check-secret.err && echo 200 || echo "$$?"); \
	if [ "$${HTTP_CODE}" != "200" ]; then \
		if [ "$${HTTP_CODE}" = "1" ]; then \
			echo "  NOT FOUND: Secret '${SECRET}' does not exist in namespace '${INSTANCE}'"; \
			echo "  (kubectl exit 1: not found / no resources / cluster unreachable)"; \
			[ -s ${K3S_TMP_DIR}/k3s-check-secret.err ] && sed 's/^/  kubectl: /' ${K3S_TMP_DIR}/k3s-check-secret.err; \
			rm -f "$${RESP_FILE}" ${K3S_TMP_DIR}/k3s-check-secret.err; \
			exit 1; \
		else \
			echo "  ERROR: kubectl failed with exit code $${HTTP_CODE}"; \
			[ -s ${K3S_TMP_DIR}/k3s-check-secret.err ] && sed 's/^/  kubectl: /' ${K3S_TMP_DIR}/k3s-check-secret.err; \
			rm -f "$${RESP_FILE}" ${K3S_TMP_DIR}/k3s-check-secret.err; \
			exit 2; \
		fi; \
	fi; \
	rm -f ${K3S_TMP_DIR}/k3s-check-secret.err; \
	jq -r --arg ns "${INSTANCE}" '${K3S_CHECK_SECRET_JQ_SUMMARY}' "$${RESP_FILE}"; \
	jq -r '${K3S_CHECK_SECRET_JQ_VALUES}' "$${RESP_FILE}"; \
	rm -f "$${RESP_FILE}"; \
	echo ""; echo "  Secret '${SECRET}' exists."; exit 0

# Wipe OpenBao raft storage. Does NOT restart anything — run sync_restart after.
# Useful when the unseal key is lost or OpenBao data is corrupted.
wipe_openbao: tunnel_up
	echo "=== Wiping OpenBao raft storage (namespace: ${INSTANCE}) ==="
	echo "Scaling down openbao StatefulSet..."
	-kubectl scale statefulset openbao -n ${INSTANCE} --replicas=0 2>/dev/null || true
	echo "Waiting for openbao-0 to terminate..."
	-kubectl wait --for=delete pod/openbao-0 -n ${INSTANCE} --timeout=60s 2>/dev/null || true
	PV_JSON=$$(kubectl get pv -o json | jq -r '.items[] | select(.spec.claimRef.name=="openbao-data" and .spec.claimRef.namespace=="${INSTANCE}")'); \
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
	echo "Deleting unseal key secret..."
	-kubectl delete secret openbao-unseal-key -n ${INSTANCE} --ignore-not-found=true
	echo "Done. Run 'make sync_restart' to re-deploy."

# Start all shoggoth services.
#
# Replaces the legacy two-pass `envsubst | kubectl apply --server-side`:
#   Pass 1 (CRDs, server-side)  -> `make crds_apply`, every run.
#   Pass 2 (everything else)    -> `helm upgrade --install`.
#
# Pass 1 is no longer conditional on install-vs-upgrade. It used to be, because
# Helm installs a chart's crds/ on `helm install` and never on `helm upgrade`,
# so the upgrade half of legacy Pass 1 needed its own target. The CRDs no longer
# live in the chart at all (they are too large — see crds_apply), so this is
# now simply the first thing `up` does. It must stay ahead of the helm call:
# the templates render ExternalSecret and Workflow objects that the API server
# would reject if their CRDs were not Established yet.
#
# The namespace is created by the Helm CLI (--create-namespace), not by the
# chart. That is deliberate: a Namespace inside the release manifest is deleted
# by `helm uninstall`, and deleting the namespace cascades to every PVC and
# Secret in it — turning `make down` into `make purge`. See
# shoggoth/chart/templates/external-secrets.yaml.
#
# WAIT=1 blocks until every workload is Ready. Legacy `make up` did not wait
# (it returned as soon as the apply was accepted), and waiting by default would
# turn a service that legitimately takes minutes to become ready — OpenBao
# waiting on an unseal, Gitea waiting on host keys — into a failed command.
up: tunnel_up crds_apply helm_version_gate
	if ! printf '%s' "${INSTANCE}" | grep -qE '^[a-z0-9][a-z0-9-]*[a-z0-9]$$|^[a-z0-9]$$'; then \
		echo "ERROR: INSTANCE must be lowercase alphanumeric with hyphens (RFC 1123), got: '${INSTANCE}'"; exit 1; \
	fi
	if [ $$(printf '%s' "${INSTANCE}" | wc -c) -gt 54 ]; then \
		echo "ERROR: '${INSTANCE}' exceeds 53 chars (namespace limit)"; exit 1; \
	fi
	echo "=== Installing release shoggoth-$(INSTANCE) into namespace ${INSTANCE} ==="
	if [ ! -f "$(SHOGGOTH_CONFIG)" ]; then \
		echo "ERROR: $(SHOGGOTH_CONFIG) does not exist."; \
		echo "       It is the per-instance configuration and the source of truth for"; \
		echo "       'make up', which always passes it as --values $(SHOGGOTH_CONFIG)."; \
		echo "       helm fails on a missing --values file, so there is no fallback to"; \
		echo "       the chart defaults in shoggoth/chart/values.yaml."; \
		echo "       Create it (INSTANCE=$(INSTANCE)), or point at another one:"; \
		echo "         make up SHOGGOTH_CONFIG=shoggoth/<instance>.yaml"; \
		exit 1; \
	fi
	helm upgrade --install shoggoth-$(INSTANCE) $(SHOGGOTH_CHART_ROOT) \
		--namespace $(INSTANCE) \
		--create-namespace \
		--take-ownership \
		$(SHOGGOTH_HELM_VALUES) \
		$(if $(WAIT),--wait,) \
		--timeout $(SHOGGOTH_HELM_TIMEOUT)
# PodSecurity baseline is incompatible with the chart — multiple Deployments
# (slave-dind, wireguard, localai, openbao-bootstrap, dind) use hostPath
# volumes + privileged containers + hostNetwork + capabilities that
# violate `baseline` by design. The legacy chart's `templates/00-namespace.yaml`
# applied `enforce=baseline`, which made every Deployment's pods
# `FailedCreate` with `forbidden: violates PodSecurity "baseline:latest"`.
# Strip the enforce label (and any audit/warn labels) from the namespace
# so the cluster-default policy applies. `kubectl label ... key-` removes
# a label; the trailing `-` is what does it.
	kubectl label namespace $(INSTANCE) \
		pod-security.kubernetes.io/enforce- \
		pod-security.kubernetes.io/warn- \
		pod-security.kubernetes.io/audit- \
		--overwrite >/dev/null 2>&1 || true
	echo "=== Release shoggoth-$(INSTANCE) applied to namespace ${INSTANCE} ==="
	helm status shoggoth-$(INSTANCE) --namespace $(INSTANCE) | head -12

# ---- per-service routing ----------------------------------------------------
#
# `make start|stop|restart_service SERVICE=<x>` runs `helm upgrade
# --reuse-values` with `--set services.<x>.enabled=<bool>` to flip a single
# toggle.
#
# SERVICE=<x> is the only identifier: the values key under .services.* is
# spelled exactly like the legacy `app:` label (e.g. `gitea-runner`), so no
# translation, table, or shim variable is needed. There is no per-service row
# in this makefile.
#
# State persistence is delegated to Helm itself (3.13+): `--reuse-values`
# reads back the last release's computed values from the cluster's
# `sh.helm.release.v1.*` Secret, then the `--set` and `--values`
# $(SHOGGOTH_CONFIG) layer on top. Last write wins — so an operator edit to
# shoggoth/<instance>.yaml survives a service toggle on the same call, and
# a service toggle persists across later calls that touch only the config
# file. Inspect the running state with `helm get values shoggoth-$(INSTANCE)`.
#
# Validation of SERVICE= runs against the chart itself: the literal key must
# exist under .services.* in $(SHOGGOTH_CHART_ROOT)/values.yaml. The chart is the
# single source of truth, so adding a new service is one edit in two places
# (values.yaml + a template that reads it), not three (those two plus this
# makefile).

# `--take-ownership` lets Helm label-and-claim pre-existing resources that
# match a chart resource's (kind, name, namespace) but lack the
# `app.kubernetes.io/managed-by: Helm` label. Needed for the first
# `helm upgrade --install` on a cluster that previously ran the legacy
# envsubst path, because that path produced
# SAs / Roles / RoleBindings / Cluster(Role|Binding)s without those
# labels. Without this flag, the first install against a cluster that
# already has unmanaged copies of chart-owned resources aborts with
# `invalid ownership metadata`. After adoption the labels are correct,
# so the flag is idempotent for subsequent installs.
#
# `--reuse-values` (Helm 3.13+) carries the last release's values forward.
# On a non-existent release, helm treats it as a no-op and falls through to
# install mode using $(SHOGGOTH_HELM_VALUES) + any per-call --set.
SHOGGOTH_HELM_INSTALL_BASE = helm upgrade --install shoggoth-$(INSTANCE) $(SHOGGOTH_CHART_ROOT) \
	--namespace $(INSTANCE) \
	--create-namespace \
	--take-ownership \
	--reuse-values \
	$(SHOGGOTH_HELM_VALUES)


# Minimum Helm binary version. `helm upgrade` gained --take-ownership in
# 3.17.0; every target that reaches the cluster through
# SHOGGOTT_HELM_INSTALL_BASE passes that flag, so an older helm aborts with
# "unknown flag: --take-ownership" and takes the whole per-service surface
# (up / start / stop / restart_service) down with it. Checked by
# `helm_version_gate`, which is a prerequisite of all four.
SHOGGOTH_HELM_MIN_VERSION?=3.17.0

# Version gate. Helm prints its own version as `v3.17.0+g<sha>`; strip the
# leading v and the build metadata, then compare with `sort -V`, whose first
# line is the smaller of the two. Equal means "at or above the floor".
# A missing helm yields an empty string, which sorts first and fails the
# comparison, so "helm not installed" lands in the same error path.
.PHONY: helm_version_gate
helm_version_gate:
	@v=$$(helm version --short 2>/dev/null | sed 's/^v//; s/+.*//'); \
	if [ -z "$$v" ]; then \
	    echo "ERROR: 'helm version' produced no output — helm is not installed or not on PATH."; \
	    echo "       'make up' / 'make start' / 'make stop' need helm >= $(SHOGGOTH_HELM_MIN_VERSION)."; \
	    exit 1; \
	fi; \
	lowest=$$(printf '%s\n%s\n' "$(SHOGGOTH_HELM_MIN_VERSION)" "$$v" | sort -V | head -1); \
	if [ "$$lowest" != "$(SHOGGOTH_HELM_MIN_VERSION)" ]; then \
	    echo "ERROR: helm $$v is older than the required $(SHOGGOTH_HELM_MIN_VERSION)."; \
	    echo "       --take-ownership (used by up/start/stop/restart_service to adopt the"; \
	    echo "       pre-chart resources) landed in helm 3.17.0; older versions abort with"; \
	    echo "       'unknown flag: --take-ownership'. Check 'helm version' on this host."; \
	    exit 1; \
	fi

# Start a single service via Helm. Passes `--set services.<label>.enabled=true`
# to flip the toggle; the rest of the values are reused from the prior
# release. Re-running on an already-started service re-renders the same
# manifests; `helm upgrade --wait` returns once they're Ready.
#
# tunnel_up is a prerequisite: helm upgrade with --wait talks to the API
# through the SSH tunnel, the same way `make up` does. helm_version_gate is
# the other one, because this recipe passes --take-ownership (Helm 3.17.0+).
#
# No kubectl wait here: `--wait --timeout $(SHOGGOTH_HELM_TIMEOUT)` on the helm
# invocation already waits for every Deployment/StatefulSet to be Ready
# before this recipe returns.
.PHONY: start
start: tunnel_up helm_version_gate
	$(SHOGGOTH_HELM_INSTALL_BASE) --set services.${SERVICE}.enabled=true --wait --timeout $(SHOGGOTH_HELM_TIMEOUT)


# Stop a single service via Helm. --set services.<label>.enabled=false flips
# the toggle; helm upgrade then deletes the resources the chart owns for that
# service. No PVC is ever deleted: they carry helm.sh/resource-policy: keep.
.PHONY: stop
stop: tunnel_up helm_version_gate
	$(SHOGGOTH_HELM_INSTALL_BASE) --set services.${SERVICE}.enabled=false --wait --timeout $(SHOGGOTH_HELM_TIMEOUT)


# Restart a single service: stop + start, both via Helm. Depends on tunnel_up
# so the underlying `start`/`stop` calls reach the API; those two run the
# version gate themselves, so it is not repeated here.
.PHONY: restart_service
restart_service: tunnel_up
	$(MAKE) stop SERVICE=${SERVICE}
	$(MAKE) start SERVICE=${SERVICE}


log: tunnel_up
	if [ -z "$(SERVICE)" ]; then echo "Usage: make log SERVICE=<app-name> [CONTAINER=<name>]"; exit 1; fi
	echo "=== $(SERVICE) ==="
	echo ""
	kubectl -n ${INSTANCE} describe pod -l app=$(SERVICE)
	echo "--- Status ---"
	kubectl -n ${INSTANCE} get pod -l app=$(SERVICE) -o wide 2>&1 || true
	echo ""
	echo "--- Container states ---"
	kubectl -n ${INSTANCE} get pod -l app=$(SERVICE) -o json 2>/dev/null | jq -r '.items[] | .metadata.name as $$pod | ("Pod: " + $$pod), (.status.initContainerStatuses // [] | .[] | "  INIT [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)"), (.status.containerStatuses // [] | .[] | "  MAIN  [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)")' 2>&1 || true
	echo ""
	echo "=== Recent events ==="
	kubectl -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -100 | grep "${SERVICE}" || true
	echo ""
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
	echo "=== Executing '$${CMD_VAL}' in $${POD_VAL} ==="; \
	exec kubectl exec -n ${INSTANCE} $${TTY_FLAGS} $${POD_VAL} -- $${CMD_VAL}

status: tunnel_up
	echo "=== Recent events ==="
	kubectl -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -20 || true
	echo ""
	echo "=== External Secrets Operator (namespace: ${INSTANCE}) ==="
	CA="$$(kubectl get validatingwebhookconfiguration externalsecret-validate -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null)"; \
	if [ -n "$$CA" ]; then printf "caBundle injected:    yes\n"; else printf "caBundle injected:    no\n"; fi
	CERT=$$(kubectl -n ${INSTANCE} get secret external-secrets-webhook -o name 2>/dev/null | wc -l); \
	if [ "$$CERT" -gt 0 ]; then printf "cert Secret present:  yes\n"; else printf "cert Secret present:  no\n"; fi
	printf "SecretStores:         %s\n" "$$(kubectl get secretstore -n ${INSTANCE} --no-headers 2>/dev/null | wc -l)"
	printf "ExternalSecrets:      %s\n" "$$(kubectl get externalsecret -n ${INSTANCE} --no-headers 2>/dev/null | wc -l)"
	echo ""
	echo "=== CoreDNS ==="
	kubectl get pods -n kube-system -l k8s-app=kube-dns
	echo ""
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
# Idempotent: HTTP 404 (tag not present) is treated as success.
#
# Usage: make drop_image_tag IMAGE=<repo> TAG=<tag>
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


# chart validation
# -----
# Offline chart smoke test. Runs helm directly against the chart without
# touching the cluster, so it has no `tunnel_up` prerequisite and no
# `INSTANCE` gating. It is operator-driven: there is no CI invocation wired
# into this repo.

# Manual lint entry point. `helm lint` catches Go-template parse errors and
# malformed YAML; the render that follows catches templates that parse but
# emit invalid or duplicated YAML. Run this after touching any template.
#
# The per-service `enabled` gates are checked by the toggle sweep at the end
# of this target, which renders once per `services.<key>` in values.yaml and
# fails if disabling that key alone removes nothing.
#
# This used to be joined by a `chart-validate` target distinguished only by
# release name and namespace, and by dropping this target's duplicate-key
# check — so it passed on charts this one rejected. It is gone; `helm_lint`
# is the offline entry point, because helm is not installed in every dev
# container.
.PHONY: helm_lint
helm_lint:
	mkdir -p ${K3S_TMP_DIR}
	echo "=== helm lint --strict ==="
	helm lint $(SHOGGOTH_CHART_ROOT) --strict
	echo "=== helm template parse check (all services enabled, overrides supplied) ==="
	helm template shoggoth-lint $(SHOGGOTH_CHART_ROOT) \
		--namespace lint \
		$(SHOGGOTH_HELM_VALUES_LINT) \
		> ${K3S_TMP_DIR}/shoggoth-lint-render.yaml \
		&& echo "  OK: rendered $$(wc -l < ${K3S_TMP_DIR}/shoggoth-lint-render.yaml) lines -> ${K3S_TMP_DIR}/shoggoth-lint-render.yaml" \
		|| { echo "FAIL: helm template failed"; exit 1; }
	echo "=== rendered-YAML duplicate-key check ==="
# Regression guard for the duplicate-label-key bug fixed in
# argo-webhook-receiver, maintenance-secret-writer, sandbox-spawner,
# external-secrets-webhooks, and reloader — every template was emitting
# `app.kubernetes.io/part-of: shoggoth` literally after invoking
# `shoggoth.labels`, producing invalid YAML 1.2 (duplicate mapping keys)
# that breaks kubeconform -strict and any strict YAML 1.2 parser.
#
# Three things keep this guard honest about what it is checking:
#   * The config enables key-duplicates and NOTHING else. `extends: default`
#     would drag in every default rule, so an unrelated `colons` violation
#     (too many spaces after a colon) exits non-zero and used to be reported
#     here as a duplicate-key regression.
#   * yamllint is located first. A missing binary returns 127, which would
#     otherwise print the duplicate-keys FAIL and blame the chart for a
#     missing linter.
#   * The FAIL block prints yamllint's own output and does not assert a cause
#     beyond the rule name, so whatever it reports can be read directly.
	if ! command -v yamllint >/dev/null 2>&1; then \
		echo "  FAIL: yamllint is not installed; the duplicate-key check did not run."; \
		echo "        pip install yamllint (or apt install yamllint) and re-run."; \
		exit 1; \
	fi; \
	if yamllint -d '{rules: {key-duplicates: enable}}' \
		${K3S_TMP_DIR}/shoggoth-lint-render.yaml > ${K3S_TMP_DIR}/shoggoth-lint-dupes.txt 2>&1; then \
		echo "  OK: no duplicate mapping keys in $$(wc -l < ${K3S_TMP_DIR}/shoggoth-lint-render.yaml)-line rendered manifest"; \
	else \
		echo "  FAIL: rendered manifest has duplicate mapping keys"; \
		echo "        (yamllint key-duplicates violations, see $(K3S_TMP_DIR)/shoggoth-lint-dupes.txt)"; \
		echo "        Helm templates must not emit literal labels/annotations that"; \
		echo "        the shoggoth.labels helper already produces — use shoggoth.labelsWithMap"; \
		echo "        if a per-template override is needed."; \
		cat ${K3S_TMP_DIR}/shoggoth-lint-dupes.txt; \
		exit 1; \
	fi
	echo "=== per-service gate check (every services.<key> toggle must remove >=1 resource) ==="
# A union gate spanning a service's own workloads makes `make stop SERVICE=<x>`
# a silent no-op: `or (x) (y)` still renders while y is on, so helm reports a
# successful upgrade and nothing is deleted. openbao hit this — its Service,
# PVC and StatefulSet sat under `or(openbao, openbao-bootstrap)`, and it was
# the only one of the 43 keys whose toggle removed nothing. This renders once
# per key with that key alone disabled and fails if the document count does not
# drop, which catches the class rather than just that instance.
	@failed=0; \
	keys=$$(yq -r '.services | keys | .[]' $(SHOGGOTH_CHART_ROOT)/values.yaml); \
	if ! helm template shoggoth-lint $(SHOGGOTH_CHART_ROOT) --namespace lint $(SHOGGOTH_HELM_VALUES_LINT) \
		> ${K3S_TMP_DIR}/gate-all.yaml 2> ${K3S_TMP_DIR}/gate-all.err; then \
		echo "  FAIL: baseline render failed"; \
		cat ${K3S_TMP_DIR}/gate-all.err; \
		exit 1; \
	fi; \
	base=$$(yq ea -r 'select(.kind != null) | .kind' ${K3S_TMP_DIR}/gate-all.yaml | grep -c .); \
	for k in $$keys; do \
		if ! helm template shoggoth-lint $(SHOGGOTH_CHART_ROOT) --namespace lint $(SHOGGOTH_HELM_VALUES_LINT) \
			--set services.$$k.enabled=false \
			> ${K3S_TMP_DIR}/gate-off.yaml 2> ${K3S_TMP_DIR}/gate-off.err; then \
			echo "  FAIL: render failed with services.$$k disabled"; \
			cat ${K3S_TMP_DIR}/gate-off.err; \
			failed=1; \
			continue; \
		fi; \
		off=$$(yq ea -r 'select(.kind != null) | .kind' ${K3S_TMP_DIR}/gate-off.yaml | grep -c .); \
		if [ "$$off" -ge "$$base" ]; then \
			echo "  FAIL: disabling services.$$k removes no resources ($$base -> $$off)"; \
			echo "        'make stop SERVICE=$$k' would report success and change nothing."; \
			echo "        Its objects are probably under a union gate with another key."; \
			failed=1; \
		fi; \
	done; \
	if [ "$$failed" -ne 0 ]; then exit 1; fi; \
	echo "  OK: all $$(printf '%s\n' $$keys | wc -l) service toggles remove >=1 resource"

