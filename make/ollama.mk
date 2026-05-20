OLLAMA_MODEL?=qwen3-coder:30b

ollama_tags:
	${MAKE} ssh_exec CMD='docker compose exec ollama ollama list'

ollama_query:
	time curl http://ollama.${SHOGGOTH_DOMAIN}/v1/completions \
		-H "Content-Type: application/json" \
		-H "Authorization: ollama" \
		-d '{"model": "${OLLAMA_MODEL}", "prompt": "What is the capital of UAE?"}'

ollama_pull:
	${MAKE} ssh_exec CMD='docker compose exec ollama ollama pull ${OLLAMA_MODEL}'
