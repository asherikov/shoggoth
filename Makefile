JOBS?=4
INSTANCE?=shoggoth
SHOGGOTH_CONFIG?=shoggoth/${INSTANCE}.yaml
# Absolute, because the make-side prerequisites (CHART_SOURCES in
# make/lint.mk) name it. The container-side spellings remap it to
# $(SHOGGOTH_CHART_ROOT_CLIENT), defined further down with the other
# container mappings.
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
# the render `make helm_lint_kube_linter` checks, so leaving the variable
# undefined would have the offline gate certify a cluster that is never
# deployed.
SHOGGOTH_HELM_VALUES = --values $(SHOGGOTH_CONFIG)

# DOMAIN is
# derived from the instance config, further down — after $(TOOLS_CLIENT_RUN)
# is defined, because the derivation runs yq inside the client image. The
# derivation is explained where it happens.

# placeholder variable, currently HOST_IP is used directly instead.
HOST?=host.${DOMAIN}
HOST_IP?=$(shell getent hosts ${HOST} | cut -f 1 -d ' ')

# The agent socket every ssh in this tree authenticates through — the
# host-side forward `make tunnel_up` opens, the plays' own ssh and rsync,
# and the host-side `make ssh`/`shutdown` alike.
#
# A live agent in the invoking environment wins: it is the operator's
# own, with their keys and their hardware tokens in it, and nothing here
# starts a second one beside it or adds anything to it. The probe is
# `ssh-add -l`'s exit status, where 2 means nothing answered —
# SSH_AUTH_SOCK unset, or pointing at a socket whose agent has exited,
# the usual leftover — and 0 or 1 mean an agent answered (1: alive but
# holding no identity, which is not this variable's problem; see
# ssh_agent_setup). Only when nothing answers does the socket fall to the
# one ssh_agent_setup manages, so a stale SSH_AUTH_SOCK routes to make's
# own agent instead of mounting a dead one into the containers.
#
# `IdentityAgent` in $(SSH_COMMON_ARGS) is how that choice reaches the
# host-side ssh calls, which run in recipes of their own and cannot
# inherit it any other way. ssh ignores an IdentityAgent path that does
# not answer — verified: `ssh -o IdentityAgent=/nonexistent -i <key>`
# still authenticates with the key file — so a target that ran no setup
# (`make ssh` before anything else) keeps exactly the behaviour it had
# without the option. The path is absolute because docker resolves a
# relative `-v` source against the daemon's working directory, not the
# client's.
TOOLS_CLIENT_AGENT_PROBE := $(shell ssh-add -l >/dev/null 2>&1; [ $$? -ne 2 ] && printf %s "$$SSH_AUTH_SOCK")
TOOLS_CLIENT_AGENT_SOCK = $(abspath $(K3S_TMP_DIR))/ssh-agent.sock
TOOLS_CLIENT_SSH_SOCK = $(if $(TOOLS_CLIENT_AGENT_PROBE),$(TOOLS_CLIENT_AGENT_PROBE),$(TOOLS_CLIENT_AGENT_SOCK))

SSH_COMMON_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentityAgent=$(TOOLS_CLIENT_SSH_SOCK)

# The client-side toolchain image, built from shoggoth/dockerfiles/tools with
# `--target client` — see that file's header for why the host-side tools are a
# second image rather than part of `:host`. It carries kube-linter,
# updatecli, KubeDiagrams with the graphviz `dot` it shells out to,
# ansible-core with the two collections and the `kubernetes` client its
# modules import, kubectl, helm, yq, jq, yamlfmt, yamllint and rsync, so every
# target that would otherwise need one of those on the host needs only docker.
TOOLS_IMAGE_CLIENT?=asherikov/shoggoth_tools:client

