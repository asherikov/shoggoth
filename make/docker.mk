DOCKER_DISTRO?=noble
DOCKER_TAG_SUFFIX?=_${DOCKER_DISTRO}


docker_build:
	cd shoggoth \
		&& docker build \
			--build-arg BASE_IMAGE=${BASE_IMAGE} \
			-f dockerfiles/${IMAGE} \
			-t registry.${DOMAIN}/${IMAGE}${DOCKER_TAG_SUFFIX}:latest \
			--progress plain \
			${DOCKER_BUILD_ADD_HOST} \
			./
			#--build-arg SHOGGOTH_DOMAIN=${DOMAIN} \

slave:
	${MAKE} docker_build IMAGE=slave BASE_IMAGE=asherikov/ccws_qwen:${DOCKER_DISTRO}

