DOCKER_DISTRO?=noble

COMPOSE_CMD=env \
		UID=`id -u` \
			GID=`id -g` \
			SHOGGOTH_IP=${SHOGGOTH_IP} \
			SHOGGOTH_ROOT=`pwd` \
			SHOGGOTH_DOMAIN='shoggoth.local' \
			docker compose -f shoggoth.yml
SERVICE?=


exec:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} exec ${SERVICE} sh'

up:
	test -z "${SERVICE}" || ${MAKE} ssh_exec CMD='${COMPOSE_CMD} up -d ${SERVICE}'
	test -n "${SERVICE}" || ${MAKE} ssh_exec CMD='${COMPOSE_CMD} up -d'

down:
	test -z "${SERVICE}" || ${MAKE} ssh_exec CMD='${COMPOSE_CMD} down ${SERVICE}'
	test -n "${SERVICE}" || ${MAKE} ssh_exec CMD='${COMPOSE_CMD} down'

restart:
	${MAKE} down
	${MAKE} up

pull:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} pull'

log:
	${MAKE} ssh_exec CMD='${COMPOSE_CMD} logs ${SERVICE} --follow'

docker_build: client_conf
	cd shoggoth \
		&& docker build \
			--build-arg BASE_IMAGE=${BASE_IMAGE} \
			-f dockerfiles/${IMAGE} \
			-t docker-registry.${SHOGGOTH_DOMAIN}/${IMAGE}_${DOCKER_DISTRO}:latest \
			--progress plain \
			--add-host apt-cache.${SHOGGOTH_DOMAIN}:${SHOGGOTH_IP} \
			./

slave:
	${MAKE} docker_build IMAGE=slave BASE_IMAGE=asherikov/ccws_qwen_${DOCKER_DISTRO}:latest

webhookd:
	${MAKE} docker_build IMAGE=webhookd BASE_IMAGE=ubuntu:${DOCKER_DISTRO}