# Where the repo is mounted inside the container. Every path these fragments
# pass is repo-relative, so a fixed root is what makes them resolve with no
# rewriting — the working directory itself is baked into the image
# (WORKDIR in the client stage of shoggoth/dockerfiles/tools), not passed
# per call site; the one exception, the kube-linter targets in make/lint.mk,
# keep their own read-only mount at $(KUBE_LINTER_WORKDIR).
TOOLS_CLIENT_WORKDIR?=/work

# -it when the recipe has a terminal, -i otherwise. The ansible family needs
# stdin: `--ask-become-pass` prompts, and a prompt on /dev/null is an error
# rather than a question. Same detection as the `shell` target in
# make/k3s.mk, in a variable so every call site shares it. Only the ansible
# prefix and the interactive kubectl exec of `make shell` use it — the other
# client-image sites read no stdin at all, not even a pipe: the helm and yq
# invocations and the kube-linter pair take their input from the mounted
# repo, the kubectl operator calls are non-interactive, and the clusterviz
# pipeline keeps its stream inside one container. The one exception is
# $(TOOLS_CLIENT_JQ) below, which carries a plain `-i` instead: every jq site
# is mid-pipeline, and docker without `-i` hands the container a closed
# stdin, so `kubectl ... -o json | jq` would filter an empty stream.
TOOLS_CLIENT_TTY = $$([ -t 0 ] && printf -- -it || printf -- -i)

# The invariant part of every `docker run` in this tree that uses the client
# image. The image itself is NOT here: docker stops parsing flags at the
# image name, and a call site often needs flags of its own after this prefix
# — a read-only mount, an `-e`. So each site spells
# `$(TOOLS_CLIENT_RUN) <its flags> $(TOOLS_IMAGE_CLIENT) <command>`, which
# also makes visible which image runs what. The four exceptions are
# $(TOOLS_CLIENT_HELM), $(TOOLS_CLIENT_YQ), $(TOOLS_CLIENT_JQ) and
# $(TOOLS_CLIENT_KUBECTL) below, which fold the prefix into the tool name
# because there are enough call sites for them to be worth sharing.
#
# Host networking: the container shares the operator host's network
# namespace. That is what lets the apiserver forward `make tunnel_up`
# binds on the host's own loopback (make/k3s.mk) answer the kubeconfig's
# `server: https://127.0.0.1:6443` verbatim — the kubeconfig is the host's
# k3s.yaml unmodified, so nothing rewrites it and no second port has to
# agree with it. One forward serves every container in the make run, and
# the ssh that carries it lives on the host, outside all of them.
# Outbound access is the host's own stack rather than a bridge NAT, which
# is all make/lint.mk's schema fetch and `make update` need. The invoking
# uid, so nothing the container writes lands root-owned in the tree.
TOOLS_CLIENT_RUN = docker run --rm --network host --user "$$(id -u):$$(id -g)"

# The repo itself, read-write: `make update` rewrites pinned versions through
# it, yamlfmt rewrites the chart's YAML through it, and clusterviz writes
# its diagrams into docs/. A read-only variant would need a second spelling of
# the same mount, and the one target that mounted it read-only is the
# kube-linter pair, which keeps its own.
TOOLS_CLIENT_REPO = -v $(CURDIR):$(TOOLS_CLIENT_WORKDIR)

# SHOGGOTH_CHART_ROOT as the container sees it, for the helm invocations the
# Makefile runs inside the client image. The variable is absolute, and a host
# absolute path resolves to nothing inside; every other path these fragments
# pass is repo-relative and needs no such mapping.
SHOGGOTH_CHART_ROOT_CLIENT = $(patsubst $(CURDIR)/%,$(TOOLS_CLIENT_WORKDIR)/%,$(SHOGGOTH_CHART_ROOT))

# helm and yq as `docker run` prefixes, so a call site reads
# `$(TOOLS_CLIENT_HELM) template ...` and the image and the repo mount follow
# behind it. Both used to be client-host prerequisites; they now run from the
# client image like kube-linter and updatecli, and docker is the only thing
# the host needs. Each site is its own container, which is what keeps the
# invocations independent — the clusterviz pipeline in make/chore.mk is the
# one place where helm, yq and the diagram tools share one, because there the
# stream between them is the point.
TOOLS_CLIENT_HELM = $(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_REPO) $(TOOLS_IMAGE_CLIENT) helm
TOOLS_CLIENT_YQ = $(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_REPO) $(TOOLS_IMAGE_CLIENT) yq

