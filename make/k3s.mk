# The kubeconfig path, and not a value of its own: it is part of the image's
# contract rather than a knob. The client stage of shoggoth/dockerfiles/tools
# bakes ENV KUBECONFIG at /work/private/kubeconfig and no call site passes it
# through, so every container reads that path whatever the environment says —
# an override here would move the write without moving the read, and an
# operator whose own KUBECONFIG is exported would have it overwritten by the
# cluster-admin credential.
export KUBECONFIG=$(CURDIR)/private/kubeconfig
K3S_TMP_DIR?=.tmp
K3S_TUNNEL_PID?=${K3S_TMP_DIR}/k3s-tunnel.pid

K3S_SSH=ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP}

# The apiserver port on the HOST, and the port the forward binds on the
# host's own loopback. Two variables because the kubeconfig names the
# second: its `server:` is `https://127.0.0.1:<tunnel port>`, so the
# forward has to bind the port the kubeconfig points at, and a kubeconfig
# served on another port needs this one changed with it. The client
# containers share the host's network namespace ($(TOOLS_CLIENT_RUN)'s
# `--network host`), so the loopback the forward binds is the one the
# kubeconfig names for them as well — the kubeconfig is the host's
# k3s.yaml verbatim and nothing rewrites it.
K3S_API_PORT?=6443
K3S_TUNNEL_PORT?=6443


# The host OS installation. The NixOS module stays the host-OS source of
# truth; ansible/host.yml renders it with the instance's values, copies it
# into /etc/nixos, refuses to rebuild until configuration.nix imports it,
# rebuilds, and warms the registry image the module's own mirror cannot
# pull through itself.
#
# No `tunnel_up`: the play contacts no cluster — it renders on the control
# node, copies and rebuilds on the host, and needs no kubeconfig. What it
# does need is the agent: the ssh to the host runs inside the client image,
# which holds no key of its own, so `ssh_agent_setup` is the prerequisite.
host_install_nixos: ssh_agent_setup
	$(ANSIBLE)/host.yml --ask-become-pass


# Fetch the kubeconfig: one ssh, one sudo cat, one redirect. Runs on the HOST
# with the agent socket ${SSH_COMMON_ARGS}' IdentityAgent forwards — it is
# the bootstrap step every other target reads $(KUBECONFIG) from, and it needs
# no kubectl, no helm and no ansible to do it, only the agent ssh_agent_setup
# provisions.
#
# No pty on this ssh, unlike ${K3S_SSH}: a forced pty merges sudo's password
# prompt and its stderr into ssh's stdout, which is the redirected kubeconfig,
# while ssh still exits 0 and the rename installs the polluted credential. The
# password is read here instead — echo off for the read, as a prompt needs —
# and handed to `sudo -S`, whose own prompt is suppressed with `-p ""`, so the
# kubeconfig is the only thing on stdout. The same no-tty shape
# --ask-become-pass used when this was a play. A run with no terminal to read
# from hands sudo an empty password and fails at the sudo, leaving a
# kubeconfig already there untouched.
#
# What the one-liner owes the cluster-admin credential it writes:
#   * install -d -m 700 — git tracks no directory modes, so a fresh clone's
#     private/ is umask-dependent.
#   * umask 077 — the redirect creates the file 0600, never umask-default.
#   * .new + mv — a failed ssh must not truncate the kubeconfig already
#     there; the rename is atomic and same-filesystem.
control_setup: ssh_agent_setup
	install -d -m 700 $(dir $(KUBECONFIG))
	umask 077 && { stty -echo 2>/dev/null || true; read -r pw; stty echo 2>/dev/null || true; echo >&2; printf '%s\n' "$$pw"; } \
		| ssh ${SSH_COMMON_ARGS} ${USER}@${HOST_IP} 'sudo -S -p "" cat /etc/rancher/k3s/k3s.yaml' \
		> $(KUBECONFIG).new && mv $(KUBECONFIG).new $(KUBECONFIG)


# connection & health
# -----

K3S_TUNNEL_LOG?=${K3S_TMP_DIR}/k3s-tunnel.log

# The SSH tunnel to the k3s API, opened on the OPERATOR HOST in front of
# the client containers. `ssh -L` binds the forward on the host's own
# loopback, and the client containers share that namespace
# ($(TOOLS_CLIENT_RUN)'s `--network host`), so the kubeconfig's
# `server: https://127.0.0.1:6443` — fetched verbatim from the host by
# `control_setup` — answers inside every container with no rewrite and no
# second port. One forward serves the whole make run and every container
# in it, and the ssh that carries it authenticates through the same
# forwarded agent every other ssh in the tree uses.
#
# A host-side forward is state on the host, and this target is that
# state's whole management: a pidfile under $(K3S_TMP_DIR), a readyz probe
# that adopts a live forward and replaces a stale one, and a gate that
# proves the API answers before any target that needs it runs — which is
# why every live target below names this as its prerequisite.
#
# `ssh_agent_setup` is the first half: the forward's ssh authenticates
# through the agent it guarantees. `ping` is the second, so an unreachable
# host fails here with ping's own error rather than at the readyz poll.
tunnel_up: ssh_agent_setup ping
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

