# offline chart validation.

.PHONY: helm_lint helm_lint_chart helm_lint_render helm_lint_kube_linter \
        helm_lint_gates helm_lint_collision helm_lint_netpol \
        helm_lint_rollout helm_lint_embedded helm_lint_inspektor

# kube-linter's own config, kept beside the make fragments that consume it
# rather than at the repo root, so `.kube-linter.yaml` is not left lying
# around for kube-linter to pick up implicitly when someone runs it by hand
# against the chart directory.
KUBE_LINTER_CONFIG?=make/kube-linter.yaml

# kube-linter runs from the client toolchain image (`--target client` in
# shoggoth/dockerfiles/tools), so docker is the only prerequisite and the
# version is pinned in that file's ARGs.
#
# The binary is spelled out on the command line because that image has no
# ENTRYPOINT: it carries five tools, and an entrypoint could name only one of
# them. It is alpine-based with ca-certificates, which is what the schema
# check needs — see the HTTPS note on the target below.

# Where the repo is mounted inside the container, and the arguments as the
# container sees them.
#
# kube-linter resolves its arguments with filepath.Abs, and config.Load
# splits the --config path the same way, so an absolute HOST path means
# nothing inside. The mount puts $(CURDIR) at
# $(KUBE_LINTER_WORKDIR) and -w sets the working directory there, which
# makes a relative path resolve correctly with no help at all; the patsubst
# covers the one case that needs help, an absolute $(K3S_TMP_DIR) spelled
# out under the repo. The case it does NOT cover is a $(K3S_TMP_DIR)
# outside the repo: that tree is not mounted, so the render prerequisite
# never appears and the target fails on a missing file. `.tmp`, the
# default, is inside.
KUBE_LINTER_WORKDIR?=/lint
KUBE_LINTER_RENDER=$(patsubst $(CURDIR)/%,%,$(LINT_RENDER))
KUBE_LINTER_OFF=$(patsubst $(CURDIR)/%,%,$(LINT_INSPEKTOR_OFF))
KUBE_LINTER_CFG=$(patsubst $(CURDIR)/%,%,$(KUBE_LINTER_CONFIG))

# The kube-linter invocation, shared by helm_lint_kube_linter and
# helm_lint_inspektor — one container, one read-only mount of the repo at
# $(KUBE_LINTER_WORKDIR), one config — so there is nothing to install to run
# either. The mount is read-only because kube-linter writes nothing —
# kubeconform only creates a cache directory when `cache:` is set, and the
# config deliberately does not set it — and `--user` keeps a container
# process from being able to leave root-owned files in the tree even if
# that ever changed. If a `cache:` is ever added to the config, this mount
# has to lose its `:ro`. It is the one call site that does not share
# $(TOOLS_CLIENT_REPO): `update` rewrites pinned versions through its mount
# and this one only reads.
#
# The container needs outbound HTTPS: the schema check fetches 18 schemas
# from raw.githubusercontent.com and the CRDs-catalog on every run, since
# no cache is configured. On a host whose docker network is locked down,
# that is where a run fails, and the failure names the URL.
KUBE_LINTER_RUN = $(TOOLS_CLIENT_RUN) \
    -v $(CURDIR):$(KUBE_LINTER_WORKDIR):ro \
    -w $(KUBE_LINTER_WORKDIR) \
    $(TOOLS_IMAGE_CLIENT) \
    kube-linter lint

# Where the lint scratch lives: the helm renders above, and the ansible/
# role wrapper playbook that ansible_lint generates below.
LINT_TMP_DIR=${K3S_TMP_DIR}/lint
LINT_RENDER=${LINT_TMP_DIR}/render.yaml
LINT_COLLISION_A=${LINT_TMP_DIR}/collision-a.yaml
LINT_COLLISION_B=${LINT_TMP_DIR}/collision-b.yaml
LINT_INSPEKTOR_OFF=${LINT_TMP_DIR}/inspektor-off.yaml

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

helm_lint: helm_lint_chart helm_lint_render helm_lint_kube_linter \
           helm_lint_gates helm_lint_collision helm_lint_netpol \
           helm_lint_rollout helm_lint_embedded helm_lint_inspektor