# jq as a `docker run` prefix for the JSON filters in make/repair.mk,
# make/k3s.mk and make/gitea.mk, which used to run a jq installed on the
# client host. Two differences from the pair above. No repo mount: every site
# is a mid-pipeline filter — `kubectl ... -o json | jq`, `curl ... | jq` —
# that touches no repo file. And `-i`: docker otherwise hands the container a
# closed stdin, and a jq with no file argument filters an empty stream. No new
# image content either — the `tools` stage of shoggoth/dockerfiles/tools
# installs jq for the in-cluster scripts (openbao.yaml's bootstrap, the
# Secret-reading convention the README documents) and `client` is built FROM
# it, so the binary is already there.
TOOLS_CLIENT_JQ = $(TOOLS_CLIENT_RUN) -i $(TOOLS_IMAGE_CLIENT) jq

# What the plays inside the container need about the host they are driving:
# the ssh user and the host's address, as `-e` flags. The image has no
# passwd entry for the invoking uid — `id -un` there prints the number —
# and group_vars/instances.yml reads the ssh user from
# lookup('env','USER') and the address from lookup('env','HOST_IP'), so
# without them the ssh user becomes the uid number and the connection
# fails on an empty host. Only the ansible prefixes carry this pair: the
# kubectl operator family opens no connection of its own, it reads the
# cluster through the host-side forward in the shared network namespace.
TOOLS_CLIENT_SSH_ENV = -e USER=$(or $(USER),$(shell id -un)) -e HOST_IP=$(HOST_IP)

# kubectl as a `docker run` prefix for the operator targets in make/k3s.mk and
# make/repair.mk, which used to run it on the host. KUBECONFIG is baked into
# the image (ENV in shoggoth/dockerfiles/tools) at the repo-relative
# private/kubeconfig `make control_setup` fetches.
#
# The container opens no connection of its own: it shares the operator
# host's network namespace ($(TOOLS_CLIENT_RUN)), where `make tunnel_up`
# has already bound the apiserver forward on the host's own loopback, so
# the kubeconfig's `https://127.0.0.1:6443` answers here unchanged. That
# is why the prefix carries no agent socket, no passwd/group mounts and
# no ssh at all — the ssh lives on the host, and every kubectl target
# names `tunnel_up` as its prerequisite. One forward serves every
# container, where an ssh per container paid for the forward on each call.
# `HOME` is baked into the image (ENV in shoggoth/dockerfiles/tools): the
# invoking uid has no home there, and kubectl's discovery cache needs a
# writable one.
#
# The TTY variant is for one call site, `make shell`'s final `kubectl exec`,
# which is interactive; every other kubectl call reads no stdin.
TOOLS_CLIENT_KUBECTL = $(TOOLS_CLIENT_RUN) \
    $(TOOLS_CLIENT_REPO) \
    $(TOOLS_IMAGE_CLIENT) kubectl
TOOLS_CLIENT_KUBECTL_TTY = $(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_TTY) \
    $(TOOLS_CLIENT_REPO) \
    $(TOOLS_IMAGE_CLIENT) kubectl

# The instance-config value the tree reads. A recursive variable re-expands
# on every reference, so a bare `$(shell yq)` per reference would start a
# container each time, and DOMAIN is referenced across the tree; the `:=`
# runs the container once per make process instead. `// "null"` keeps the
# field a non-empty string. With docker unavailable or the config missing
# the value comes out empty — the failure a missing host yq used to
# produce, reported where the value is used rather than at parse time.
SHOGGOTH_DOMAIN := $(shell $(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_REPO) $(TOOLS_IMAGE_CLIENT) yq -r '.identity.domain // "null"' $(TOOLS_CLIENT_WORKDIR)/$(SHOGGOTH_CONFIG) 2>/dev/null)
DOMAIN ?= $(SHOGGOTH_DOMAIN)

