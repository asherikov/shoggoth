JOBS?=4
DOMAIN?=$(shell grep SHOGGOTH_DOMAIN shoggoth/.env | cut -f 2 -d '=')
GITHUB_ORG?=$(shell grep SHOGGOTH_GITHUB_ORG shoggoth/.env | cut -f 2 -d '=')
HOST?=host.${DOMAIN}
HOST_IP?=$(shell getent hosts ${HOST} | cut -f 1 -d ' ')
GITEA_TOKEN?=$(shell cat shoggoth/private/gitea-server-token.txt)
API_HOST=api.${DOMAIN}
REMOTE_PATH?=~/

SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null


help:
	@grep -v "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\." | grep -v ".mk:$$"

-include make/*.mk
-include private/*.mk

sync:
	rsync -e "ssh ${SSH_COMMON_ARGS}" -r shoggoth ${USER}@${HOST_IP}:${REMOTE_PATH} || true
	@cmds="mkdir -p /var/lib/rancher/k3s/storage/shoggoth/coredns-blacklists"; \
	cmds="$$cmds && cp ./dns/hosts-blacklist/hosts /var/lib/rancher/k3s/storage/shoggoth/coredns-blacklists/blocklist.hosts"; \
	for entry in ${K3S_HOST_PATHS}; do \
		DST=$$(echo "$$entry" | cut -d: -f1); \
		SRC=$$(echo "$$entry" | cut -d: -f2); \
		cmds="$$cmds && rsync -a --delete ./$$SRC/ /var/lib/rancher/k3s/storage/shoggoth/$$DST/"; \
	done; \
	${K3S_SSH} "sh -c \"cd ${REMOTE_PATH}/shoggoth && sudo bash -c '$$cmds'\""

sync_restart: tunnel_up
	${MAKE} down
	${MAKE} sync
	${MAKE} up

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
	ssh ${SSH_COMMON_ARGS} -t ${USER}@${HOST_IP} 'cd ${REMOTE_PATH}/shoggoth && ${CMD}'

shutdown:
	-${MAKE} out
	${MAKE} down
	${MAKE} ssh_exec CMD='exec su -l -c "shutdown -P now"'

hosts:
	./shoggoth/setup-client.sh --update-hosts --domain "${DOMAIN}" --host-ip "${HOST_IP}"

ping:
	ping "${HOST_IP}"

home:
	firefox http://${DOMAIN}

test:
	@echo "Requires VPN connectivity"
	@echo "======================================================="
	@echo ">>>>>>>>>>>> docker cache"
	curl -s --connect-timeout 5 "docker-cache.${DOMAIN}:3128/ca.crt" --output /dev/null
	@echo "======================================================="
	@echo ">>>>>>>>>>>> docker registry"
	@curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "http://docker-registry.${DOMAIN}/v2/" | grep -q '^200' && echo "OK" || echo "FAIL"
	@curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "http://docker-registry.${DOMAIN}/v2/_catalog" | grep -q '^200' && echo "OK" || echo "FAIL"
	@echo "======================================================="
	@echo ">>>>>>>>>>>> DNS"
	host ${DOMAIN} dns.${DOMAIN}
	@echo "======================================================="
	@echo ">>>>>>>>>>>> api gateway /gitea redirect"
	curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://${API_HOST}/gitea | grep -q '^301 http://${API_HOST}/gitea/' && echo "OK" || echo "FAIL"
	@echo "======================================================="
	@echo ">>>>>>>>>>>> api gateway /gitea/"
	curl -s -o /dev/null -w '%{http_code}' http://${API_HOST}/gitea/ | grep -q '^200' && echo "OK" || echo "FAIL"
	@echo "======================================================="
	@echo ">>>>>>>>>>>> api gateway /gitea/api/v1/version"
	curl -s -o /dev/null -w '%{http_code}' http://${API_HOST}/gitea/api/v1/version | grep -q '^200' && echo "OK" || echo "FAIL"
	@echo "======================================================="
	@echo ">>>>>>>>>>>> api gateway /redmine redirect"
	curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://${API_HOST}/redmine | grep -q '^301 http://${API_HOST}/redmine/' && echo "OK" || echo "FAIL"
	@echo "======================================================="
	@echo ">>>>>>>>>>>> api gateway /redmine/"
	curl -s -o /dev/null -w '%{http_code}' http://${API_HOST}/redmine/ | grep -q '^200' && echo "OK" || echo "FAIL"
	@echo "======================================================="
	@echo ">>>>>>>>>>>> localai"
	${MAKE} ai_embed
	${MAKE} ai_models
	@echo ""
	@echo "======================================================="
	@echo ">>>>>>>>>>>> apt-proxy"
	curl http://apt-cache.${DOMAIN}/acng-report.html --output /dev/null


personal_conf:
	./shoggoth/setup-client.sh \
		--client-conf ${HOME}/.config/shoggoth \
		--domain "${DOMAIN}" --host-ip "${HOST_IP}" \
		--gitea-user $$(cat shoggoth/private/gitea-user.txt) \
		--ai-token ${AI_TOKEN} \
		--ssh-config
