export GITEA_URL?=git.shoggoth.local
export GITEA_API=http://${GITEA_URL}/api/v1
#export GITEA_TOKEN?=<token> # use auth.mk
# issues not copied if true, see https://github.com/go-gitea/gitea/pull/20311 and https://forum.gitea.com/t/mirror-a-github-site-does-not-mirror-issues/8141
export GITEA_MIRROR?=true

export GITHUB_USER?=asherikov
export GITHUB_REPO?=ccws
#export GITHUB_TOKEN?=<token> # use auth.mk

export REPO_DESCRIPTION?=

SOURCES_DIR?=
GITEA_PROJECT?=


github_to_gitea_repo:
	echo "Copying ${GITHUB_USER}/${GITHUB_REPO}"
	curl -s \
		"${GITEA_API}/repos/migrate" \
		-H "accept: application/json" \
		-H "Authorization: token ${GITEA_TOKEN}" \
		-H "Content-Type: application/json" \
		-d "{ \
			\"auth_token\": \"${GITHUB_TOKEN}\", \
			\"clone_addr\": \"https://github.com/${GITHUB_USER}/${GITHUB_REPO}\", \
			\"description\": \"${REPO_DESCRIPTION}\", \
			\"issues\": true, \
			\"labels\": true, \
			\"milestones\": true, \
			\"mirror\": ${GITEA_MIRROR}, \
			\"private\": false, \
			\"pull_requests\": true, \
			\"releases\": true, \
			\"repo_name\": \"${GITHUB_REPO}\", \
			\"repo_owner\": \"${GITHUB_USER}\", \
			\"service\": \"git\", \
			\"wiki\": true \
			}" \
		-i

github_to_gitea_user:
	${MAKE} gitea_create_project GITEA_PROJECT=${GITHUB_USER}
	@page=1; \
	while true; do \
		headerfile=$$(mktemp); \
		curl -s --dump-header "$$headerfile" "https://api.github.com/users/${GITHUB_USER}/repos?page=$${page}&per_page=100" \
			| jq -r '.[] | "\(.name)\t\(.description // empty)"' | while IFS=$$'\t' read -r name desc; do \
			if [ -f shoggoth/private/github_to_gitea.blacklist ] && grep -qx "$$name" shoggoth/private/github_to_gitea.blacklist; then \
				echo "Skipping blacklisted repository: $$name"; \
			else \
				${MAKE} github_to_gitea_repo GITHUB_REPO="$$name" REPO_DESCRIPTION="$$desc"; \
			fi; \
		done; \
		link_header=$$(grep -i '^Link:' "$$headerfile"); \
		if ! echo "$$link_header" | grep -q 'rel="next"'; then rm -f "$$headerfile" && break; fi; \
		page=$$((page + 1)); \
		rm -f "$$headerfile"; \
	done

gitea_create_project:
	@echo "Creating Gitea project: ${GITEA_PROJECT}"
	curl -s -o /dev/null -w "%{http_code}" \
		"${GITEA_API}/orgs" \
		-H "accept: application/json" \
		-H "Authorization: token ${GITEA_TOKEN}" \
		-H "Content-Type: application/json" \
		-d "{ \
			\"username\": \"${GITEA_PROJECT}\", \
			\"full_name\": \"${GITEA_PROJECT}\" \
			}" \
		| grep -q "201\|422" && echo "Project ${GITEA_PROJECT} created or already exists" || \
		(echo "Failed to create project ${GITEA_PROJECT}" && exit 1)

gitea_push_repos:
	@echo "Pushing repositories from ${SOURCES_DIR} to Gitea project ${GITEA_PROJECT}"
	find ${SOURCES_DIR} -mindepth 2 -maxdepth 2 -type d -name ".git" \
		| sed 's|/\.git$$||' \
		| xargs -I {} basename {} \
		| xargs -P ${JOBS} -I {} ${MAKE} gitea_push_repo REPO_NAME={} REPO_PATH=${SOURCES_DIR}/{} GITEA_PROJECT=${GITEA_PROJECT}

gitea_push_repo:
	@echo "Processing repository: ${REPO_NAME}"
	@echo "Checking if Gitea repository ${REPO_NAME} already exists"
	curl -s -o /dev/null \
		"${GITEA_API}/repos/${GITEA_PROJECT}/${REPO_NAME}" \
		-H "accept: application/json" \
		-H "Authorization: token ${GITEA_TOKEN}"
	@echo "Creating Gitea repository: ${REPO_NAME}"
	curl -s -X POST \
		"${GITEA_API}/orgs/${GITEA_PROJECT}/repos" \
		-H "accept: application/json" \
		-H "Authorization: token ${GITEA_TOKEN}" \
		-H "Content-Type: application/json" \
		-d "{ \
			\"name\": \"${REPO_NAME}\", \
			\"private\": false \
			}"
	# \"default_branch\": \"main\",
	@echo "Adding shoggoth remote to ${REPO_NAME}"
	cd "${REPO_PATH}" \
		&& (git remote remove shoggoth 2>/dev/null || true) \
		&& git remote add shoggoth "ssh://git@${GITEA_URL}:3022/${GITEA_PROJECT}/${REPO_NAME}.git" \
		&& git push --mirror shoggoth

gitea_import:
	@echo "Importing repositories from ${SOURCES_DIR} to Gitea project ${GITEA_PROJECT}"
	${MAKE} gitea_create_project
	${MAKE} gitea_push_repos

gitea_delete_repos:
	@echo "Deleting all repositories in Gitea project: ${GITEA_PROJECT}"
	@while true; do \
		repos=$$(curl -s \
			"${GITEA_API}/orgs/${GITEA_PROJECT}/repos?page=1&limit=50" \
			-H "accept: application/json" \
			-H "Authorization: token ${GITEA_TOKEN}" \
			| grep -o '"name":"[^"]*"' \
			| sed 's/"name":"//;s/"$$//'); \
		if [ -z "$${repos}" ]; then break; fi; \
		echo "$${repos}" | xargs -P ${JOBS} -I {} ${MAKE} gitea_delete_repo REPO_NAME={} GITEA_PROJECT=${GITEA_PROJECT}; \
	done

gitea_delete_repo:
	@echo "Deleting repository: ${GITEA_PROJECT}/${REPO_NAME}"
	@curl -s -X DELETE \
		"${GITEA_API}/repos/${GITEA_PROJECT}/${REPO_NAME}" \
		-H "accept: application/json" \
		-H "Authorization: token ${GITEA_TOKEN}"

gitea_delete_org:
	@echo "Deleting Gitea organization: ${GITEA_PROJECT}"
	@curl -s -X DELETE \
		"${GITEA_API}/orgs/${GITEA_PROJECT}" \
		-H "accept: application/json" \
		-H "Authorization: token ${GITEA_TOKEN}"

gitea_remove_project:
	@echo "Removing Gitea project ${GITEA_PROJECT} with all repositories"
	${MAKE} gitea_delete_repos
	${MAKE} gitea_delete_org