# `helm lint` catches Go-template parse errors and malformed chart
# metadata, and reports both itself.
helm_lint_chart:
	$(TOOLS_CLIENT_HELM) lint $(SHOGGOTH_CHART_ROOT_CLIENT) --strict

# The renders are written to a temporary name and moved into place, so a
# failed render leaves no half-written file behind for the next check to
# read as if it were good.
${LINT_RENDER}: ${CHART_SOURCES} $(SHOGGOTH_CONFIG) | ${LINT_TMP_DIR}
	$(TOOLS_CLIENT_HELM) template shoggoth-lint $(SHOGGOTH_CHART_ROOT_CLIENT) \
		--namespace lint \
		$(SHOGGOTH_HELM_VALUES) \
		> $@.tmp
	mv $@.tmp $@

${LINT_COLLISION_A}: ${CHART_SOURCES} $(SHOGGOTH_CONFIG) | ${LINT_TMP_DIR}
	$(TOOLS_CLIENT_HELM) template shoggoth-collision-a $(SHOGGOTH_CHART_ROOT_CLIENT) \
		--namespace collision-a \
		$(SHOGGOTH_HELM_VALUES) \
		> $@.tmp
	mv $@.tmp $@

${LINT_COLLISION_B}: ${CHART_SOURCES} $(SHOGGOTH_CONFIG) | ${LINT_TMP_DIR}
	$(TOOLS_CLIENT_HELM) template shoggoth-collision-b $(SHOGGOTH_CHART_ROOT_CLIENT) \
		--namespace collision-b \
		$(SHOGGOTH_HELM_VALUES) \
		> $@.tmp
	mv $@.tmp $@

# The gadget-disabled render. helm_lint_inspektor needs it. It is the same
# render the gates loop produces for the `gadget` key — same release name,
# same namespace, same values, same --set — and it is rendered a second
# time rather than shared, because the gate renders are files written
# inside a recipe and nothing can declare a prerequisite on them. The two
# ways to share one both cost more than the duplicate render: a recursive
# make per key re-parses the makefiles, and the parse-time derivations run
# a container each, and depending on the gates target would fail this
# play's CEL proof whenever an unrelated toggle regresses.
${LINT_INSPEKTOR_OFF}: ${CHART_SOURCES} $(SHOGGOTH_CONFIG) | ${LINT_TMP_DIR}
	$(TOOLS_CLIENT_HELM) template shoggoth-lint $(SHOGGOTH_CHART_ROOT_CLIENT) \
		--namespace lint \
		$(SHOGGOTH_HELM_VALUES) \
		--set services.gadget.enabled=false \
		> $@.tmp
	mv $@.tmp $@

helm_lint_render: ${LINT_RENDER}
	wc -l < ${LINT_RENDER}

# The whole chart, checked against 63 built-in rules, four CEL checks of our
# own and the Kubernetes JSON schemas. One binary, one render: the
# duplicate-key guard, the roleRef resolver and both halves of the gadget
# scope check are CEL checks in make/kube-linter.yaml, where the reason each
# one exists is a comment rather than a pipeline.
#
# WHY A FILE AND NOT THE CHART DIRECTORY. kube-linter renders Helm charts
# in-process, and it hardcodes ReleaseOptions{Name: "test-release",
# Namespace: "default"} with no values file — so `kube-linter lint
# $(SHOGGOTH_CHART_ROOT)` would lint a chart rendered with none of this
# instance's settings, and every service gate would render at its default.
# The render is done by helm here, where the values are the real ones, and
# the FILE is what kube-linter reads: every document lands in one lint
# context, which is what makes the cross-object CEL checks mean anything.
#
# A directory would be wrong even if the values problem did not exist.
# kube-linter merges every .yaml under a directory into one context, so
# pointing it at ${LINT_TMP_DIR} would fold the collision and gate renders
# into this one and the checks would see a cluster that does not exist.
#
# Duplicate mapping keys are caught here too, by the schema check with
# strict: true — kubeconform switches to yaml.UnmarshalStrict for the whole
# document, which is what reports them. The class it covers is the
# duplicate-label-key bug in five templates.
#
# Twelve of kube-linter's default-on checks are excluded in the config, each
# with the design fact that explains it. They are the checks written for a
# generic multi-tenant cluster — no read-only root fs, no NET_RAW drop, no
# host mounts, no host network — which fire on every third-party image this
# chart runs and on the two host-level daemons it exists to deploy. A gate
# that reports a hundred deliberate choices is a gate nobody reads.
#
# `--fail-if-no-objects-found` is not passed: the render is asserted
# non-empty by `wc -l` in helm_lint_render, and a zero there would be a
# render failure the helm template above already exited on.
helm_lint_kube_linter: ${LINT_RENDER}
	$(KUBE_LINTER_RUN) $(KUBE_LINTER_RENDER) --config $(KUBE_LINTER_CFG)

