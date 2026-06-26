AI_MODEL?=qwen3-coder:30b

ai_embed:
	curl http://litellm.${DOMAIN}/v1/embeddings -X POST -H "Content-Type: application/json" -H "Authorization: Bearer ai" -d '{ "input": "My text to embed", "model": "nomic-embed-text" }'

ai_models:
	curl http://localai.${DOMAIN}/v1/models

ai_query:
	time curl http://ai.${DOMAIN}/v1/completions \
		-H "Content-Type: application/json" \
		-H "Authorization: ai" \
		-d '{"model": "${AI_MODEL}", "prompt": "What is the capital of UAE?"}'
