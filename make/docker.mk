DOCKER_DISTRO?=noble
SERVICE?=


exec:
	${MAKE} ssh_exec CMD='docker compose exec ${SERVICE} sh'

up:
	test -z "${SERVICE}" || ${MAKE} ssh_exec CMD='./setup-env.sh ${SHOGGOTH_DOMAIN} ${SHOGGOTH_IP} && docker compose up -d ${SERVICE}'
	test -n "${SERVICE}" || ${MAKE} ssh_exec CMD='./setup-env.sh ${SHOGGOTH_DOMAIN} ${SHOGGOTH_IP} && docker compose up -d'

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

