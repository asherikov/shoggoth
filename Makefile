USER?=aleks
SHOGGOTH_NAME?=shoggoth.local
SHOGGOTH_HOST?=${SHOGGOTH_NAME}
SHOGGOTH_IP?=$(shell getent hosts ${SHOGGOTH_NAME} | cut -f 1 -d ' ')
REMOTE_PATH?=~/
SERVICE?=docker-cache

COMPOSE_CMD=env \
			UID=`id -u` \
			GID=`id -g` \
			SHOGGOTH_IP=${SHOGGOTH_IP} \
			SHOGGOTH_ROOT=`pwd` \
			docker compose -f shoggoth.yml
SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null


help:
	@grep -v "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\." | grep -v ".mk:$$"

-include make/*.mk

sync:
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

up_srv:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} up -d ${SERVICE}'

down_srv:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} down ${SERVICE}'

exec:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} exec ${SERVICE} sh'

up:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} up -d'

down:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} down'

pull:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} pull'

log:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} logs ${SERVICE} --follow'

shutdown:
	${MAKE} ssh_exec CMD='exec su -l -c "shutdown -P now"'

update_hosts:
	./shoggoth/setup-client.sh --update-hosts --host "${SHOGGOTH_NAME}" --host-ip "${SHOGGOTH_IP}"

ping:
	ping "${SHOGGOTH_NAME}"

home:
	firefox http://${SHOGGOTH_NAME}

test:
	@echo "======================================================="
	@echo ">>>>>>>>>>>> docker cache"
	curl -s --connect-timeout 5 "docker-cache.${SHOGGOTH_NAME}/ca.crt" --output /dev/null
	@echo "======================================================="
	@echo ">>>>>>>>>>>> DNS"
	host ${SHOGGOTH_NAME} ${SHOGGOTH_NAME}
	@echo "======================================================="
	@echo ">>>>>>>>>>>> ollama"
	curl http://ollama.${SHOGGOTH_NAME}/api/tags
	@echo ""
	@echo "======================================================="
	@echo ">>>>>>>>>>>> apt-proxy"
	curl http://apt-cache.${SHOGGOTH_NAME}/acng-report.html --output /dev/null

ollama_query:
	time curl http://ollama.${SHOGGOTH_NAME}/v1/completions \
		-H "Content-Type: application/json" \
		-H "Authorization: ollama" \
		-d '{"model": "qwen3-coder:30b", "prompt": "What is the capital of UAE?"}'

