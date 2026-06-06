DOCKER_DISTRO?=noble
SERVICE?=


exec:
	${MAKE} ssh_exec CMD='docker compose exec ${SERVICE} sh'

up:
	test -z "${SERVICE}" || ${MAKE} ssh_exec CMD='docker compose up -d ${SERVICE}'
	test -n "${SERVICE}" || ${MAKE} ssh_exec CMD='docker compose up -d && sleep 5'

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

docker_build:
	cd shoggoth \
		&& docker build \
			--build-arg BASE_IMAGE=${BASE_IMAGE} \
			-f dockerfiles/${IMAGE} \
			-t docker-registry.${DOMAIN}/${IMAGE}${DOCKER_TAG_SUFFIX}:latest \
			--progress plain \
			${DOCKER_BUILD_ADD_HOST} \
			./

slave:
	${MAKE} docker_build IMAGE=slave BASE_IMAGE=asherikov/ccws_qwen_${DOCKER_DISTRO}:latest

