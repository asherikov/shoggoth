# The flags shared by every helm command that reads the chart: the release
# namespace and the per-instance configuration file.
SHOGGOTH_HELM_COMMON_ARGS = --namespace $(INSTANCE) --values $(SHOGGOTH_CONFIG)

DIAGRAMS_RESOURCES=$(shell find ${HOME} ${PIPX_HOME} ${PIPX_GLOBAL_HOME} -ipath "*/resources/alibabacloud" | head -1 | xargs -I {} dirname {})
SKILL_DIR?=shoggoth/ai/plugin/skills/

graph:
	rm -f docs/*.svg docs/*.gv docs/*.png
	hiearch -f svg:cairo -r ${DIAGRAMS_RESOURCES} --temp-dir .tmp/ -o docs docs/*.yaml

# clusterviz — render the K8s manifests as a set of static cluster
# diagrams (separate from `graph` above, which renders the
# `docs/arch_*.yaml` source data into the hand-crafted
# `architecture.svg` / `bringup.svg` via hiearch).
#
# The full cluster is large enough that one all-in-one diagram is
# unreadable, so the cluster is sliced into 5 focused views by
# resource kind. `make clusterviz` regenerates all of them at once;
# invoke any one of the per-view targets directly to regenerate just
# that view. Each view's input is filtered at parse time with `grep
# -lE '^kind: ...$$'` against the manifest list, so an empty result
# (e.g. no CronJobs in this cluster) just means that view is a no-op.
#
# Available views (each is a separate make target):
#   clusterviz              — meta-target: rebuild all five subset
#                              views in one go. No all-in-one diagram
#                              is produced; the subsets are the views.
#   clusterviz-rbac         — Role / RoleBinding / ClusterRole /
#                              ClusterRoleBinding / ServiceAccount
#                              (100 resources in the current cluster)
#   clusterviz-workloads    — Deployment / StatefulSet / DaemonSet / Job
#                              (35 resources)
#   clusterviz-storage      — PersistentVolumeClaim / ConfigMap / Secret
#                              (62 resources)
#   clusterviz-networking   — Service (32 resources)
#   clusterviz-secrets      — ExternalSecret / SecretStore / PushSecret
#                              (24 resources; the External Secrets
#                              Operator sub-stack)
#
# Each output is a separate SVG under docs/:
#   docs/cluster-rbac.svg
#   docs/cluster-workloads.svg
#   docs/cluster-storage.svg
#   docs/cluster-networking.svg
#   docs/cluster-secrets.svg
#
# Pipeline (shared across all views via the clusterviz_run function):
#   1. kube-diagrams emits DOT with the bundled default layout (LR).
#   2. graphviz re-renders with -Grankdir=TB, switching the layout
#      from the default left-to-right (which produces 16000+pt wide
#      SVGs that don't fit any browser viewport) to top-to-bottom.
#   3. The renderer is `-Tsvg:cairo` rather than the default `-Tsvg`.
#      Cairo's renderer converts every glyph to a vector path before
#      writing the SVG, so the output is font-independent (the viewer
#      doesn't need the same font installed to render labels
#      correctly) and is generally a touch more compact than the
#      built-in SVG renderer. Downside: text is no longer selectable
#      as text — it lives as `<path>` data — so search/copy/paste of
#      node names in the rendered SVG no longer works.
#
# Icons: KubeDiagrams emits <image xlink:href="https://raw.githubusercontent.com/...">
# references for every node, and the `svg:cairo` renderer passes them
# through unchanged. The SVG therefore requires network access to
# raw.githubusercontent.com to render icons. In offline / air-gapped
# viewers and on GitHub's own SVG sanitizer (PR diffs, raw blob view)
# the icons will be missing. This is an accepted trade-off for the
# simpler pipeline.
#
# Requires on $PATH: helm and yq, for the render and the kind filter. Both
# are host prerequisites the Makefile uses everywhere else.
#
# kube-diagrams and graphviz `dot` run from the client image (`--target
# client` in shoggoth/dockerfiles/tools), which carries KubeDiagrams and the
# graphviz package with the cairo/pango plugins `-Tsvg:cairo` needs. docker is
# the only prerequisite for this half.
#
# Known limitation: KubeDiagrams does not resolve ${SHOGGOTH_NAMESPACE}
# placeholders in the input manifests, so node.namespace fields render
# as the literal string "${SHOGGOTH_NAMESPACE}" in the diagram. The chart
# renders through `helm template` with a real namespace, so the rendered
# stream carries no unresolved placeholders and the limitation stays latent.
#
# Does NOT touch docs/architecture.svg, docs/bringup.svg, etc. — the
# `graph` target above is the one that regenerates the architecture
# diagrams; these targets are additive.

DOCS_CLUSTER_DIAGRAM_RBAC     := docs/cluster-rbac.svg
DOCS_CLUSTER_DIAGRAM_WORKLOADS:= docs/cluster-workloads.svg
DOCS_CLUSTER_DIAGRAM_STORAGE  := docs/cluster-storage.svg
DOCS_CLUSTER_DIAGRAM_NETWORKING := docs/cluster-networking.svg
DOCS_CLUSTER_DIAGRAM_SECRETS  := docs/cluster-secrets.svg

empty :=
space := $(empty) $(empty)

CLUSTERVIZ_CONFIG := make/kube-diagrams.yaml

# Diagram inputs are the chart, rendered and filtered in one pipeline:
#
#     helm template | yq 'select(kind in ...)' | kube-diagrams -
#
# kube-diagrams reads stdin when the filename is `-` (that is how its own README
# pipes `helmfile template` and `kubectl kustomize` into it, both of which emit
# multi-document YAML), and yq emits exactly such a stream. So there is no
# intermediate file, no per-document split, and no script.
#
# The chart renders as a single multi-document stream rather than one
# resource per file, so the selection has to happen on the stream.
#
# $1 = output name (e.g. "cluster-rbac" for docs/cluster-rbac.svg)
# $2 = space-separated set of kinds for that view
#
# helm and yq run on the host: they are host prerequisites the Makefile
# itself uses everywhere else, and the render has to happen where the chart
# directory is. kube-diagrams and dot run together in ONE container — they are
# two halves of one step, and the .dot between them is an intermediate only
# the pair shares — and the stream reaches kube-diagrams through the pipe, so
# the container is started with `-i`. No `-t`: stdin is a pipe, and a pty
# would only mangle it.
#
# The paths stay repo-relative and are resolved by `-w` inside the container,
# so the host and the container agree on where .tmp/clusterviz and docs are
# without either side rewriting them.
define clusterviz_run
	set -e; mkdir -p .tmp/clusterviz; \
	KIND_RE='^($(subst $(space),|,$(2)))$$'; \
	N=$$(helm template shoggoth-clusterviz $(SHOGGOTH_CHART_ROOT) $(SHOGGOTH_HELM_COMMON_ARGS) \
		| yq ea -r "select(.kind != null and (.kind | test(\"$$KIND_RE\"))) | .kind" \
		| grep -v '^---$$' | wc -l); \
	if [ "$$N" -eq 0 ]; then \
		echo "FAIL: view '$1' matched no resources — the render produced nothing for kinds: $(2)"; \
		exit 1; \
	fi; \
	echo "  $$N manifests for view '$1'"; \
	helm template shoggoth-clusterviz $(SHOGGOTH_CHART_ROOT) $(SHOGGOTH_HELM_COMMON_ARGS) \
		| yq ea "select(.kind != null and (.kind | test(\"$$KIND_RE\")))" \
		| $(TOOLS_CLIENT_RUN) -i $(TOOLS_CLIENT_REPO) $(TOOLS_IMAGE_CLIENT) \
			sh -c 'kube-diagrams -c ${CLUSTERVIZ_CONFIG} -o .tmp/clusterviz/$1.dot -f dot - \
				&& dot -Grankdir=TB -Gsplines=spline -Tsvg:cairo \
					.tmp/clusterviz/$1.dot -o docs/$1.svg' \
		|| { echo "FAIL: kube-diagrams or dot failed for '$1'"; exit 1; }
endef

.PHONY: clusterviz clusterviz-rbac clusterviz-workloads clusterviz-storage clusterviz-networking clusterviz-secrets
# Meta-target: rebuild all five subset views. The full cluster is too
# large for one diagram, so the all-in-one view is intentionally not
# produced — the subsets are the views.
clusterviz: clusterviz-rbac clusterviz-workloads clusterviz-storage clusterviz-networking clusterviz-secrets

.PHONY: clusterviz-rbac
clusterviz-rbac: ${DOCS_CLUSTER_DIAGRAM_RBAC}
${DOCS_CLUSTER_DIAGRAM_RBAC}: $(SHOGGOTH_CONFIG) $(SHOGGOTH_CHART_ROOT)/templates/*.yaml ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-rbac,ClusterRole ClusterRoleBinding Role RoleBinding ServiceAccount)

.PHONY: clusterviz-workloads
clusterviz-workloads: ${DOCS_CLUSTER_DIAGRAM_WORKLOADS}
${DOCS_CLUSTER_DIAGRAM_WORKLOADS}: $(SHOGGOTH_CONFIG) $(SHOGGOTH_CHART_ROOT)/templates/*.yaml ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-workloads,Deployment StatefulSet DaemonSet Job)

.PHONY: clusterviz-storage
clusterviz-storage: ${DOCS_CLUSTER_DIAGRAM_STORAGE}
${DOCS_CLUSTER_DIAGRAM_STORAGE}: $(SHOGGOTH_CONFIG) $(SHOGGOTH_CHART_ROOT)/templates/*.yaml ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-storage,PersistentVolumeClaim ConfigMap Secret)

.PHONY: clusterviz-networking
clusterviz-networking: ${DOCS_CLUSTER_DIAGRAM_NETWORKING}
${DOCS_CLUSTER_DIAGRAM_NETWORKING}: $(SHOGGOTH_CONFIG) $(SHOGGOTH_CHART_ROOT)/templates/*.yaml ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-networking,Service)

.PHONY: clusterviz-secrets
clusterviz-secrets: ${DOCS_CLUSTER_DIAGRAM_SECRETS}
${DOCS_CLUSTER_DIAGRAM_SECRETS}: $(SHOGGOTH_CONFIG) $(SHOGGOTH_CHART_ROOT)/templates/*.yaml ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-secrets,ExternalSecret SecretStore PushSecret)

fmt:
	cp README.md .tmp/README.md.back
	# copy everything starting from Introduction section (skip toc)
	sed '/^Introduction$$/,$$!d' .tmp/README.md.back > README.md
	pandoc --standalone --columns=80 --markdown-headings=setext --tab-stop=4 --to=gfm --toc --toc-depth=2 README.md -o README.fmt.md
	mv README.fmt.md README.md

spell:
	hunspell -H -p ./.hunspell_dict ./README.md

html: # based on https://github.com/jez/pandoc-markdown-css-theme
	pandoc README.md \
		--output gh-pages/index.html \
		--standalone \
		--table-of-contents \
		--toc-depth=3 \
		--number-sections \
		--to html5+smart \
		--embed-resources \
		--template=gh-pages/template.html5 \
		--css gh-pages/theme.css \
		--css gh-pages/skylighting-solarized-theme.css \
		--wrap=none \
		--variable=date:"DATE: `date '+%Y-%m-%d'`" \
		--variable=author:"VERSION: `git describe --broken --dirty --always`" \
        --metadata title="Code Incomplete"

ghpages:
	cd gh-pages; git checkout gh-pages
	${MAKE} html
	cd gh-pages; git add *; git commit -a -m "${GIT_MESSAGE}"; git push

ghpages_action:
	sudo apt update
	sudo ${APT_INSTALL} pandoc
	${MAKE} ghpages

yamlfmt:
	# https://github.com/google/yamlfmt/blob/main/docs/config-file.md#basic-formatter
	# indent=2 matches the rest of the repo's YAML (shoggoth/<instance>.yaml,
	# make/kube-diagrams.yaml, docs/arch_*.yaml); indent=4 re-indents even
	# Chart.yaml's list entries, which is a different style, not a fix.
	#
	# `chart/templates/` is deliberately NOT in this glob. Those files are Go
	# templates, not YAML: a bare `{{- /* ... */}}` header at column 0 opens a
	# flow mapping, so all 62 fail to parse and the target exits 2. To check
	# what the chart actually emits, run `make helm_lint`.
	yamlfmt -formatter indent=2,retain_line_breaks=true shoggoth/chart/*.yaml


get_skill:
	mkdir -p ${SKILL_DIR}/${NAME}
	curl -fsSL ${URL}/SKILL.md -o ${SKILL_DIR}/${NAME}/SKILL.md

skills:
	# https://code.claude.com/docs/en/plugin-marketplaces
	# https://deepwiki.com/QwenLM/qwen-code/9.4-creating-extensions
	# https://qwenlm.github.io/qwen-code-docs/en/users/extension/introduction
	${MAKE} get_skill NAME=caveman			URL=https://raw.githubusercontent.com/JuliusBrussee/caveman/main/skills/caveman/
	${MAKE} get_skill NAME=memory-notes 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-notes/
	${MAKE} get_skill NAME=memory-reflect 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-reflect/
	${MAKE} get_skill NAME=memory-schema 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-schema/
	${MAKE} get_skill NAME=memory-defrag 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-defrag/
	${MAKE} get_skill NAME=memory-lifecycle	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-lifecycle/
	${MAKE} get_skill NAME=memory-ingest	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-ingest/
	${MAKE} get_skill NAME=memory-metadata-search URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-metadata-search/


# update — bump every pinned version in this tree, in place, and stop there.

UPDATECLI_CONFIG?=make/updatecli.yaml

# The second config, for the versions pinned in the client image. It is a
# separate file because it reads from different sources and writes to a
# different file: shoggoth/dockerfiles/tools, the ARG block the `client`
# stage is built from. `make update` runs both, and neither writes a file the
# other touches, so the two runs are independent and unordered.
UPDATECLI_IMAGE_CONFIG?=make/updatecli-images.yaml

# Both configs, in the order they run. Override to run one half.
UPDATECLI_CONFIGS?=$(UPDATECLI_CONFIG) $(UPDATECLI_IMAGE_CONFIG)

# The two CRD submodules updatecli cannot reach. A submodule pointer is a gitlink
# (mode 160000) recorded in the superproject's index, not file content, so there is
# nothing on disk for a `file` target to match: updatecli bumps the tag in
# shoggoth/crd/versions.yaml instead, and the loop below checks the submodule out
# to it. Each name is both a key of that file and a directory under shoggoth/crd/.
CRD_SUBMODULES?=argo-workflows external-secrets

.PHONY: update

# updatecli runs from the client image, like kube-linter above: the version is
# pinned in the Dockerfile's ARGs. The mount is read-write because the whole
# point of the target is to rewrite the pinned versions in the tree.
update:
	mkdir -p .tmp
	set -e; for cfg in $(UPDATECLI_CONFIGS); do \
		$(TOOLS_CLIENT_RUN) $(TOOLS_CLIENT_REPO) \
			-e TMPDIR=$(TOOLS_CLIENT_WORKDIR)/.tmp \
			$(TOOLS_IMAGE_CLIENT) \
			updatecli pipeline apply \
			--config $(TOOLS_CLIENT_WORKDIR)/$$cfg \
			--commit=false \
			--push=false; \
	done
	set -e; for m in $(CRD_SUBMODULES); do \
		v=$$(yq ".$$m" shoggoth/crd/versions.yaml); \
		echo ">>>>>>>>>>>> shoggoth/crd/$$m -> $$v"; \
		git -C shoggoth/crd/$$m fetch --tags --quiet; \
		git -C shoggoth/crd/$$m checkout --quiet --detach "$$v"; \
	done
