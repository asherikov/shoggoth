DOCKER_DISTRO?=noble
SERVICE?=


exec:
	${MAKE} ssh_exec CMD='docker compose exec ${SERVICE} sh'

up:
	test -z "${SERVICE}" || ${MAKE} ssh_exec CMD='./setup-env.sh ${SHOGGOTH_DOMAIN} ${SHOGGOTH_IP} && docker compose up -d ${SERVICE}'
	test -n "${SERVICE}" || ${MAKE} ssh_exec CMD='./setup-env.sh ${SHOGGOTH_DOMAIN} ${SHOGGOTH_IP} && docker compose up -d && sleep 5'

down:
	test -z "${SERVICE}" || ${MAKE} ssh_exec CMD='docker compose down ${SERVICE}'
	test -n "${SERVICE}" || ${MAKE} ssh_exec CMD='docker compose down'

restart:
	${MAKE} down
	${MAKE} up

pull:
	${MAKE} ssh_exec CMD='docker compose pull'

log:
	${MAKE} ssh_exec CMD='docker compose logs ${SERVICE} --follow'

DOCKER_TAG_SUFFIX?=_${DOCKER_DISTRO}

docker_build: client_conf
	cd shoggoth \
		&& docker build \
			--build-arg BASE_IMAGE=${BASE_IMAGE} \
			-f dockerfiles/${IMAGE} \
			-t docker-registry.${SHOGGOTH_DOMAIN}/${IMAGE}${DOCKER_TAG_SUFFIX}:latest \
			--progress plain \
			--add-host apt-cache.${SHOGGOTH_DOMAIN}:${SHOGGOTH_IP} \
			--add-host proxpi.${SHOGGOTH_DOMAIN}:${SHOGGOTH_IP} \
			${DOCKER_BUILD_ADD_HOST} \
			./

slave:
	${MAKE} docker_build IMAGE=slave BASE_IMAGE=asherikov/ccws_qwen_${DOCKER_DISTRO}:latest

mcp_skills:
	${MAKE} docker_build IMAGE=mcp_skills DOCKER_TAG_SUFFIX=

