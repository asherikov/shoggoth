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

export GITEA_API?=https://tvoygit.ru/api/v1/repos/migrate
#export GITEA_TOKEN?=<token> # use auth.mk
# issues not copied if true, see https://github.com/go-gitea/gitea/pull/20311 and https://forum.gitea.com/t/mirror-a-github-site-does-not-mirror-issues/8141
export GITEA_MIRROR?=true

export GITHUB_USER?=asherikov
export GITHUB_REPO?=ccws
#export GITHUB_TOKEN?=<token> # use auth.mk

export REPO_DESCRIPTION?=


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

apt_stats:
	firefox http://apt-cache.${SHOGGOTH_NAME}/acng-report.html

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

gitea_runner_token:
	@openssl rand -hex 24 > shoggoth/gitea-runner/runner-token.txt

github_to_gitea_repo:
	echo "Copying ${GITHUB_USER}/${GITHUB_REPO}"
	curl \
		"${GITEA_API}" \
		-H "accept: application/json" \
		-H "Authorization: token ${GITEA_TOKEN}" \
		-H "Content-Type: application/json" \
		-d "{ \
			\"auth_token\": \"${GITHUB_TOKEN}\", \
			\"clone_addr\": \"https://github.com/${GITHUB_USER}/${GITHUB_REPO}\", \
			\"description\": \"${REPO_DESCRIPTION}\", \
			\"issues\": true, \
			\"labels\": true, \
			\"milestones\": true, \
			\"mirror\": ${GITEA_MIRROR}, \
			\"private\": false, \
			\"pull_requests\": true, \
			\"releases\": true, \
			\"repo_name\": \"${GITHUB_REPO}\", \
			\"service\": \"git\", \
			\"wiki\": true \
			}" \
		-i

github_to_gitea_user:
	curl "https://api.github.com/users/${GITHUB_USER}/repos" \
		| jq -r '.[] | "${MAKE} github_to_gitea_repo GITHUB_REPO=\"\(.name )\" REPO_DESCRIPTION=\"\(.description)\""' | sed 's/null//' \
		| sh
#docker exec apt-cacher-ng /usr/sbin/apt-cacher-ng -e
