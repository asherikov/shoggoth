# make/lint.mk — offline chart validation.
#
# Split out of make/k3s.mk. `helm_lint` used to be a single target running
# five unrelated checks inside one long shell block, so a failure said which
# line tripped without saying which check it belonged to. Each check is its
# own target here and can be run on its own.
#
# No check wraps a command in `if`, `&&` or `||`, and none prints a verdict
# of its own. The exit status of the tool that did the work is the verdict
# and that tool's own output is the report: `diff` prints the names that
# differ, `join` prints the rows it could not pair, `yamllint` prints the
# duplicated keys, `helm` prints the template error. The command make
# echoes before running it is the whole diagnosis.
#
# The renders are file targets, so one render is shared by every check that
# reads it, and a chart edit invalidates them.
#
# `make/*.mk` is included alphabetically, so this file is parsed after
# make/k3s.mk — that is where K3S_TMP_DIR and SHOGGOTH_HELM_VALUES come
# from.

.PHONY: helm_lint helm_lint_chart helm_lint_render helm_lint_dupes \
        helm_lint_gates helm_lint_collision helm_lint_roledef

# Values for the offline renders. This is just the instance config: the
# checks render to a file and never apply anything, so there is nothing here
# for a throwaway override to protect. The release name and namespace are
# what distinguish lint output from a real install.
SHOGGOTH_HELM_VALUES_LINT = $(SHOGGOTH_HELM_VALUES)

# Where the renders live. LINT_TMP_DIR is a directory target, so it is
# created once and every render lands inside it.
LINT_TMP_DIR=${K3S_TMP_DIR}/lint
LINT_RENDER=${LINT_TMP_DIR}/render.yaml
LINT_COLLISION_A=${LINT_TMP_DIR}/collision-a.yaml
LINT_COLLISION_B=${LINT_TMP_DIR}/collision-b.yaml

# Everything a render depends on. A change to any of these re-renders.
CHART_SOURCES=$(SHOGGOTH_CHART_ROOT)/Chart.yaml \
	$(SHOGGOTH_CHART_ROOT)/values.yaml \
	$(wildcard $(SHOGGOTH_CHART_ROOT)/templates/*.yaml) \
	$(wildcard $(SHOGGOTH_CHART_ROOT)/templates/*.tpl)

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
# make crds_apply applies them from their upstream sources before Helm
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