# A union gate spanning a service's own workloads makes `make stop
# SERVICE=<x>` a silent no-op: `or (x) (y)` still renders while y is on,
# so helm reports a successful upgrade and nothing is deleted — openbao's
# Service, PVC and StatefulSet under `or(openbao, openbao-bootstrap)` is
# the shape. This renders once per key with that key alone disabled and
# checks that the resources it owns are gone, which catches the class
# rather than one instance.
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
	$(TOOLS_CLIENT_YQ) -r 'select(.kind != null) | .kind + "/" + .metadata.name' ${LINT_RENDER} \
		| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/gates-base.ids
	$(TOOLS_CLIENT_YQ) -r '.services | keys | .[]' $(SHOGGOTH_CHART_ROOT_CLIENT)/values.yaml \
		| sort > ${LINT_TMP_DIR}/gates-keys.txt
	grep -c '' ${LINT_TMP_DIR}/gates-keys.txt
	for k in $$(cat ${LINT_TMP_DIR}/gates-keys.txt); do \
		$(TOOLS_CLIENT_HELM) template shoggoth-lint $(SHOGGOTH_CHART_ROOT_CLIENT) \
			--namespace lint \
			$(SHOGGOTH_HELM_VALUES) \
			--set services.$$k.enabled=false \
			> ${LINT_TMP_DIR}/gate-$$k.yaml; \
		printf '%s %s\n' "$$k" "$$(wc -l < ${LINT_TMP_DIR}/gate-$$k.yaml)"; \
	done > ${LINT_TMP_DIR}/gates-sizes.txt
	sed -n '/ 0$$/p' ${LINT_TMP_DIR}/gates-sizes.txt > ${LINT_TMP_DIR}/gates-empty.txt
	diff /dev/null ${LINT_TMP_DIR}/gates-empty.txt
	for k in $$(cat ${LINT_TMP_DIR}/gates-keys.txt); do \
		$(TOOLS_CLIENT_YQ) -r 'select(.kind != null) | .kind + "/" + .metadata.name' \
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
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "ClusterRole" or .kind == "ClusterRoleBinding") | .kind + "/" + .metadata.name' \
		${LINT_COLLISION_A} | sed '/^---$$/d' | sort -u \
		> ${LINT_TMP_DIR}/collision-a.names
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "ClusterRole" or .kind == "ClusterRoleBinding") | .kind + "/" + .metadata.name' \
		${LINT_COLLISION_B} | sed '/^---$$/d' | sort -u \
		> ${LINT_TMP_DIR}/collision-b.names
	comm -12 ${LINT_TMP_DIR}/collision-a.names ${LINT_TMP_DIR}/collision-b.names \
		> ${LINT_TMP_DIR}/collision-shared.names
	diff /dev/null ${LINT_TMP_DIR}/collision-shared.names

