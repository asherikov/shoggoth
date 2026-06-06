AI_MODEL?=qwen3-coder:30b

ollama_tags:
	${MAKE} ssh_exec CMD='docker compose exec ollama ollama list'

ollama_pull:
	${MAKE} ssh_exec CMD='docker compose exec ollama ollama pull ${AI_MODEL}'

ai_query:
	time curl http://ai.${DOMAIN}/v1/completions \
		-H "Content-Type: application/json" \
		-H "Authorization: ai" \
		-d '{"model": "${AI_MODEL}", "prompt": "What is the capital of UAE?"}'
