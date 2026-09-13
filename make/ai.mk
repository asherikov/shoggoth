AI_MODEL?=shoggoth-default

ai_embed:
	curl http://litellm.${DOMAIN}/v1/embeddings -X POST -H "Content-Type: application/json" -H "Authorization: Bearer ai" -d '{ "input": "My text to embed", "model": "nomic-embed-text" }'

ai_models:
	curl http://localai.${DOMAIN}/v1/models

ai_query:
	time curl http://litellm.${DOMAIN}/v1/chat/completions \
		-H "Content-Type: application/json" \
		-H "Authorization: Bearer ai" \
		-d '{"model": "${AI_MODEL}", "messages": [{"role": "user", "content": "What is the capital of UAE?"}]}'