# Six ConfigMaps carry a whole YAML document inside a block scalar: the
# collector pipeline, the gadget daemon's own config, and four service
# templates. Nothing parses those blocks until the process that reads them
# starts and exits, and a broken block scalar is otherwise invisible -- the
# ConfigMap is valid YAML whatever its data values are, so the render and
# the schema validation stay green while the collector or the daemon
# crash-loops on a config parse error. Every ConfigMap that has a
# `config.yaml` key is extracted and parsed here, so a new one is covered
# the moment it exists. The config enables key-duplicates and NOTHING else:
# `extends: default` would drag in every default rule, so an unrelated
# `colons` violation (too many spaces after a colon) exits non-zero and
# would be reported here as a duplicate-key regression.
#
# The failures are collected rather than propagated: a `for` loop's exit
# status is the last command's, so a broken block in the middle of the list
# would be masked by a clean one after it. Each failing name is appended and
# the collection is asserted empty, and yamllint's own report is printed
# before the assertion so the offending line is visible. yamllint runs from
# the client image like the yq beside it, so the target asks nothing of the
# host but docker.
helm_lint_embedded: ${LINT_RENDER}
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "ConfigMap") | .metadata.name + " " + ((.data // {}) | keys | .[])' ${LINT_RENDER} \
		| sed '/^---$$/d' | awk '$$2 == "config.yaml" { print $$1 }' | sort -u \
		> ${LINT_TMP_DIR}/embedded.names
	grep -c '' ${LINT_TMP_DIR}/embedded.names
	cp /dev/null ${LINT_TMP_DIR}/embedded.failed
	cp /dev/null ${LINT_TMP_DIR}/embedded.log
	for n in $$(cat ${LINT_TMP_DIR}/embedded.names); do \
		$(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_REPO) -e CM_NAME="$$n" $(TOOLS_IMAGE_CLIENT) \
			yq -r 'select(.kind == "ConfigMap" and .metadata.name == strenv(CM_NAME)) | .data["config.yaml"]' ${LINT_RENDER} \
			> ${LINT_TMP_DIR}/embedded-$$n.yaml; \
		$(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_REPO) $(TOOLS_IMAGE_CLIENT) \
			yamllint -d '{rules: {key-duplicates: enable}}' ${LINT_TMP_DIR}/embedded-$$n.yaml \
			>> ${LINT_TMP_DIR}/embedded.log 2>&1 \
			|| echo "$$n" >> ${LINT_TMP_DIR}/embedded.failed; \
	done
	cat ${LINT_TMP_DIR}/embedded.log
	diff /dev/null ${LINT_TMP_DIR}/embedded.failed

# The three NetworkPolicies' ingress allowlists duplicate pod labels by hand,
# and nothing pinned the two sets together: a pod-label rename leaves every
# policy admitting pods that no longer exist, and a deleted `podSelector`
# entry silently stops admitting a consumer -- both with every helm_lint_*
# target green. The reverse direction compares the admitted set against the
# expected table below, which is what catches a removal, and it is the half
# that stays here.
#
# The FORWARD direction -- every admitted `app` label and every policy's own
# target resolved against the pod templates of the same render -- is
# kube-linter's dangling-networkpolicy and
# dangling-networkpolicypeer-podselector, both included via checks.include in
# make/kube-linter.yaml. They are built-in checks that resolve a
# podSelector against the pods of the same render, which is exactly the
# forward half, and running them costs nothing extra: helm_lint_kube_linter
# already passes this render to kube-linter.
#
# What they do NOT do is compare against a table of what SHOULD be admitted,
# so the reverse direction stays: a policy that admits nobody is not a
# dangling peer, and a policy that admits the wrong pods is not one either.
# The expected table is the only thing in the repo that records the
# intended graph, so a removal from it fails here even though the
# remaining entries all resolve.
#
# A service whose policy is gated off renders no policy at all, so its row
# simply does not appear and is not compared -- the same policy applies to
# the forward direction, where a gated-off policy is not a dangling peer
# because its target pods are gone by the gate that removed it. That is why
# neither check runs against the gate renders.
NETPOL_INGRESS_EXPECTED = victoria-metrics:otelcol,tempo,grafana,web-internal loki:otelcol,grafana tempo:otelcol,grafana

helm_lint_netpol: ${LINT_RENDER}
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "NetworkPolicy") | .spec.podSelector.matchLabels.app as $$t | .spec.ingress[] | .from[] | .podSelector.matchLabels.app | $$t + " " + .' ${LINT_RENDER} \
		| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/netpol-admits.txt
	grep -c '' ${LINT_TMP_DIR}/netpol-admits.txt
	for e in $(NETPOL_INGRESS_EXPECTED); do \
		t=$${e%%:*}; s=$${e#*:}; \
		for x in $$(echo $$s | tr ',' ' '); do echo "$$t $$x"; done; \
	done | sort -u > ${LINT_TMP_DIR}/netpol-expected.txt
	diff ${LINT_TMP_DIR}/netpol-expected.txt ${LINT_TMP_DIR}/netpol-admits.txt

# The otelcol DaemonSet's pod template has to reference the config it mounts:
# the collector pipeline is defined only in otelcol-config.yaml, and
# reloader.yaml passes --resources-to-ignore=configmaps, so without the
# checksum annotation a ConfigMap edit rolls nothing and the previous
# pipeline keeps running until the DaemonSet is deleted by hand. The
# annotation is what makes the edit visible, and this is its regression
# guard -- it renders twice with a value that changes only that ConfigMap
# and asserts the pod template is no longer byte-identical. Removing the
# annotation fails here.
helm_lint_rollout: ${LINT_RENDER}
	$(TOOLS_CLIENT_HELM) template shoggoth-lint $(SHOGGOTH_CHART_ROOT_CLIENT) \
		--namespace lint \
		$(SHOGGOTH_HELM_VALUES) \
		--set gadget.metricsPort=2424 \
		> ${LINT_TMP_DIR}/rollout-alt.yaml.tmp
	mv ${LINT_TMP_DIR}/rollout-alt.yaml.tmp ${LINT_TMP_DIR}/rollout-alt.yaml
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "DaemonSet" and .metadata.name == "otelcol") | .spec.template.metadata.annotations // {} | to_entries | .[] | .key + "=" + .value' ${LINT_RENDER} \
		| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/rollout-base.txt
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "DaemonSet" and .metadata.name == "otelcol") | .spec.template.metadata.annotations // {} | to_entries | .[] | .key + "=" + .value' ${LINT_TMP_DIR}/rollout-alt.yaml \
		| sed '/^---$$/d' | sort -u > ${LINT_TMP_DIR}/rollout-alt.txt
	grep -c '' ${LINT_TMP_DIR}/rollout-base.txt
	if diff ${LINT_TMP_DIR}/rollout-base.txt ${LINT_TMP_DIR}/rollout-alt.txt > ${LINT_TMP_DIR}/rollout-diff.txt; then \
		echo "otelcol pod template is byte-identical when otelcol-config changes"; \
		exit 1; \
	fi
	wc -l < ${LINT_TMP_DIR}/rollout-diff.txt

