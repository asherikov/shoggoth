test_api_redmine: tunnel_up
	curl --connect-timeout 10 -m 30 -w '%{http_code}' -X PUT \
		http://api.${DOMAIN}/redmine/issues/1.json \
		-H "Content-Type: application/json" \
		-d '{"issue": {"notes": "redmine_test target - ignore"}}'

test_redmine: tunnel_up
	curl --connect-timeout 10 -m 30 -w '%{http_code}' -X PUT \
		http://redmine.${DOMAIN}/issues/1.json \
		-H "Content-Type: application/json" \
		-H "X-Redmine-API-Key: ${REDMINE_TOKEN}" \
		-d '{"issue": {"notes": "redmine_test_direct target - ignore"}}'

test_api_gitea: tunnel_up
	curl --connect-timeout 10 -m 30 -w '%{http_code}' \
		http://api.${DOMAIN}/gitea/api/v1/user

test_gitea: tunnel_up
	curl --connect-timeout 10 -m 30 -w '%{http_code}' \
		http://git.${DOMAIN}/api/v1/user \
		-H "Authorization: token ${GITEA_TOKEN}"
