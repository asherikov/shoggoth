export GITEA_API?=https://tvoygit.ru/api/v1/repos/migrate
#export GITEA_TOKEN?=<token> # use auth.mk
# issues not copied if true, see https://github.com/go-gitea/gitea/pull/20311 and https://forum.gitea.com/t/mirror-a-github-site-does-not-mirror-issues/8141
export GITEA_MIRROR?=true

export GITHUB_USER?=asherikov
export GITHUB_REPO?=ccws
#export GITHUB_TOKEN?=<token> # use auth.mk

export REPO_DESCRIPTION?=


gitea_runner_token:
	@openssl rand -hex 24 > shoggoth/gitea-runner/runner-token.txt

github_to_gitea_repo:
	echo "Copying ${GITHUB_USER}/${GITHUB_REPO}"
	curl \
		"${GITEA_API}" \
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
			\"service\": \"git\", \
			\"wiki\": true \
			}" \
		-i

github_to_gitea_user:
	curl "https://api.github.com/users/${GITHUB_USER}/repos" \
		| jq -r '.[] | "${MAKE} github_to_gitea_repo GITHUB_REPO=\"\(.name )\" REPO_DESCRIPTION=\"\(.description)\""' | sed 's/null//' \
		| sh