# Stop the forward and drop its state. Nothing in the tree calls it: a
# running forward is adopted by the next `tunnel_up` rather than torn
# down, and closing it is the operator's call — it is a background ssh on
# their host.
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

# The kubectl operator family. The containers read the cluster through the
# host-side forward, and the pod-log fallback below is an ssh to the host,
# so `tunnel_up` — which provisions the agent before the forward — is the
# prerequisite: a shell with no agent of its own gets make's, and one with
# nothing loadable fails here, before the first container starts.
log: tunnel_up
	test -n "$(SERVICE)" || (echo "Usage: make log SERVICE=<app-name> [CONTAINER=<name>]" && false)
	$(TOOLS_CLIENT_KUBECTL) -n ${INSTANCE} describe pod -l app=$(SERVICE)
	$(TOOLS_CLIENT_KUBECTL) -n ${INSTANCE} get pod -l app=$(SERVICE) -o wide 2>&1 || true
	$(TOOLS_CLIENT_KUBECTL) -n ${INSTANCE} get pod -l app=$(SERVICE) -o json 2>/dev/null | $(TOOLS_CLIENT_JQ) -r '.items[] | .metadata.name as $$pod | ("Pod: " + $$pod), (.status.initContainerStatuses // [] | .[] | "  INIT [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)"), (.status.containerStatuses // [] | .[] | "  MAIN  [\(.name)] state=\(.state | keys[0]) reason=\(.state.terminated.reason // .state.waiting.reason // "n/a") exitCode=\(.state.terminated.exitCode // "n/a") restartCount=\(.restartCount)")' 2>&1 || true
	$(TOOLS_CLIENT_KUBECTL) -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -100 | grep "${SERVICE}" || true
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
		POD_VAL=$$($(TOOLS_CLIENT_KUBECTL) get pod -n ${INSTANCE} -l app=$${APP_VAL} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
		if [ -z "$${POD_VAL}" ]; then \
			echo "FAIL: no pod found for label app=$${APP_VAL} in namespace ${INSTANCE}"; \
			echo "      Available pods:"; \
			$(TOOLS_CLIENT_KUBECTL) get pod -n ${INSTANCE} --no-headers -o custom-columns=NAME:.metadata.name,APP:.metadata.labels.app 2>/dev/null | sed 's/^/        /'; \
			exit 1; \
		fi; \
		echo "=== Resolved app=$${APP_VAL} -> pod $${POD_VAL} (namespace: ${INSTANCE}) ==="; \
	fi; \
	TTY_FLAGS="-i"; \
	if [ -t 0 ]; then TTY_FLAGS="-it"; fi; \
	exec $(TOOLS_CLIENT_KUBECTL_TTY) exec -n ${INSTANCE} $${TTY_FLAGS} $${POD_VAL} -- $${CMD_VAL}

status: tunnel_up
	$(TOOLS_CLIENT_KUBECTL) -n ${INSTANCE} get events --sort-by=.lastTimestamp 2>&1 | tail -20 || true
	CA="$$($(TOOLS_CLIENT_KUBECTL) get validatingwebhookconfiguration externalsecret-validate -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null)"; \
	if [ -n "$$CA" ]; then printf "caBundle injected:    yes\n"; else printf "caBundle injected:    no\n"; fi
	CERT=$$($(TOOLS_CLIENT_KUBECTL) -n ${INSTANCE} get secret external-secrets-webhook -o name 2>/dev/null | wc -l); \
	if [ "$$CERT" -gt 0 ]; then printf "cert Secret present:  yes\n"; else printf "cert Secret present:  no\n"; fi
	printf "SecretStores:         %s\n" "$$($(TOOLS_CLIENT_KUBECTL) get secretstore -n ${INSTANCE} --no-headers 2>/dev/null | wc -l)"
	printf "ExternalSecrets:      %s\n" "$$($(TOOLS_CLIENT_KUBECTL) get externalsecret -n ${INSTANCE} --no-headers 2>/dev/null | wc -l)"
	echo "=== CoreDNS ==="
	$(TOOLS_CLIENT_KUBECTL) get pods -n kube-system -l k8s-app=kube-dns
	echo "=== K3s shoggoth services (namespace: ${INSTANCE}) ==="
	$(TOOLS_CLIENT_KUBECTL) get pods,svc,deployment,statefulset,job,pvc -n ${INSTANCE} -o wide
	echo "=== Pod readiness ==="
	$(TOOLS_CLIENT_KUBECTL) get pods -n ${INSTANCE} \
	    -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,STATUS:.status.phase \
        2>&1 || true

# Drop a tag from the local registry cache.
#
# Triggers on-demand re-sync from upstream on the next pull of IMAGE:TAG.
# zot is configured with anonymous access, so no credentials are required.
# Requires network reachability to registry.${DOMAIN}.
# Example: make drop_image_tag IMAGE=library/nginx TAG=latest
#
# No prerequisite: every task in the play is delegated to the control node, so
# it opens no ssh to the host and reads no apiserver — neither half of
# `tunnel_up` is load-bearing here.
drop_image_tag:
	$(ANSIBLE)/drop_image_tag.yml -e shoggoth_image=$(IMAGE) -e shoggoth_tag=$(TAG)
