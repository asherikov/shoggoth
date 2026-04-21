secret_all: gitea_runner_token

secret_gitea_runner:
	@openssl rand -hex 24 > shoggoth/private/gitea-runner-token.txt
	@chmod 600 shoggoth/private/gitea-runner-token.txt

secret_kestra_db:
	@pwgen -1 > shoggoth/private/kestra-db-password.txt

secret_kestra_basic_auth:
	@pwgen -1 > shoggoth/private/kestra-basic-auth-password.txt

secret_ollama_bearer_token:
	@openssl rand -hex 32 > shoggoth/private/ollama-bearer-token.txt
	@chmod 600 shoggoth/private/ollama-bearer-token.txt