# No home mount. The plays' ssh authenticates through the agent socket
# $(TOOLS_CLIENT_IDENT_SOCK) forwards — OpenSSH reads it from
# SSH_AUTH_SOCK with no -A, which covers a key that lives in an agent or
# a hardware token — so no key file is ever read from the invoking user's
# home, and `make ssh_agent_setup` fails fast when neither a live agent
# nor a loadable key is there. The
# controller's staging dir is pinned to the container's /tmp by
# ansible.cfg's local_tmp, which is fresh for every play because the
# container is `docker run --rm`. HOME is baked into the image at that
# same /tmp (ENV in shoggoth/dockerfiles/tools) so a tool that writes
# "home" files (kubectl's discovery cache) has a writable target instead
# of a host path that does not exist inside the image.
#
# The passwd/group databases ride along, and they are load-bearing rather
# than incidental. OpenSSH resolves its uid with getpwuid(getuid())
# unconditionally — verified: `ssh -F <file> -G host` dies with "No user
# exists for uid <uid>" even with the config file given explicitly — and
# the home it finds there is where every `~` expansion lands, NOT $HOME
# (`HOME=/tmp/x ssh -G host` still prints userknownhostsfile under the
# passwd entry's home). The image carries no entry for the invoking uid,
# so without these mounts every ssh aborts before it opens a socket — the
# undelegated host_state tasks and the synchronize task's
# `rsync --rsh ssh` alike. What the mounts no longer have to cover is a
# user config file: none is read, because the config source is pinned
# everywhere — `-F /dev/null` in ansible.cfg's ssh_common_args covers
# every ssh in the image. Read-only: nothing in the container writes them.
TOOLS_CLIENT_IDENT = -v /etc/passwd:/etc/passwd:ro \
    -v /etc/group:/etc/group:ro

# Where the agent socket is mounted INSIDE the container. A fixed path,
# and it is part of the image's contract rather than a value of its own:
# the client stage of shoggoth/dockerfiles/tools bakes `ENV SSH_AUTH_SOCK`
# at it, so ssh, ssh-add, the plays' own ssh and rsync all find the
# forwarded agent with no `-e` at any call site — the mount alone is the
# whole host side. The HOST side of the mount stays
# $(TOOLS_CLIENT_SSH_SOCK): the operator's own agent when one answers,
# make's otherwise.
TOOLS_CLIENT_AGENT_MOUNT = /ssh-agent.sock

# The agent socket, which only the live sites carry. The mount is
# unconditional: $(TOOLS_CLIENT_SSH_SOCK) always resolves to a path — the
# environment's agent when one answers, otherwise
# $(TOOLS_CLIENT_AGENT_SOCK) — and `ssh_agent_setup` guarantees an agent
# is listening there before any container that carries this starts, so a
# live container never starts with no agent at all.
#
# The offline ansible sites deliberately take $(TOOLS_CLIENT_IDENT)
# instead: they run `--syntax-check` and an inventory export, which open
# no ssh connection and must stay offline, and a bind mount of a socket
# that does not exist would have the docker daemon create a directory in
# its place.
TOOLS_CLIENT_IDENT_SOCK = $(TOOLS_CLIENT_IDENT) \
    -v $(TOOLS_CLIENT_SSH_SOCK):$(TOOLS_CLIENT_AGENT_MOUNT)

