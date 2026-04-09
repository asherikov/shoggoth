JOBS?=4
USER?=aleks
SHOGGOTH_DOMAIN?=shoggoth.local
SHOGGOTH_HOST?=${SHOGGOTH_DOMAIN}
SHOGGOTH_IP?=$(shell getent hosts ${SHOGGOTH_DOMAIN} | cut -f 1 -d ' ')
REMOTE_PATH?=~/

SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null


help:
	@grep -v "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\." | grep -v ".mk:$$"

-include make/*.mk
-include private/*.mk

sync: client_conf
	rsync -r shoggoth ${USER}@${SHOGGOTH_HOST}:${REMOTE_PATH} || true

sync_restart:
	${MAKE} down
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


shutdown:
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
	curl http://ollama.${SHOGGOTH_DOMAIN}/api/tags
	@echo ""
	@echo "======================================================="
	@echo ">>>>>>>>>>>> apt-proxy"
	curl http://apt-cache.${SHOGGOTH_DOMAIN}/acng-report.html --output /dev/null

ollama_query:
	time curl http://ollama.${SHOGGOTH_DOMAIN}/v1/completions \
		-H "Content-Type: application/json" \
		-H "Authorization: ollama" \
		-d '{"model": "qwen3-coder:30b", "prompt": "What is the capital of UAE?"}'

client_conf:
	./shoggoth/setup-client.sh \
		--client-conf shoggoth/client \
		--host "${SHOGGOTH_DOMAIN}" --host-ip "${SHOGGOTH_IP}"

personal_conf:
	./shoggoth/setup-client.sh \
		--client-conf \
		--host "${SHOGGOTH_DOMAIN}" --host-ip "${SHOGGOTH_IP}" \
		--gitea-token ${GITEA_TOKEN} --redmine-token ${REDMINE_TOKEN}
