JOBS?=4
INSTANCE?=shoggoth
SHOGGOTH_CONFIG?=shoggoth/${INSTANCE}.yaml
SHOGGOTH_CHART_ROOT?=$(CURDIR)/shoggoth/chart

# The `--values` half of the instance config, for callers that spell
# `--namespace` themselves. SHOGGOTH_HELM_COMMON_ARGS (make/chore.mk) is the
# same idea but also carries `--namespace $(INSTANCE)`, which the lint targets
# cannot use: they render into namespace `lint` and must not inherit the
# instance's.
#
# This variable has to be defined, and leaving it undefined is silent. `helm
# template` with no `--values` renders every default in values.yaml, and every
# default there is empty, so the result is a render that validates nothing
# that gets deployed: the three hostNetwork Services render `port:` with
# nothing after it, which is YAML null and is rejected by the schema; the dns
# Service loses its pinned clusterIP; and every hostPath lands under
# /var/lib/rancher/k3s/storage// with an empty instanceDir. That is exactly
# the render `make helm_lint_kube_linter` was checking until this was added.
SHOGGOTH_HELM_VALUES = --values $(SHOGGOTH_CONFIG)

SHOGGOTH_CFG = $(shell yq -r '$(1)' $(SHOGGOTH_CONFIG) 2>/dev/null)
DOMAIN?=$(call SHOGGOTH_CFG,.identity.domain)
WEB_EXT_PORT?=$(call SHOGGOTH_CFG,.network.webExternal.hostPort)
WG_PORT?=$(call SHOGGOTH_CFG,.network.wireguard.udpPort)
DNS_IP?=$(call SHOGGOTH_CFG,.identity.dnsIp)
REGISTRY_PORT?=$(call SHOGGOTH_CFG,.network.registry.hostPort)
WG_UI_PORT?=$(call SHOGGOTH_CFG,.network.wireguard.uiPort)

# placeholder variable, currently HOST_IP is used directly instead.
HOST?=host.${DOMAIN}
HOST_IP?=$(shell getent hosts ${HOST} | cut -f 1 -d ' ')

SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null

# The client-side toolchain image, built from shoggoth/dockerfiles/tools with
# `--target client` — see that file's header for why the host-side tools are a
# second image rather than part of `:host`. It carries kube-linter,
# updatecli, KubeDiagrams with the graphviz `dot` it shells out to,
# ansible-core with the two collections and the `kubernetes` client its
# modules import, kubectl and rsync, so every target that would otherwise
# need one of those on the host needs only docker.
TOOLS_IMAGE_CLIENT?=asherikov/shoggoth_tools:client

# Where the repo is mounted inside the container, and the working directory
# set to the same place. Every path these fragments pass is repo-relative, so
# a fixed root plus -w is what makes them resolve with no rewriting; the one
# exception, the kube-linter targets in make/lint.mk, keep their own
# read-only mount at $(KUBE_LINTER_WORKDIR).
TOOLS_CLIENT_WORKDIR?=/work

# -it when the recipe has a terminal, -i otherwise. The ansible family needs
# stdin: `--ask-become-pass` prompts, and a prompt on /dev/null is an error
# rather than a question. Same detection as the `shell` target in
# make/k3s.mk, in a variable so every call site shares it. Only the ansible
# prefix uses it — kube-linter, updatecli and the clusterviz pipeline either
# do not read stdin or read it from a pipe, where a pty would only mangle it.
TOOLS_CLIENT_TTY = $$([ -t 0 ] && printf -- -it || printf -- -i)

# The invariant part of every `docker run` in this tree that uses the client
# image. The image itself is NOT here: docker stops parsing flags at the
# image name, and three of the four call sites need flags of their own after
# this prefix — a read-only mount, a TMPDIR, an `-i` for the clusterviz pipe.
# So each site spells `$(TOOLS_CLIENT_RUN) <its flags> $(TOOLS_IMAGE_CLIENT)
# <command>`, which also makes visible which image runs what.
#
# No network namespace of its own: the schema check in make/lint.mk fetches
# its JSON schemas over HTTPS. The invoking uid, so nothing the container
# writes lands root-owned in the tree.
TOOLS_CLIENT_RUN = docker run --rm --user "$$(id -u):$$(id -g)"

# The repo itself, read-write: `make update` rewrites pinned versions through
# it and clusterviz writes into .tmp/ and docs/. A read-only variant would
# need a second spelling of the same mount, and the one target that mounted
# it read-only is the kube-linter pair, which keeps its own.
TOOLS_CLIENT_REPO = -v $(CURDIR):$(TOOLS_CLIENT_WORKDIR) -w $(TOOLS_CLIENT_WORKDIR)

# The invoking user's home, mounted at the same path it has on the host. This
# is not a convenience: ansible's ssh transport authenticates with the key
# files under ~/.ssh, and the control node writes its local scratch to
# ~/.ansible/tmp (DEFAULT_LOCAL_TMP), neither of which exists in an image
# running as an arbitrary uid.
#
# The agent socket rides along when there is one — OpenSSH reads it from
# SSH_AUTH_SOCK with no -A, which covers a key that lives in an agent or a
# hardware token rather than in ~/.ssh.
TOOLS_CLIENT_HOME = -v $(HOME):$(HOME) \
    -e HOME=$(HOME) \
    $(if $(SSH_AUTH_SOCK),-v $(SSH_AUTH_SOCK):$(SSH_AUTH_SOCK) \
        -e SSH_AUTH_SOCK=$(SSH_AUTH_SOCK),)

