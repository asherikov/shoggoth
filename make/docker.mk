DOCKER_DISTRO?=noble


docker_build:
	cd shoggoth \
		&& docker build \
			--build-arg BASE_IMAGE=${BASE_IMAGE} \
			-f dockerfiles/${IMAGE} \
			-t registry.${DOMAIN}/${IMAGE}:${DOCKER_DISTRO} \
			--progress plain \
			${DOCKER_BUILD_ADD_HOST} \
			./
			#--build-arg SHOGGOTH_DOMAIN=${DOMAIN} \

slave:
	${MAKE} docker_build IMAGE=slave BASE_IMAGE=asherikov/ccws:${DOCKER_DISTRO}

# The two tools images, both stages of the one multi-stage Dockerfile. CI
# builds and pushes them (.github/workflows/docker-tools.yml); these are the
# local build of the same thing, for testing a Dockerfile change before it is
# pushed. No --build-arg: the ARGs in that file carry their defaults and CI
# passes none, so the local build has to be the build that gets pushed.
#
# Two targets rather than one command because the stages are independent —
# `client` is FROM the alpine base, not FROM `tools` — and because the common
# case is rebuilding the half that changed. Same meta-target shape as
# `clusterviz` in make/chore.mk.
#
# The context is the repo root, not shoggoth/: the client stage copies
# ansible/requirements.yml, which sits at the root.
TOOLS_IMAGE_HOST?=asherikov/shoggoth_tools:host

tools_images: tools_host tools_client

tools_host:
	docker build \
		--target tools \
		-f shoggoth/dockerfiles/tools \
		-t $(TOOLS_IMAGE_HOST) \
		--progress plain \
		.

tools_client:
	docker build \
		--target client \
		-f shoggoth/dockerfiles/tools \
		-t $(TOOLS_IMAGE_CLIENT) \
		--progress plain \
		.

