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
# Requires on $PATH:
#   kube-diagrams (https://github.com/philippemerle/KubeDiagrams):
#     pip install KubeDiagrams
#   graphviz `dot` binary (already on this system at /usr/bin/dot)
#
# Known limitation: KubeDiagrams does not resolve ${SHOGGOTH_NAMESPACE}
# placeholders in the input manifests, so node.namespace fields render
# as the literal string "${SHOGGOTH_NAMESPACE}" in the diagram. Pipe
# the manifests through `envsubst` first to get a namespace-resolved
# diagram (e.g. `envsubst < shoggoth/k3s/foo.yaml | kube-diagrams -`).
# Left as a follow-up since the current cluster uses a single
# namespace in practice.
#
# Does NOT touch docs/architecture.svg, docs/bringup.svg, etc. — the
# `graph` target above is the one that regenerates the architecture
# diagrams; these targets are additive.

DOCS_CLUSTER_DIAGRAM_RBAC     := docs/cluster-rbac.svg
DOCS_CLUSTER_DIAGRAM_WORKLOADS:= docs/cluster-workloads.svg
DOCS_CLUSTER_DIAGRAM_STORAGE  := docs/cluster-storage.svg
DOCS_CLUSTER_DIAGRAM_NETWORKING := docs/cluster-networking.svg
DOCS_CLUSTER_DIAGRAM_SECRETS  := docs/cluster-secrets.svg

CLUSTERVIZ_CONFIG := make/kube-diagrams.yaml

# Local copy of the manifest list. make/chore.mk is included
# alphabetically before make/k3s.mk (where K3S_ALL_MANIFESTS /
# K3S_ARGO_CRD_FILES are defined with `:=` immediate evaluation), so
# those variables are empty when this target is parsed. Duplicating
# the find here is cheaper than reordering the includes. If the
# source-of-truth paths in make/k3s.mk change, mirror them here.
CLUSTERVIZ_INPUTS := \
    $(shell find shoggoth/k3s -name '*.yaml' \
        ! -path '*/kube-diagrams.yaml' | sort) \
    $(shell find shoggoth/argo-workflows/manifests/base/crds/full \
        -maxdepth 1 -name 'argoproj.io_*.yaml' | sort)

# Subset filters — grep the manifests for the relevant `kind:` lines.
# Each `$(shell ...)` runs at parse time; an empty result just means
# the subset target is a no-op for that view (e.g. no CronJob in this
# cluster). Sort + uniq keeps the input list deterministic.
CLUSTERVIZ_INPUTS_RBAC := \
    $(shell grep -lE '^kind: (ClusterRole|ClusterRoleBinding|Role|RoleBinding|ServiceAccount)$$' \
        $(CLUSTERVIZ_INPUTS) 2>/dev/null | sort -u)
CLUSTERVIZ_INPUTS_WORKLOADS := \
    $(shell grep -lE '^kind: (Deployment|StatefulSet|DaemonSet|Job)$$' \
        $(CLUSTERVIZ_INPUTS) 2>/dev/null | sort -u)
CLUSTERVIZ_INPUTS_STORAGE := \
    $(shell grep -lE '^kind: (PersistentVolumeClaim|ConfigMap|Secret)$$' \
        $(CLUSTERVIZ_INPUTS) 2>/dev/null | sort -u)
CLUSTERVIZ_INPUTS_NETWORKING := \
    $(shell grep -lE '^kind: Service$$' \
        $(CLUSTERVIZ_INPUTS) 2>/dev/null | sort -u)
CLUSTERVIZ_INPUTS_SECRETS := \
    $(shell grep -lE '^kind: (ExternalSecret|SecretStore|PushSecret)$$' \
        $(CLUSTERVIZ_INPUTS) 2>/dev/null | sort -u)