# What the container cannot infer about the host it is driving:
#
#   * USER and HOST_IP, because the image has no passwd entry for the
#     invoking uid and `id -un` there prints the number.
#     group_vars/instances.yml reads the ssh user from lookup('env','USER')
#     and the address from lookup('env','HOST_IP'), so without them the
#     ssh user becomes "1000" and the connection fails on an empty host.
#     Both come from $(TOOLS_CLIENT_SSH_ENV) — the tunnel in front of every
#     live play authenticates with the same pair.
#   * KUBECONFIG, baked into the image (ENV in shoggoth/dockerfiles/tools)
#     at the repo-relative private/kubeconfig the plays read.
#   * ANSIBLE_CONFIG, because ansible searches ./ansible.cfg, then
#     ~/.ansible.cfg, then /etc/ansible/ansible.cfg — and this repo keeps its
#     config in ansible/ansible.cfg, which none of those find.
TOOLS_CLIENT_ANSIBLE_ENV = $(TOOLS_CLIENT_SSH_ENV) \
    -e ANSIBLE_CONFIG=$(TOOLS_CLIENT_WORKDIR)/ansible/ansible.cfg

# Everything a live ansible-playbook needs, for the call sites in make/k3s.mk
# and make/repair.mk and for $(ANSIBLE) below. The offline lint sites do NOT
# take this one — they run $(TOOLS_CLIENT_ANSIBLE_OFF), which is the same
# prefix without the tunnel, because a syntax check and an inventory export
# touch no host and must not open an ssh connection to one.
#
# `--network host` (from $(TOOLS_CLIENT_RUN)) does double duty here: the
# delegated control-node tasks reach the apiserver through the host-side
# forward `make tunnel_up` binds on the host's loopback — the kubeconfig
# the plays read is the host's k3s.yaml verbatim and its server is
# `https://127.0.0.1:6443` — and the undelegated tasks' ssh out to
# $HOST_IP leaves through the host's own stack, exactly as the operator's
# own `make ssh` does. Every task in the plays is either `delegate_to:
# localhost` — the control node, which is this container — or an ssh to
# the host through the forwarded agent.
#
# No /etc/hosts mount: the delegated localhost tasks' service names
# (drop_image_tag.yml's call to registry.${DOMAIN}, a
# hostNetwork service) resolve through the container's own resolver, which
# forwards to the operator host's nameservers — on a VPN client those are
# the cluster CoreDNS (see shoggoth/chart/templates/dns.yaml).
TOOLS_CLIENT_ANSIBLE = $(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_TTY) \
    $(TOOLS_CLIENT_REPO) $(TOOLS_CLIENT_IDENT_SOCK) $(TOOLS_CLIENT_ANSIBLE_ENV) \
    $(TOOLS_IMAGE_CLIENT)

# The same prefix without the tunnel, for the offline ansible checks in
# make/lint.mk: `ansible-playbook --syntax-check` over every playbook and
# the roles wrapper, and `ansible-inventory --list --export`. They parse and
# resolve variables and nothing else, so an ssh to $HOST_IP would make an
# offline gate depend on a reachable host, a loaded agent and a live
# apiserver — three things that have nothing to do with whether a playbook
# parses. Same base, same identity mounts, same environment; the image name
# ends the prefix, so a call site appends its ansible command line to it.
TOOLS_CLIENT_ANSIBLE_OFF = $(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_TTY) \
    $(TOOLS_CLIENT_REPO) $(TOOLS_CLIENT_IDENT) $(TOOLS_CLIENT_ANSIBLE_ENV) \
    $(TOOLS_IMAGE_CLIENT)

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

