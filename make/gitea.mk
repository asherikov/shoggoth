export GITEA_URL?=git.s.local
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
			if [ -f private/github_to_gitea.blacklist ] && grep -qx "$$name" private/github_to_gitea.blacklist; then \
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
		&& git remote add shoggoth "ssh://git@${GITEA_URL}/${GITEA_PROJECT}/${REPO_NAME}.git" \
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

gitea_make_all_repos_public:
	@echo "Making all repositories public in all Gitea projects"
	@page=1; \
	while true; do \
		orgs=$$(curl -s \
			"${GITEA_API}/orgs?page=$${page}&limit=50" \
			-H "accept: application/json" \
			-H "Authorization: token ${GITEA_TOKEN}"); \
		if [ -z "$$orgs" ] || echo "$$orgs" | jq -e 'length == 0' >/dev/null; then break; fi; \
		echo "$$orgs" | jq -r '.[].username' | while read -r org; do \
			${MAKE} gitea_make_repos_public GITEA_PROJECT="$$org"; \
		done; \
		if echo "$$orgs" | jq 'length' | grep -qE "^50$$"; then \
			page=$$((page + 1)); \
		else \
			break; \
		fi; \
	done

gitea_make_repos_public:
	@echo "Making all repositories public in Gitea project ${GITEA_PROJECT}"
	@page=1; \
	while true; do \
		repos=$$(curl -s \
			"${GITEA_API}/orgs/${GITEA_PROJECT}/repos?page=$${page}&limit=50" \
			-H "accept: application/json" \
			-H "Authorization: token ${GITEA_TOKEN}"); \
		if [ -z "$$repos" ] || echo "$$repos" | jq -e 'length == 0' >/dev/null; then break; fi; \
		echo "$$repos" | jq -r '.[].name' | while read -r name; do \
			echo "Making ${GITEA_PROJECT}/$$name public"; \
			curl -sfS -X PATCH \
				"${GITEA_API}/repos/${GITEA_PROJECT}/$$name" \
				-H "accept: application/json" \
				-H "Authorization: token ${GITEA_TOKEN}" \
				-H "Content-Type: application/json" \
				-d '{"private": false}' > /dev/null; \
		done; \
		if echo "$$repos" | jq 'length' | grep -qE "^50$$"; then \
			page=$$((page + 1)); \
		else \
			break; \
		fi; \
	done

gitea_enable_actions:
	@echo "Enabling Actions for all repositories in Gitea project ${GITEA_PROJECT}"
	@page=1; \
	while true; do \
		repos=$$(curl -s \
			"${GITEA_API}/orgs/${GITEA_PROJECT}/repos?page=$${page}&limit=50" \
			-H "accept: application/json" \
			-H "Authorization: token ${GITEA_TOKEN}"); \
		if [ -z "$$repos" ] || echo "$$repos" | jq -e 'length == 0' >/dev/null; then break; fi; \
		echo "$$repos" | jq -r '.[].name' | while read -r name; do \
			echo "Enabling Actions for ${GITEA_PROJECT}/$$name"; \
			curl -sfS -X PATCH \
				"${GITEA_API}/repos/${GITEA_PROJECT}/$$name" \
				-H "accept: application/json" \
				-H "Authorization: token ${GITEA_TOKEN}" \
				-H "Content-Type: application/json" \
				-d '{"has_actions": true}' > /dev/null; \
		done; \
		if echo "$$repos" | jq 'length' | grep -qE "^50$$"; then \
			page=$$((page + 1)); \
		else \
			break; \
		fi; \
	done

gitea_unmirror_repos:
	@echo "Converting all mirror repositories to normal in Gitea project ${GITEA_PROJECT}"
	@page=1; \
	while true; do \
		repos=$$(curl -s \
			"${GITEA_API}/orgs/${GITEA_PROJECT}/repos?page=$${page}&limit=50" \
			-H "accept: application/json" \
			-H "Authorization: token ${GITEA_TOKEN}"); \
		if [ -z "$$repos" ] || echo "$$repos" | jq -e 'length == 0' >/dev/null; then break; fi; \
		echo "$$repos" | jq -r '.[] | select(.mirror == true) | .name' | while read -r name; do \
			${MAKE} gitea_unmirror_repo REPO_NAME="$$name" GITEA_PROJECT=${GITEA_PROJECT}; \
		done; \
		if echo "$$repos" | jq 'length' | grep -qE "^50$$"; then \
			page=$$((page + 1)); \
		else \
			break; \
		fi; \
	done

gitea_unmirror_repo:
	@echo "Converting mirror repository to normal: ${GITEA_PROJECT}/${REPO_NAME}"
	@TMP_PASS="$$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 20)"; \
	trap 'rm -f "$${cookiefile}"; \
		curl -sfS -X PATCH "${GITEA_API}/admin/users/admin" \
			-H "Authorization: token ${GITEA_TOKEN}" \
			-H "Content-Type: application/json" \
			-d "{\"source_id\":0,\"login_name\":\"admin\",\"password\":\"${GITEA_TOKEN}\"}" > /dev/null' EXIT; \
	curl -sfS -X PATCH \
		"${GITEA_API}/admin/users/admin" \
		-H "Authorization: token ${GITEA_TOKEN}" \
		-H "Content-Type: application/json" \
		-d "{\"source_id\":0,\"login_name\":\"admin\",\"password\":\"$${TMP_PASS}\"}" > /dev/null; \
	cookiefile="$$(mktemp)"; \
	curl -s -c "$${cookiefile}" "${GITEA_URL}/user/login" > /dev/null; \
	curl -s -b "$${cookiefile}" -c "$${cookiefile}" \
		-X POST "${GITEA_URL}/user/login" \
		-H "Content-Type: application/x-www-form-urlencoded" \
		-d "user_name=admin&password=$${TMP_PASS}" \
		-o /dev/null -w "%{http_code}" | grep -q "303" || { echo "Login failed"; exit 1; }; \
	curl -sfS -b "$${cookiefile}" \
		-X POST "${GITEA_URL}/${GITEA_PROJECT}/${REPO_NAME}/settings" \
		-H "Content-Type: application/x-www-form-urlencoded" \
		-d "action=convert&repo_name=${REPO_NAME}" \
		-o /dev/null -w "%{http_code}" | grep -qE "303|200" || { echo "Convert failed"; exit 1; }

gitea_remove_project:
	@echo "Removing Gitea project ${GITEA_PROJECT} with all repositories"
	${MAKE} gitea_delete_repos
	${MAKE} gitea_delete_org
