# offline chart validation.

.PHONY: helm_lint helm_lint_chart helm_lint_render helm_lint_dupes \
        helm_lint_gates helm_lint_collision helm_lint_roledef

# Values for the offline renders. This is just the instance config: the
# checks render to a file and never apply anything, so there is nothing here
# for a throwaway override to protect. The release name and namespace are
# what distinguish lint output from a real install.
SHOGGOTH_HELM_VALUES_LINT = $(SHOGGOTH_HELM_VALUES)

# Where the lint scratch lives: the helm renders above, and the ansible/
# role wrapper playbook that ansible_lint_syntax generates below.
LINT_TMP_DIR=${K3S_TMP_DIR}/lint
LINT_RENDER=${LINT_TMP_DIR}/render.yaml
LINT_COLLISION_A=${LINT_TMP_DIR}/collision-a.yaml
LINT_COLLISION_B=${LINT_TMP_DIR}/collision-b.yaml

# Everything a render depends on. A change to any of these re-renders.
# files/ is listed through `find` rather than $(wildcard .../**): the chart's
# verbatim-mounted content (Grafana dashboards, plugin sources) lives there,
# and make's `**` wildcard is version-dependent while find is not. An edit to
# a file under files/ must invalidate the render exactly like a template edit.
CHART_SOURCES=$(SHOGGOTH_CHART_ROOT)/Chart.yaml \
	$(SHOGGOTH_CHART_ROOT)/values.yaml \
	$(wildcard $(SHOGGOTH_CHART_ROOT)/templates/*.yaml) \
	$(wildcard $(SHOGGOTH_CHART_ROOT)/templates/*.tpl) \
	$(shell find $(SHOGGOTH_CHART_ROOT)/files -type f 2>/dev/null)

${LINT_TMP_DIR}:
	mkdir -p $@

helm_lint: helm_lint_chart helm_lint_render helm_lint_dupes \
           helm_lint_gates helm_lint_collision helm_lint_roledef

# `helm lint` catches Go-template parse errors and malformed chart
# metadata, and reports both itself.
helm_lint_chart:
	helm lint $(SHOGGOTH_CHART_ROOT) --strict

# The renders are written to a temporary name and moved into place, so a
# failed render leaves no half-written file behind for the next check to
# read as if it were good.
${LINT_RENDER}: ${CHART_SOURCES} $(SHOGGOTH_CONFIG) | ${LINT_TMP_DIR}
	helm template shoggoth-lint $(SHOGGOTH_CHART_ROOT) \
		--namespace lint \
		$(SHOGGOTH_HELM_VALUES_LINT) \
		> $@.tmp
	mv $@.tmp $@

${LINT_COLLISION_A}: ${CHART_SOURCES} $(SHOGGOTH_CONFIG) | ${LINT_TMP_DIR}
	helm template shoggoth-collision-a $(SHOGGOTH_CHART_ROOT) \
		--namespace collision-a \
		$(SHOGGOTH_HELM_VALUES_LINT) \
		> $@.tmp
	mv $@.tmp $@

${LINT_COLLISION_B}: ${CHART_SOURCES} $(SHOGGOTH_CONFIG) | ${LINT_TMP_DIR}
	helm template shoggoth-collision-b $(SHOGGOTH_CHART_ROOT) \
		--namespace collision-b \
		$(SHOGGOTH_HELM_VALUES_LINT) \
		> $@.tmp
	mv $@.tmp $@

helm_lint_render: ${LINT_RENDER}
	wc -l < ${LINT_RENDER}

# Regression guard for the duplicate-label-key bug fixed in
# argo-webhook-receiver, maintenance-secret-writer, sandbox-spawner,
# external-secrets-webhooks, and reloader — every template was emitting
# `app.kubernetes.io/part-of: shoggoth` literally after invoking
# `shoggoth.labels`, producing invalid YAML 1.2 (duplicate mapping keys)
# that breaks kubeconform -strict and any strict YAML 1.2 parser.
#
# The config enables key-duplicates and NOTHING else. `extends: default`
# would drag in every default rule, so an unrelated `colons` violation
# (too many spaces after a colon) exits non-zero and would be reported
# here as a duplicate-key regression.
#
# yamllint prints the offending lines itself and exits non-zero, so there
# is nothing to catch and re-word. A missing yamllint says so.
helm_lint_dupes: ${LINT_RENDER}
	yamllint -d '{rules: {key-duplicates: enable}}' ${LINT_RENDER}

# A union gate spanning a service's own workloads makes `make stop
# SERVICE=<x>` a silent no-op: `or (x) (y)` still renders while y is on,
# so helm reports a successful upgrade and nothing is deleted. openbao hit
# this — its Service, PVC and StatefulSet sat under
# `or(openbao, openbao-bootstrap)`, and it was the only one of the 43 keys
# whose toggle removed nothing. This renders once per key with that key
# alone disabled and checks that the resources it owns are gone, which
# catches the class rather than just that instance.
#
# The comparison is by resource identity (kind/name), not by a document
# count: a toggle that removes one resource and adds another leaves the
# count unchanged, and a count comparison would report that as "removes
# nothing" even though it did remove one.
#
# Every key that removes nothing is a line missing from the right-hand
# list, and `diff` prints it by name.
#
# `grep -c ''` prints the number of lines and exits non-zero on an empty
# file, so the key list is asserted non-empty by the tool that counts it.
# A key whose render failed is a zero in the size column, and `diff`
# against an empty file prints the key that produced it.
helm_lint_gates: ${LINT_RENDER}
	yq -r 'select(.kind != null) | .kind + "/" + .metadata.name' ${LINT_RENDER} \
		| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/gates-base.ids
	yq -r '.services | keys | .[]' $(SHOGGOTH_CHART_ROOT)/values.yaml \
		| sort > ${LINT_TMP_DIR}/gates-keys.txt
	grep -c '' ${LINT_TMP_DIR}/gates-keys.txt
	for k in $$(cat ${LINT_TMP_DIR}/gates-keys.txt); do \
		helm template shoggoth-lint $(SHOGGOTH_CHART_ROOT) \
			--namespace lint \
			$(SHOGGOTH_HELM_VALUES_LINT) \
			--set services.$$k.enabled=false \
			> ${LINT_TMP_DIR}/gate-$$k.yaml; \
		printf '%s %s\n' "$$k" "$$(wc -l < ${LINT_TMP_DIR}/gate-$$k.yaml)"; \
	done > ${LINT_TMP_DIR}/gates-sizes.txt
	sed -n '/ 0$$/p' ${LINT_TMP_DIR}/gates-sizes.txt > ${LINT_TMP_DIR}/gates-empty.txt
	diff /dev/null ${LINT_TMP_DIR}/gates-empty.txt
	for k in $$(cat ${LINT_TMP_DIR}/gates-keys.txt); do \
		yq -r 'select(.kind != null) | .kind + "/" + .metadata.name' \
			${LINT_TMP_DIR}/gate-$$k.yaml \
			| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/gate-off.ids; \
		comm -23 ${LINT_TMP_DIR}/gates-base.ids ${LINT_TMP_DIR}/gate-off.ids \
			| sed "s|^|$$k |"; \
	done > ${LINT_TMP_DIR}/gates-removed.txt
	cut -d' ' -f1 ${LINT_TMP_DIR}/gates-removed.txt | sort -u \
		> ${LINT_TMP_DIR}/gates-keys-removed.txt
	diff ${LINT_TMP_DIR}/gates-keys.txt ${LINT_TMP_DIR}/gates-keys-removed.txt

# A cluster-scoped object (ClusterRole, ClusterRoleBinding) has no
# namespace to separate two shoggoth instances on one cluster. When both
# instances render the SAME name, `make up` — which passes --take-ownership
# — silently re-labels the first instance's ClusterRole to the second
# release and rewrites the ClusterRoleBinding's `subjects` list
# (rbac/v1.Subjects is an atomic list with no merge key, so the whole list
# is replaced and the first instance's ServiceAccount drops out). `make
# down` on either instance then deletes the shared objects outright,
# leaving the other instance's collector / Argo controller / ESO operator
# without permissions.
#
# Only ClusterRole and ClusterRoleBinding are checked: CRDs are
# cluster-scoped too, but they are shared BY DESIGN (two instances cannot
# own two copies of the same CRD) and are not part of the chart at all —
# `make up` applies them from their upstream sources before Helm
# runs.
#
# A name the chart hardcodes rather than deriving from the release name is
# the one that collides, so `diff` against an empty file reports it.
helm_lint_collision: ${LINT_COLLISION_A} ${LINT_COLLISION_B}
	yq -r 'select(.kind == "ClusterRole" or .kind == "ClusterRoleBinding") | .kind + "/" + .metadata.name' \
		${LINT_COLLISION_A} | sed '/^---$$/d' | sort -u \
		> ${LINT_TMP_DIR}/collision-a.names
	yq -r 'select(.kind == "ClusterRole" or .kind == "ClusterRoleBinding") | .kind + "/" + .metadata.name' \
		${LINT_COLLISION_B} | sed '/^---$$/d' | sort -u \
		> ${LINT_TMP_DIR}/collision-b.names
	comm -12 ${LINT_TMP_DIR}/collision-a.names ${LINT_TMP_DIR}/collision-b.names \
		> ${LINT_TMP_DIR}/collision-shared.names
	diff /dev/null ${LINT_TMP_DIR}/collision-shared.names

# The rename moved each ClusterRole and its ClusterRoleBinding together,
# and `ClusterRoleBinding.roleRef` is immutable server-side — so the two
# must move in lockstep or the binding is stuck pointing at a name that no
# longer exists. Kubernetes does not validate a roleRef target at
# admission: the binding is created, then grants nothing, and the bound
# ServiceAccount loses every rule the ClusterRole carried. A name-only
# check cannot see this, so the roleRef of every rendered binding is
# resolved against the ClusterRoles the SAME render defines.
#
# The Kubernetes built-in aggregate roles (admin, edit, view, cluster-admin)
# and the system: ones are created by the cluster, not by this chart, so a
# binding to one is not a dangling reference.
#
# `join -v 1` prints the bindings whose roleRef target no ClusterRole in
# the same render defines, and `diff` against an empty file fails on them.
# The extractions are asserted non-empty by `grep -c ''`, which prints the
# number of lines and exits non-zero on an empty file: a successful render
# always carries both ClusterRoles and ClusterRoleBindings, so an empty
# extraction means the extraction failed and there is nothing to resolve.
helm_lint_roledef: ${LINT_COLLISION_A} ${LINT_COLLISION_B}
	yq -r 'select(.kind == "ClusterRole") | .metadata.name' ${LINT_COLLISION_A} \
		| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/collision-a.roles
	yq -r 'select(.kind == "ClusterRole") | .metadata.name' ${LINT_COLLISION_B} \
		| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/collision-b.roles
	yq -r 'select(.kind == "ClusterRoleBinding" and .roleRef.kind == "ClusterRole") | .roleRef.name + " " + .metadata.name' \
		${LINT_COLLISION_A} \
		| sed -E '/^---$$/d; /^(admin|edit|view|cluster-admin|system:)/d' \
		| sort -u -k1,1 > ${LINT_TMP_DIR}/collision-a.rolebindings
	yq -r 'select(.kind == "ClusterRoleBinding" and .roleRef.kind == "ClusterRole") | .roleRef.name + " " + .metadata.name' \
		${LINT_COLLISION_B} \
		| sed -E '/^---$$/d; /^(admin|edit|view|cluster-admin|system:)/d' \
		| sort -u -k1,1 > ${LINT_TMP_DIR}/collision-b.rolebindings
	grep -c '' ${LINT_TMP_DIR}/collision-a.roles
	grep -c '' ${LINT_TMP_DIR}/collision-b.roles
	grep -c '' ${LINT_TMP_DIR}/collision-a.rolebindings
	grep -c '' ${LINT_TMP_DIR}/collision-b.rolebindings
	join -v 1 -t' ' ${LINT_TMP_DIR}/collision-a.rolebindings ${LINT_TMP_DIR}/collision-a.roles \
		> ${LINT_TMP_DIR}/collision-a.unresolved
	join -v 1 -t' ' ${LINT_TMP_DIR}/collision-b.rolebindings ${LINT_TMP_DIR}/collision-b.roles \
		> ${LINT_TMP_DIR}/collision-b.unresolved
	cat ${LINT_TMP_DIR}/collision-a.unresolved ${LINT_TMP_DIR}/collision-b.unresolved \
		> ${LINT_TMP_DIR}/collision.unresolved
	diff /dev/null ${LINT_TMP_DIR}/collision.unresolved

# ---------------------------------------------------------------------------
# ansible/ offline validation
# ---------------------------------------------------------------------------

.PHONY: ansible_lint ansible_lint_syntax \
        ansible_lint_vars ansible_check

# Every .yml under ansible/ that is a PLAYBOOK. Three paths are excluded,
# because `--syntax-check` on any of them fails with "a playbook must be a
# list of plays" (or simply finds no play at all): requirements.yml is a
# collection requirements list, group_vars/ is variable data rather than a
# playbook, and roles/ holds TASK lists rather than playbooks — a role's
# tasks/main.yml is the body of a task, not the body of a play. Everything
# else is included by walking the tree rather than by listing it, so a new
# playbook anywhere under ansible/ is covered the moment it exists and needs
# nobody to remember to add it here. A new non-playbook .yml fails loudly
# instead, which is the cheap direction to fail in.
#
# `find` rather than make's `**`: the wildcard is version-dependent while
# find is not, which is the same reason CHART_SOURCES above uses it.
#
# Excluding roles/ is NOT the same as skipping it. A flat
# `$(wildcard ansible/*.yml)` silently skipped
# roles/host_state/tasks/main.yml, and nothing else covered it either:
# deploy.yml reaches the role with `include_role`, which is the DYNAMIC
# form — the role is loaded when that task executes, not when the playbook
# is parsed, so `--syntax-check ansible/deploy.yml` never read a line of
# it. The wrapper loop in ansible_lint_syntax is what closes that hole,
# using `import_role`, the static form a syntax check does resolve.
ANSIBLE_PLAYBOOKS = $(filter-out ansible/requirements.yml \
                                  ansible/group_vars/% \
                                  ansible/roles/%, \
                      $(shell find ansible -type f -name '*.yml' | sort))

# Every role directory under ansible/roles/, as bare names. Empty when the
# tree has no roles at all, which the recipe checks for before generating a
# wrapper for nothing.
ANSIBLE_ROLES = $(patsubst ansible/roles/%,%, \
                  $(shell find ansible/roles -mindepth 1 -maxdepth 1 \
                               -type d 2>/dev/null | sort))

# The cheapest guard the ansible/ tree can carry: every playbook parses and
# the inventory resolves. No cluster, no collections, no host.
#
# It catches what `make -n` structurally cannot — a dry run prints the
# ansible-playbook command line without ever parsing the playbook, so a YAML
# typo, a bad task name or a broken inventory reference otherwise reaches
# the host on the first real run. This is also the only test in the repo
# that touches the ansible/ tree at all: there is no CI here (see
# ansible/README.md), so whatever is not checked locally is not checked.
#
# A missing ansible-playbook says so and skips, exactly the way
# helm_lint_dupes treats a missing yamllint. ansible-core is a control-node
# prerequisite, not something this target installs.
#
# ANSIBLE_CONFIG is spelled out here rather than reusing the Makefile's
# $(ANSIBLE) wrapper: this loop runs a bare `ansible-playbook`, and an empty
# ANSIBLE_CONFIG does not mean "use the defaults" — it means ansible.cfg is
# not applied, so `inventory = hosts.ini` is lost and the check runs against
# whatever /etc/ansible/hosts happens to hold. A syntax check that passes
# because it parsed no inventory at all is worse than one that fails.
#
# The second half of the recipe is the roles/ tree, and it generates a
# playbook rather than checking the role files in place. `--syntax-check`
# takes a playbook, and a role's tasks/main.yml is a task list, so the only
# way to hand it one is to build a play that pulls the roles in — with
# `import_role`, because that is the form resolved while the playbook is
# being PARSED, which is the only moment a syntax check looks at. The
# wrapper is generated from $(ANSIBLE_ROLES) rather than committed, so a new
# role is checked without anyone editing a list; committing it would
# reproduce the exact silent-skip the walk above just removed, one level
# down. ANSIBLE_ROLES_PATH is set because the wrapper does not live beside
# the roles/ directory it names, and ANSIBLE_INVENTORY because the wrapper
# asks for `localhost` and must not depend on the configured inventory
# resolving at all.
ANSIBLE_CFG = ansible/ansible.cfg

ansible_lint: ansible_lint_syntax ansible_lint_vars

# Order-only on the scratch directory: the wrapper playbook below is written
# into it, and it must exist before the recipe runs rather than being a
# reason for the recipe to re-run.
ansible_lint_syntax: | ${LINT_TMP_DIR}
	@if ! command -v ansible-playbook >/dev/null 2>&1; then \
		echo "ansible_lint_syntax: ansible-playbook not installed — skipped"; \
		echo "  (ansible-core is a control-node prerequisite; see README.md)"; \
		exit 0; \
	fi; \
	for play in $(ANSIBLE_PLAYBOOKS); do \
		echo "syntax-check $$play"; \
		ANSIBLE_CONFIG=$(ANSIBLE_CFG) ansible-playbook --syntax-check $$play || exit 1; \
	done; \
	if [ -n "$(ANSIBLE_ROLES)" ]; then \
		{ \
			printf '%s\n' '---'; \
			printf '%s\n' '- name: Syntax-check the roles tree'; \
			printf '%s\n' '  hosts: localhost'; \
			printf '%s\n' '  gather_facts: false'; \
			printf '%s\n' '  tasks:'; \
			for role in $(ANSIBLE_ROLES); do \
				printf '    - name: Load the %s role\n' "$$role"; \
				printf '      ansible.builtin.import_role:\n'; \
				printf '        name: %s\n' "$$role"; \
			done; \
		} > ${LINT_TMP_DIR}/roles.yml; \
		echo "syntax-check roles: $(ANSIBLE_ROLES)"; \
		ANSIBLE_CONFIG=$(ANSIBLE_CFG) \
			ANSIBLE_INVENTORY=ansible/hosts.ini \
			ANSIBLE_ROLES_PATH=ansible/roles \
			ansible-playbook --syntax-check ${LINT_TMP_DIR}/roles.yml || exit 1; \
	fi

# ---------------------------------------------------------------------------
# ansible/ variable resolution
# ---------------------------------------------------------------------------

# The inventory and every group_vars expression, resolved.
#
# `--syntax-check` parses a playbook; it never evaluates a variable.
# group_vars/instances.yml is almost ENTIRELY expressions —
# `lookup('env', 'USER')`, `lookup('env', 'HOST_IP')`, `inventory_dir`,
# `normpath`, `query('fileglob', ...)` — and that file's own header carries
# the warning that a pinned ansible-core may refuse to resolve a magic
# variable inside group_vars, with the fix being to move the block into each
# play's `vars:`. Nothing else here can see that happen: the syntax check
# passes, the play starts, and the first task that reads
# `shoggoth_repo_root` dies with a message naming neither the file nor the
# variable.
#
# `ansible-inventory --list --export` builds the whole inventory and prints
# it, which is the cheapest way to force every one of those expressions to
# be evaluated without connecting to a host. An expression that fails is an
# error on stderr and a non-zero exit; one that resolves is a key in the
# output. Discarding stdout is deliberate — the exit status is the check,
# and the inventory is large.
#
# HOST_IP is exported rather than passed with `-e`: the expression reads it
# with `lookup('env', ...)`, and an extra var would be a second spelling of
# the same value that could disagree with the environment the real run sees.
#
# The success echo rides on the command with `&&` rather than sitting on its
# own recipe line: every recipe line is a separate shell, so a line of its own
# would run after the skip path's `exit 0` and report success on a host with
# no ansible-inventory at all. A failing `ansible-inventory` prints the
# expression it could not resolve and exits non-zero, so there is no `||` arm.
ansible_lint_vars:
	@if ! command -v ansible-inventory >/dev/null 2>&1; then \
		echo "ansible_lint_vars: ansible-inventory not installed — skipped"; \
		echo "  (ansible-core is a control-node prerequisite; see README.md)"; \
		exit 0; \
	fi; \
	HOST_IP="$(HOST_IP)" ANSIBLE_CONFIG=$(ANSIBLE_CFG) \
		ansible-inventory --list --export >/dev/null && \
		echo "ansible_lint_vars: the inventory and every group_vars expression resolve"

# ---------------------------------------------------------------------------
# ansible/ dry run
# ---------------------------------------------------------------------------

# The service the `service.yml` dry runs flip. The play asserts the name is
# non-empty and defaults it to nothing, so the check has to name one. The
# first key in values.yaml is as good as any: check mode skips the `helm`
# module, so none of the three actions reaches a cluster. Same derivation as
# helm_lint_gates' key list above.
ANSIBLE_CHECK_SERVICE ?= $(shell yq -r '.services | keys | .[0]' \
	$(SHOGGOTH_CHART_ROOT)/values.yaml 2>/dev/null)

# `ansible-playbook --check --diff` over every playbook the make targets
# invoke, in the same shape they invoke them.
#
# WHAT IT BUYS, and it is less than the name suggests: check mode SKIPS
# every `ansible.builtin.command` task, and the plays carry the whole deploy
# through command tasks (`kubectl apply`, `kubectl wait`, `helm upgrade`,
# `helm status`). What is left is what this target is for:
#
#   * every `assert` runs, so an empty `SERVICE` is caught here rather than at
#     the host;
#   * `include_vars` loads shoggoth/<instance>.yaml for real — the one step a
#     syntax check cannot reach, and the one that breaks silently when the
#     instance config is renamed;
#   * the `host_state` role's `file` and `copy` tasks run and report "would
#     change", which is the only thing that proves the storage paths resolve
#     on the host;
#   * `k8s_info` queries the API, so the namespace and release-record
#     predicates are evaluated against the live cluster;
#   * `synchronize` contacts the host with `--dry-run --delete-after`;
#   * `clear_endpoints.yml` opts its reads back IN with `check_mode: false`.
#     Check mode would otherwise skip the `command` tasks that read the k3s
#     unit's argv and the kine datastore, and those reads are the part of
#     that play worth checking — they prove the datastore path and the SQL
#     against the live schema, which is exactly what a syntax check cannot
#     reach. They are SELECTs, so a check run reads a database a live
#     apiserver is writing and changes nothing. The write is gated the other
#     way, on `not ansible_check_mode`.
#
# What it does NOT buy is a rehearsal of the deploy: a chart change whose
# `helm upgrade` would abort passes this clean, because that task is a
# command task and is skipped.
#
# It needs a host, which is why it is not part of `ansible_lint`. `HOST_IP`
# has to resolve and the tunnel has to be up — the `tunnel_up` prerequisite
# and the `ping` under it are what enforce that, exactly as they do for
# `make up`. The become password is asked once per escalated play — three of
# them, one collection per `ansible-playbook` process — because check mode
# still runs the host_state role's `file` and `copy` tasks as root to report
# what they would change. A NOPASSWD host answers with an empty line (see the
# Makefile).
#
# Three of the plays need a value the real targets take from the command line.
# `SERVICE` is derived above. `drop_image_tag.yml` gets literals, and they
# are never used for a request: `uri` has no check-mode support, so its
# DELETE is SKIPPED and nothing is sent — a check that dropped the tag would
# not be a check. The literals exist only to get past the play's own usage
# assert and reach the `include_vars` and the URL construction behind it.
# `clear_endpoints.yml` gets one documentation address (TEST-NET-1) for the
# same reason and under the same guarantee: its write is gated on
# `not ansible_check_mode`, so no tombstone is written and k3s is never
# stopped.
#
# `purge.yml` is covered only when INSTANCE is not the primary instance: the
# play's own assert refuses `shoggoth` outright, so running it against the
# default INSTANCE would fail on that guard rather than on anything this
# target is looking for.
ansible_check: tunnel_up
	@if ! command -v ansible-playbook >/dev/null 2>&1; then \
		echo "ansible_check: ansible-playbook not installed — skipped"; \
		echo "  (ansible-core is a control-node prerequisite; see README.md)"; \
		exit 0; \
	fi; \
	if [ -z "$(ANSIBLE_CHECK_SERVICE)" ]; then \
		echo "ansible_check: no service name to flip — yq is missing or"; \
		echo "  .services in values.yaml is empty. Pass one explicitly:"; \
		echo "    make ansible_check ANSIBLE_CHECK_SERVICE=<app-name>"; \
		exit 1; \
	fi; \
	ansible_check_run() { \
		echo "--- check $$1 ($$2)"; \
		ANSIBLE_CONFIG=$(ANSIBLE_CFG) ansible-playbook \
			--check --diff \
			ansible/$$1 \
			--limit $(INSTANCE) \
			-e ansible_host=$(HOST_IP) \
			$$3 || exit 1; \
	}; \
	ansible_check_run deploy.yml "make up" "--ask-become-pass"; \
	ansible_check_run down.yml "make down"; \
	if [ "$(INSTANCE)" = "shoggoth" ]; then \
		echo "--- check purge.yml (make purge) — skipped: the play refuses the primary instance"; \
	else \
		ansible_check_run purge.yml "make purge" "--ask-become-pass"; \
	fi; \
	ansible_check_run kubeconfig.yml "make client_kubeconfig" "--ask-become-pass"; \
	for a in enable disable; do \
		ansible_check_run service.yml "action=$$a" \
			"-e shoggoth_service=$(ANSIBLE_CHECK_SERVICE) -e shoggoth_service_action=$$a"; \
	done; \
	ansible_check_run drop_image_tag.yml "make drop_image_tag" \
		"-e shoggoth_image=lint/not-a-real-image -e shoggoth_tag=check"; \
	ansible_check_run clear_endpoints.yml "make k3s_clear_endpoints" \
		"-e shoggoth_k3s_stale_ips=192.0.2.111 --ask-become-pass"
