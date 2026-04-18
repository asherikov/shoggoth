secret_all: gitea_runner_token

secret_gitea_runner:
	@openssl rand -hex 24 > shoggoth/gitea-runner/runner-token.txt

secret_kestra_db:
	@pwgen -1 > shoggoth/kestra/db-password.txt