# The inspektor scrape job targets gadget.<ns>.svc.cluster.local, a Service
# that exists only while services.gadget.enabled is true. helm_lint_gates
# cannot see an ungated job: it compares only the kind/name ids of each
# key's own resources, and otelcol-config renders byte-identically in both
# renders. So the job's guard is a CEL check (inspektor-job-gated in
# make/kube-linter.yaml), and this target is the part CEL cannot do for
# itself: prove the check is not vacuous.
#
# A check that returns "" on a render where the job is correctly absent is
# indistinguishable from a check that never ran -- which is the whole
# difficulty with this one, because the pass case and the broken case look
# identical to kube-linter. Two greps close it: the job is asserted PRESENT
# in the full render, and otelcol-config is asserted PRESENT in the
# gadget-off one, so the check had both a job to find and a render to look
# for it in. Rename the Service back, or un-gate the job, and the first
# grep still passes while kube-linter reports the violation.
#
# --do-not-auto-add-defaults is what makes this a gate rather than a
# second full lint: only the custom checks and the two netpol checks run.
# The built-ins would otherwise fire on a partial cluster -- dangling-service
# alone flags every Service whose pods the toggle removed -- and none of
# that is a regression. Custom checks are always enabled regardless of the
# flag, which is what keeps this from silently becoming a no-op: if the
# config ever lost the inspektor-job-gated entry, kube-linter would exit 0
# with nothing checked, and the two greps above would still pass.
helm_lint_inspektor: ${LINT_RENDER} ${LINT_INSPEKTOR_OFF}
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "ConfigMap" and .metadata.name == "otelcol-config") | .data["config.yaml"]' ${LINT_RENDER} \
		| grep 'job_name: inspektor' > ${LINT_TMP_DIR}/inspektor-on.txt
	grep -c '' ${LINT_TMP_DIR}/inspektor-on.txt
	$(TOOLS_CLIENT_YQ) -r 'select(.kind == "ConfigMap" and .metadata.name == "otelcol-config") | .data["config.yaml"]' ${LINT_INSPEKTOR_OFF} \
		> ${LINT_TMP_DIR}/inspektor-off.conf
	grep -c '' ${LINT_TMP_DIR}/inspektor-off.conf
	$(KUBE_LINTER_RUN) $(KUBE_LINTER_OFF) \
		--config $(KUBE_LINTER_CFG) \
		--do-not-auto-add-defaults