# KUBECONFIG as the container sees it. k3s.mk exports the host path, and the
# plays read it from the environment — `kubectl config current-context` in
# deploy.yml's preflight, and the kubernetes.core modules behind it — so it
# has to be remapped rather than inherited. A path outside the repo is passed
# through unchanged: that tree is not mounted, and a missing file fails the
# task instead of silently reaching a different cluster.
TOOLS_CLIENT_KUBECONFIG = $(patsubst $(CURDIR)/%,$(TOOLS_CLIENT_WORKDIR)/%,$(KUBECONFIG))

# What the container cannot infer about the host it is driving:
#
#   * USER, because the image has no passwd entry for the invoking uid and
#     `id -un` there prints the number. group_vars/instances.yml reads the
#     ssh user from lookup('env','USER'), so without this the ssh user
#     becomes "1000".
#   * HOST_IP, for the same lookup in that file, and because
#     `-e ansible_host` alone would leave a second, empty spelling of it.
#   * KUBECONFIG, per the mapping above.
#   * ANSIBLE_CONFIG, because ansible searches ./ansible.cfg, then
#     ~/.ansible.cfg, then /etc/ansible/ansible.cfg — and this repo keeps its
#     config in ansible/ansible.cfg, which none of those find.
TOOLS_CLIENT_ANSIBLE_ENV = -e USER=$(or $(USER),$(shell id -un)) \
    -e HOST_IP=$(HOST_IP) \
    -e KUBECONFIG=$(TOOLS_CLIENT_KUBECONFIG) \
    -e ANSIBLE_CONFIG=$(TOOLS_CLIENT_WORKDIR)/ansible/ansible.cfg

# Everything a bare ansible-playbook or ansible-inventory needs, for the call
# sites in make/lint.mk that run the binaries with their own flags rather
# than through $(ANSIBLE).
TOOLS_CLIENT_ANSIBLE = docker run $(TOOLS_CLIENT_TTY) $(TOOLS_CLIENT_REPO) \
    $(TOOLS_CLIENT_HOME) $(TOOLS_CLIENT_ANSIBLE_ENV) $(TOOLS_IMAGE_CLIENT)

# Ansible looks for ansible.cfg in $PWD, then ~/.ansible.cfg, then
# /etc/ansible/ansible.cfg. It does not look next to the playbook, so
# `ansible-playbook ansible/deploy.yml`.
#
# The value ends in the `ansible` directory so each call site appends its
# playbook to it. The two flags sit ahead of that: every playbook in the tree
# is `hosts: instances`, and `--limit $(INSTANCE)` together with
# `-e ansible_host=$(HOST_IP)` is the one-instance-on-one-host targeting all
# eight call sites pass. Spelling it here keeps it in one place.
# `--ask-become-pass` deliberately is NOT shared — it prompts every play it
# touches (see ansible/README.md). Recursive (`=`), so `INSTANCE=` and
# `HOST_IP=` on the command line still reach it.
#
# ansible-playbook runs inside the client image: the two collections, the
# `kubernetes` client and ansible-core itself are baked in there, so the
# control node needs nothing but docker. The environment is carried as `-e`
# flags on the `docker run`: a `VAR=x cmd` prefix would set the variable for
# the docker CLI process, not for the process inside the container.
ANSIBLE = $(TOOLS_CLIENT_ANSIBLE) ansible-playbook \
          --limit $(INSTANCE) -e ansible_host=$(HOST_IP) ansible

help:
	grep -hv "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\."

-include make/*.mk
-include private/*.mk


# Tear the instance down and bring it back up from scratch.
restart: tunnel_up
	${MAKE} down
	${MAKE} up

ssh:
	ssh ${SSH_COMMON_ARGS} "${USER}@${HOST_IP}"

sshkey:
	ssh-copy-id ${SSH_COMMON_ARGS} -i "${HOME}/.ssh/id_rsa.pub" "${USER}@${HOST_IP}"

shutdown:
	-${MAKE} out
	${MAKE} down
	${K3S_SSH} 'exec su -l -c "shutdown -P now"'

reboot:
	-${MAKE} out
	${MAKE} down
	${K3S_SSH} 'exec su -l -c "shutdown -r now"'

hosts:
	# DOMAIN/HOST_IP travel through the environment, not --domain/--host-ip
	# flags: setup-client.sh already reads DOMAIN from the environment
	# (line 13), and passing config as CLI args is what this layout avoids.
	DOMAIN="${DOMAIN}" HOST_IP="${HOST_IP}" ./shoggoth/setup-client.sh --update-hosts

ping:
	ping -c 1 "${HOST_IP}"

home:
	firefox http://${DOMAIN}

test:
	echo "Requires VPN connectivity"
	echo "======================================================="
	echo ">>>>>>>>>>>> registry (zot)"
	curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "http://registry.${DOMAIN}:${REGISTRY_PORT}/v2/" | grep -q '^200' && echo "OK" || echo "FAIL"
	echo "======================================================="
	echo ">>>>>>>>>>>> DNS"
	host ${DOMAIN} dns.${DOMAIN}
	echo "======================================================="
	echo ">>>>>>>>>>>> localai"
	${MAKE} ai_models
	echo "======================================================="
	echo ">>>>>>>>>>>> apt-proxy"
	curl http://apt-cache.${DOMAIN}/acng-report.html --output /dev/null


personal_conf:
	./shoggoth/setup-client.sh \
		--client-conf ${HOME}/.config/shoggoth \
		--domain "${DOMAIN}" --host-ip "${HOST_IP}" \
		--ai-token ${AI_TOKEN} \
		--ssh-config