# Guarantees the agent every ssh in this tree authenticates through, and
# starts one of make's own when the invoking shell has none to offer.
# It matters because a live container authenticates only
# through the forwarded agent: the image has no key file for the invoking
# uid (HOME is /tmp and no home is mounted), so an ssh with no agent
# falls back to a password prompt and burns MaxAuthTries until "Too many
# authentication failures" — an error that says nothing about the cause.
#
# Three cases, in the order they are decided:
#
#   1. The environment's agent answers with identities — the operator's
#      own agent, the normal case, holding every key and hardware token
#      they loaded. Nothing happens: no agent is started and nothing is
#      added to theirs.
#   2. A socket answers but holds no identity. Also nothing: the agent is
#      the operator's and its contents are their business, so this fails
#      with what to run rather than loading keys into an agent they may
#      be keeping deliberately empty — and a hardware token's key cannot
#      be loaded from a file at all.
#   3. Nothing answers — SSH_AUTH_SOCK unset, or pointing at a socket
#      whose agent has exited, the usual leftover of a rebooted shell.
#      make starts its own agent on $(TOOLS_CLIENT_AGENT_SOCK) — reusing
#      one already listening there — and loads the default identity files
#      (~/.ssh/id_ed25519, id_rsa, id_ecdsa, ...) into it with a plain
#      `ssh-add`. A passphrase-protected key prompts there, on the
#      terminal make is already using.
#
# The agent is deliberately left running. Recipes are separate shells, so
# the socket path — not an exported variable — is what carries it to the
# containers ($(TOOLS_CLIENT_IDENT_SOCK)) and to the host-side ssh calls
# (`IdentityAgent` in $(SSH_COMMON_ARGS)), and the next make run finds
# the same agent through the same path and skips all of this.
# `ssh-agent -k` with SSH_AUTH_SOCK pointed at it, or removing
# $(TOOLS_CLIENT_AGENT_SOCK) and re-running, is how it goes away.
#
# A live target can rely on the socket being there because
# $(TOOLS_CLIENT_SSH_SOCK) resolves to the environment's agent when one
# answers and to $(TOOLS_CLIENT_AGENT_SOCK) otherwise, and every target
# that opens the tunnel or runs a play carries this prerequisite. The one
# target that does NOT carry it — `make ssh` — keeps its key-file fallback
# for as long as no agent is listening on $(TOOLS_CLIENT_AGENT_SOCK): ssh
# ignores an IdentityAgent path that does not answer, and once a live
# target has started the agent it authenticates through it like everything
# else.
.PHONY: ssh_agent_setup
ssh_agent_setup:
	@ssh-add -l >/dev/null 2>&1; rc=$$?; \
	if [ $$rc -eq 0 ]; then exit 0; fi; \
	if [ $$rc -eq 1 ]; then \
		echo "FAIL: the agent at $$SSH_AUTH_SOCK holds no identity — ssh-add the key for $${USER:-$$(id -un)}@${HOST_IP}"; \
		exit 1; \
	fi; \
	SOCK="$(TOOLS_CLIENT_AGENT_SOCK)"; \
	mkdir -p "$$(dirname "$$SOCK")"; \
	SSH_AUTH_SOCK="$$SOCK" ssh-add -l >/dev/null 2>&1; src=$$?; \
	if [ $$src -eq 0 ]; then exit 0; fi; \
	if [ $$src -ne 1 ]; then \
		rm -f "$$SOCK"; \
		ssh-agent -a "$$SOCK" >/dev/null \
			|| { echo "FAIL: could not start an agent at $$SOCK — ssh-agent exited $$?"; exit 1; }; \
		echo "ssh_agent_setup: started an agent at $$SOCK"; \
	fi; \
	export SSH_AUTH_SOCK="$$SOCK"; \
	ssh-add || { echo "FAIL: no key loaded into the agent at $$SOCK — ssh-add looks for the default identities (~/.ssh/id_ed25519, id_rsa, id_ecdsa); a hardware token's key cannot be loaded from a file, so export SSH_AUTH_SOCK at your own agent instead"; exit 1; }

help:
	grep -hv "^	" Makefile make/*.mk | grep -v "^ " | grep -v "^$$" | grep -v "^\."

-include make/*.mk
-include private/*.mk


# Tear the instance down and bring it back up from scratch. The tunnel is
# a prerequisite of both halves, so it is up before either runs.
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

ping:
	ping -c 1 "${HOST_IP}"

home:
	firefox http://${DOMAIN}

test:
	echo "Requires VPN connectivity"
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
		--domain "${DOMAIN}" \
		--ai-token ${AI_TOKEN} \
		--ssh-config
