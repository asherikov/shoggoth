DIAGRAMS_RESOURCES=$(shell find ${HOME} ${PIPX_HOME} ${PIPX_GLOBAL_HOME} -ipath "*/resources/alibabacloud" | head -1 | xargs -I {} dirname {})

graph:
	rm -f docs/*.svg docs/*.gv docs/*.png
	hiearch -f svg:cairo -r ${DIAGRAMS_RESOURCES} -o docs docs/*.yaml

fmt:
	cp README.md README.md.back
	# copy everything starting from Introduction section (skip toc)
	sed '/^Introduction$$/,$$!d' README.md.back > README.md
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
	yamlfmt -formatter indent=4,retain_line_breaks=true shoggoth/docker-compose.yml
