# Wipe OpenBao raft storage. Does NOT restart anything — run `make restart`
# after. Useful when the unseal key is lost or OpenBao data is corrupted.
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


# Clear the `kubernetes` service's endpoint records for good — by removing the
# master leases that keep putting them back.
K3S_STALE_IPS?=
.PHONY: k3s_clear_endpoints
k3s_clear_endpoints:
	# KUBECONFIG=./private/kubeconfig kubectl -n default get endpoints kubernetes
	$(ANSIBLE)/clear_endpoints.yml -e 'shoggoth_k3s_stale_ips=$(K3S_STALE_IPS)' --ask-become-pass
