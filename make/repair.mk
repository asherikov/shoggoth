# Wipe OpenBao raft storage. Does NOT restart anything — run `make restart`
# after. Useful when the unseal key is lost or OpenBao data is corrupted.
# The kubectl calls read the cluster through the host-side forward, and the
# wipe itself is an ssh to the host, so `tunnel_up` — which provisions the
# agent before the forward — is the prerequisite: a shell with no agent of
# its own gets make's, and one with nothing loadable fails here rather than
# on the first container.
wipe_openbao: tunnel_up
	-$(TOOLS_CLIENT_KUBECTL) scale statefulset openbao -n ${INSTANCE} --replicas=0 2>/dev/null || true
	-$(TOOLS_CLIENT_KUBECTL) wait --for=delete pod/openbao-0 -n ${INSTANCE} --timeout=60s 2>/dev/null || true
	PV_JSON=$$($(TOOLS_CLIENT_KUBECTL) get pv -o json | $(TOOLS_CLIENT_JQ) -r '.items[] | select(.spec.claimRef.name=="openbao-data" and .spec.claimRef.namespace=="${INSTANCE}")'); \
	PV_HOST_PATH=$$(printf '%s' "$${PV_JSON}" | $(TOOLS_CLIENT_JQ) -r '.spec.local.path // empty'); \
	PV_NAME=$$(printf '%s' "$${PV_JSON}" | $(TOOLS_CLIENT_JQ) -r '.metadata.name // empty'); \
	if [ -n "$${PV_HOST_PATH}" ]; then \
		${K3S_SSH} "sudo sh -c 'find \"$${PV_HOST_PATH}\" -mindepth 1 -exec rm -rf {} +'"; \
		WIPE_EXIT=$$?; \
		if [ $${WIPE_EXIT} -ne 0 ]; then \
			echo "ERROR: Host data wipe failed (exit code $${WIPE_EXIT}). Check sudo access."; \
			exit 1; \
		fi; \
	fi; \
	if [ -n "$${PV_NAME}" ]; then \
		$(TOOLS_CLIENT_KUBECTL) patch pv "$${PV_NAME}" -p '{"metadata":{"finalizers":null}}' 2>/dev/null || true; \
		-$(TOOLS_CLIENT_KUBECTL) delete pvc openbao-data -n ${INSTANCE} --ignore-not-found=true --wait=false 2>/dev/null; \
		-$(TOOLS_CLIENT_KUBECTL) delete pv "$${PV_NAME}" --ignore-not-found=true --wait=false 2>/dev/null; \
		for i in 1 2 3 4 5 6 7 8 9 10; do \
			$(TOOLS_CLIENT_KUBECTL) get pvc openbao-data -n ${INSTANCE} >/dev/null 2>&1 || break; \
			sleep 1; \
		done; \
	fi;
	-$(TOOLS_CLIENT_KUBECTL) delete secret openbao-unseal-key -n ${INSTANCE} --ignore-not-found=true


# Clear the `kubernetes` service's endpoint records for good — by removing the
# master leases that keep putting them back.
K3S_STALE_IPS?=
.PHONY: k3s_clear_endpoints
k3s_clear_endpoints: ssh_agent_setup
	# The agent, not the tunnel: every task in the play is undelegated ssh to
	# the host — it stops k3s, edits the kine datastore and restarts k3s
	# again, and the cluster itself is not contacted — so what the
	# prerequisite has to provide is the agent, which `ssh_agent_setup`
	# guarantees. The tunnel's readyz gate would block the exact
	# broken-apiserver state this play exists to repair.
	# By hand, with the forward up:
	# KUBECONFIG=./private/kubeconfig kubectl -n default get endpoints kubernetes
	$(ANSIBLE)/clear_endpoints.yml -e 'shoggoth_k3s_stale_ips=$(K3S_STALE_IPS)' --ask-become-pass
