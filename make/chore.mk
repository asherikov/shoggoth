DIAGRAMS_RESOURCES=$(shell find ${HOME} ${PIPX_HOME} ${PIPX_GLOBAL_HOME} -ipath "*/resources/alibabacloud" | head -1 | xargs -I {} dirname {})
SKILL_DIR?=shoggoth/ai/plugin/skills/

graph:
	rm -f docs/*.svg docs/*.gv docs/*.png
	hiearch -f svg:cairo -r ${DIAGRAMS_RESOURCES} --temp-dir .tmp/ -o docs docs/*.yaml

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
	${MAKE} get_skill NAME=redmine-cli 		URL=https://raw.githubusercontent.com/aarondpn/redmine-cli/main/skills/redmine-cli/
	${MAKE} get_skill NAME=kestra-flow 		URL=https://raw.githubusercontent.com/kestra-io/agent-skills/main/skills/kestra-flow/
	${MAKE} get_skill NAME=caveman			URL=https://raw.githubusercontent.com/JuliusBrussee/caveman/main/skills/caveman/
	${MAKE} get_skill NAME=memory-notes 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-notes/
	${MAKE} get_skill NAME=memory-reflect 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-reflect/
	${MAKE} get_skill NAME=memory-schema 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-schema/
	${MAKE} get_skill NAME=memory-defrag 	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-defrag/
	${MAKE} get_skill NAME=memory-lifecycle	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-lifecycle/
	${MAKE} get_skill NAME=memory-ingest	URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-ingest/
	${MAKE} get_skill NAME=memory-metadata-search URL=https://raw.githubusercontent.com/basicmachines-co/basic-memory-skills/refs/heads/main/memory-metadata-search/