# ---------------------------------------------------------------------------
# ansible/ offline validation
# ---------------------------------------------------------------------------

.PHONY: ansible_lint ansible_check

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
# `$(wildcard ansible/*.yml)` misses roles/host_state/tasks/main.yml
# entirely: deploy.yml reaches the role with `include_role`, which is the
# DYNAMIC form — the role is loaded when that task executes, not when the
# playbook is parsed, so `--syntax-check ansible/deploy.yml` never reads a
# line of it. The wrapper loop in ansible_lint closes that hole,
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
# that touches the ansible/ tree at all: no CI job covers it (see
# ansible/README.md), so whatever is not checked locally is not checked.
#
# ansible-playbook runs in the client image: the one prerequisite is docker,
# and a missing image fails with docker's own error rather than passing
# silently.
#
# The config is carried by $(TOOLS_CLIENT_ANSIBLE) as `-e ANSIBLE_CONFIG`,
# and an `-e` rather than a `VAR=x cmd` prefix, because a prefix would set
# the variable for the docker CLI and not for ansible inside the container.
# It has to be set at all — the Makefile's ANSIBLE_CONFIG note gives the
# search order ansible falls back through without it; unset, the check loses
# `inventory = hosts.ini` and runs against whatever /etc/ansible/hosts
# happens to hold — a syntax check that passes because it parsed no
# inventory at all is worse than one that fails. The Makefile's
# $(ANSIBLE) wrapper is not reused here: this loop runs a bare
# `ansible-playbook`, and that variable ends in the `ansible` directory a
# playbook path is appended to. Nor is $(TOOLS_CLIENT_ANSIBLE): this is an
# offline check, so it takes $(TOOLS_CLIENT_ANSIBLE_OFF), the same prefix
# without the live sites' identity mounts — a syntax check must not open
# an ssh connection.
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
# HOST_IP is passed to the container with `-e` rather than as an ansible
# extra var: the expression reads it with `lookup('env', ...)`, and an extra
# var would be a second spelling of the same value that could disagree with
# the environment the real run sees. $(TOOLS_CLIENT_ANSIBLE_OFF) carries both
# it and ANSIBLE_CONFIG.
#
# The success echo rides on the command with `&&` rather than following it:
# it is the recipe's last command, so without the `&&` a failed export would
# still exit zero and report success. A failing `ansible-inventory` prints
# the expression it could not resolve and exits non-zero, so there is no
# `||` arm.

# Order-only on the scratch directory: the wrapper playbook below is written
# into it, and it must exist before the recipe runs rather than being a
# reason for the recipe to re-run.
ansible_lint: | ${LINT_TMP_DIR}
	for play in $(ANSIBLE_PLAYBOOKS); do \
		echo "syntax-check $$play"; \
		$(TOOLS_CLIENT_ANSIBLE_OFF) ansible-playbook --syntax-check $$play || exit 1; \
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
		$(TOOLS_CLIENT_ANSIBLE_OFF) \
			-e ANSIBLE_INVENTORY=ansible/hosts.ini \
			-e ANSIBLE_ROLES_PATH=ansible/roles \
			ansible-playbook --syntax-check ${LINT_TMP_DIR}/roles.yml || exit 1; \
	fi; \
	$(TOOLS_CLIENT_ANSIBLE_OFF) ansible-inventory --list --export >/dev/null && \
		echo "ansible_lint: the inventory and every group_vars expression resolve"

# ---------------------------------------------------------------------------
# ansible/ dry run
# ---------------------------------------------------------------------------