# Render one view: $1 = output name (e.g. "cluster" for cluster.svg),
# $2 = space-separated input manifest list. The intermediate .dot
# file lives under .tmp/ which is already .gitignore'd.
define clusterviz_run
	@mkdir -p .tmp/clusterviz
	kube-diagrams -c ${CLUSTERVIZ_CONFIG} -o .tmp/clusterviz/$1.dot -f dot $2
	dot -Grankdir=TB -Gsplines=spline -Tsvg:cairo .tmp/clusterviz/$1.dot -o docs/$1.svg
endef

.PHONY: clusterviz clusterviz-rbac clusterviz-workloads clusterviz-storage clusterviz-networking clusterviz-secrets
# Meta-target: rebuild all five subset views. The full cluster is too
# large for one diagram, so the all-in-one view is intentionally not
# produced — the subsets are the views.
clusterviz: clusterviz-rbac clusterviz-workloads clusterviz-storage clusterviz-networking clusterviz-secrets

.PHONY: clusterviz-rbac
clusterviz-rbac: ${DOCS_CLUSTER_DIAGRAM_RBAC}
${DOCS_CLUSTER_DIAGRAM_RBAC}: ${CLUSTERVIZ_INPUTS_RBAC} ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-rbac,${CLUSTERVIZ_INPUTS_RBAC})

.PHONY: clusterviz-workloads
clusterviz-workloads: ${DOCS_CLUSTER_DIAGRAM_WORKLOADS}
${DOCS_CLUSTER_DIAGRAM_WORKLOADS}: ${CLUSTERVIZ_INPUTS_WORKLOADS} ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-workloads,${CLUSTERVIZ_INPUTS_WORKLOADS})

.PHONY: clusterviz-storage
clusterviz-storage: ${DOCS_CLUSTER_DIAGRAM_STORAGE}
${DOCS_CLUSTER_DIAGRAM_STORAGE}: ${CLUSTERVIZ_INPUTS_STORAGE} ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-storage,${CLUSTERVIZ_INPUTS_STORAGE})

.PHONY: clusterviz-networking
clusterviz-networking: ${DOCS_CLUSTER_DIAGRAM_NETWORKING}
${DOCS_CLUSTER_DIAGRAM_NETWORKING}: ${CLUSTERVIZ_INPUTS_NETWORKING} ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-networking,${CLUSTERVIZ_INPUTS_NETWORKING})

.PHONY: clusterviz-secrets
clusterviz-secrets: ${DOCS_CLUSTER_DIAGRAM_SECRETS}
${DOCS_CLUSTER_DIAGRAM_SECRETS}: ${CLUSTERVIZ_INPUTS_SECRETS} ${CLUSTERVIZ_CONFIG}
	$(call clusterviz_run,cluster-secrets,${CLUSTERVIZ_INPUTS_SECRETS})

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
	yamlfmt -formatter indent=4,retain_line_breaks=true shoggoth/k3s/*.yaml


get_skill:
	mkdir -p ${SKILL_DIR}/${NAME}
	curl -fsSL ${URL}/SKILL.md -o ${SKILL_DIR}/${NAME}/SKILL.md

skills:
	# https://code.claude.com/docs/en/plugin-marketplaces
	# https://deepwiki.com/QwenLM/qwen-code/9.4-creating-extensions
	# https://qwenlm.github.io/qwen-code-docs/en/users/extension/introduction
	${MAKE} get_skill NAME=kestra-flow 		URL=https://raw.githubusercontent.com/kestra-io/agent-skills/main/skills/kestra-flow/
	${MAKE} get_skill NAME=caveman			URL=https://raw.githubusercontent.com/JuliusBrussee/caveman/main/skills/caveman/
	${MAKE} get_skill NAME=memory-notes 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-notes/
	${MAKE} get_skill NAME=memory-reflect 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-reflect/
	${MAKE} get_skill NAME=memory-schema 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-schema/
	${MAKE} get_skill NAME=memory-defrag 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-defrag/
	${MAKE} get_skill NAME=memory-lifecycle	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-lifecycle/
	${MAKE} get_skill NAME=memory-ingest	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-ingest/
	${MAKE} get_skill NAME=memory-metadata-search URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-metadata-search/
