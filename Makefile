JOBS?=4
USER?=aleks
SHOGGOTH_DOMAIN?=s.local
SHOGGOTH_HOST?=${SHOGGOTH_DOMAIN}
SHOGGOTH_IP?=$(shell getent hosts ${SHOGGOTH_DOMAIN} | cut -f 1 -d ' ')
GITEA_TOKEN?=$(shell cat shoggoth/private/gitea-server-token.txt)
REMOTE_PATH?=~/


SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null


help:
	@grep -v "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\." | grep -v ".mk:$$"

-include make/*.mk
-include shoggoth/private/*.mk

sync:
	rsync -r shoggoth ${USER}@${SHOGGOTH_HOST}:${REMOTE_PATH} || true

sync_restart:
	-${MAKE} down
	${MAKE} sync
	${MAKE} up

mount:
	mkdir -p mountpoint
	sshfs ${USER}@${SHOGGOTH_HOST}:${REMOTE_PATH} ./mountpoint

umount:
	fusermount3 -u mountpoint

ssh:
	ssh ${SSH_COMMON_ARGS} "${USER}@${SHOGGOTH_HOST}"

sshkey:
	ssh-copy-id ${SSH_COMMON_ARGS} -i "${HOME}/.ssh/id_rsa.pub" "${USER}@${SHOGGOTH_HOST}"

ssh_exec:
	ssh ${SSH_COMMON_ARGS} -t ${USER}@${SHOGGOTH_HOST} 'cd ${REMOTE_PATH}/shoggoth && ${CMD}'


shutdown: down
	${MAKE} ssh_exec CMD='exec su -l -c "shutdown -P now"'

hosts:
	./shoggoth/setup-client.sh --update-hosts --host "${SHOGGOTH_DOMAIN}" --host-ip "${SHOGGOTH_IP}"

ping:
	ping "${SHOGGOTH_DOMAIN}"

home:
	firefox http://${SHOGGOTH_DOMAIN}

test:
	@echo "======================================================="
	@echo ">>>>>>>>>>>> docker cache"
	curl -s --connect-timeout 5 "docker-cache.${SHOGGOTH_DOMAIN}/ca.crt" --output /dev/null
	@echo "======================================================="
	@echo ">>>>>>>>>>>> DNS"
	host ${SHOGGOTH_DOMAIN} dns.${SHOGGOTH_DOMAIN}
	@echo "======================================================="
	@echo ">>>>>>>>>>>> ollama"
	${MAKE} ollama_tags
	@echo ""
	@echo "======================================================="
	@echo ">>>>>>>>>>>> apt-proxy"
	curl http://apt-cache.${SHOGGOTH_DOMAIN}/acng-report.html --output /dev/null

personal_conf:
	./shoggoth/setup-client.sh \
		--client-conf ${HOME}/.config/shoggoth \
		--host "${SHOGGOTH_DOMAIN}" --host-ip "${SHOGGOTH_IP}" \
		--gitea-token ${GITEA_TOKEN} \
		--gitea-user $$(cat shoggoth/private/gitea-user.txt) \
		--ai-token ${AI_TOKEN} \
		--redmine-token $$(cat shoggoth/private/redmine-token.txt) \
		--ssh-config