# `ansible-playbook --check --diff` over every playbook the make targets
# invoke, in the same shape they invoke them.
#
# WHAT IT BUYS, and it is less than the name suggests: check mode SKIPS
# every `ansible.builtin.command` task, and the plays carry the whole deploy
# through command tasks (`kubectl apply`, `kubectl wait`, `helm upgrade`,
# `helm status`). What is left is what this target is for:
#
#   * every `assert` runs — `service.yml`'s service-name check and
#     `drop_image_tag.yml`'s usage assert among them — so a bad value fails
#     here rather than at the host;
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
# has to resolve and the key for it has to be reachable — the
# `tunnel_up` prerequisite provisions the agent and opens the forward,
# starting make's own agent when the shell has none, exactly as it does
# for `make up`, so a host that does not answer fails at the tunnel's
# readyz poll rather than part-way through a play. The
# become password is asked once per escalated play — one collection per
# `ansible-playbook` process, and the `service.yml` runs escalate nothing —
# because check mode still runs the host_state role's `file` and `copy` tasks
# as root to report what they would change. A NOPASSWD host answers with an
# empty line (see the Makefile).
#
# Every task in these plays runs inside the client image, the delegated
# `command` tasks and `synchronize` along with ansible itself, which is why
# that image carries kubectl, helm and rsync — see the Dockerfile's client
# stage.
#
# Two of the plays need a value the real targets take from the command line.
# `drop_image_tag.yml` gets literals, and they are never used for a request:
# `uri` has no check-mode support, so its DELETE is SKIPPED and nothing is
# sent — a check that dropped the tag would not be a check. The literals
# exist only to get past the play's own usage assert and reach the
# `include_vars` and the URL construction behind it.
# `clear_endpoints.yml` gets one documentation address (TEST-NET-1) for the
# same reason and under the same guarantee: its write is gated on
# `not ansible_check_mode`, so no tombstone is written and k3s is never
# stopped.
#
# `service.yml` takes none: every key the chart's values.yaml defines is
# checked, in both actions, with the keys read at recipe time — one container
# when the target runs, rather than a parse-time derivation every make
# process pays for. Check mode skips the `helm` module, so none of the runs
# reaches a cluster.
#
# `purge.yml` is covered only when INSTANCE is not the primary instance: the
# play's own assert refuses `shoggoth` outright, so running it against the
# default INSTANCE would fail on that guard rather than on anything this
# target is looking for.
#
# `host.yml` is skipped for the same shape of reason: its import gate refuses
# a host whose `/etc/nixos/configuration.nix` imports nothing yet — the state
# the play's own first pass leaves — so the run would stop on that guard
# rather than on anything this target is looking for. The gate's grep is what
# the play's first pass against the host exercises.
ansible_check: tunnel_up
	services=$$($(TOOLS_CLIENT_YQ) -r '.services | keys | .[]' $(SHOGGOTH_CHART_ROOT_CLIENT)/values.yaml); \
	if [ -z "$$services" ]; then \
		echo "ansible_check: .services in"; \
		echo "  values.yaml is empty, or the container that reads it did not"; \
		echo "  run."; \
		exit 1; \
	fi; \
	ansible_check_run() { \
		echo "--- check $$1 ($$2)"; \
		$(ANSIBLE)/$$1 --check --diff $$3 || exit 1; \
	}; \
	ansible_check_run deploy.yml "make up" "--ask-become-pass"; \
	ansible_check_run down.yml "make down"; \
	if [ "$(INSTANCE)" = "shoggoth" ]; then \
		echo "--- check purge.yml (make purge) — skipped: the play refuses the primary instance"; \
	else \
		ansible_check_run purge.yml "make purge" "--ask-become-pass"; \
	fi; \
	for s in $$services; do \
		for a in enable disable; do \
			ansible_check_run service.yml "action=$$a service=$$s" \
				"-e shoggoth_service=$$s -e shoggoth_service_action=$$a"; \
		done; \
	done; \
	ansible_check_run drop_image_tag.yml "make drop_image_tag" \
		"-e shoggoth_image=lint/not-a-real-image -e shoggoth_tag=check"; \
	ansible_check_run clear_endpoints.yml "make k3s_clear_endpoints" \
		"-e shoggoth_k3s_stale_ips=192.0.2.111 --ask-become-pass"; \
	echo "--- check host.yml (make host_install_nixos) — skipped: the play's import gate refuses a host whose configuration.nix imports nothing yet"
