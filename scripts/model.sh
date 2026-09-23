#!/usr/bin/env bash
# make up, step 3: make sure the model is pulled, load it once, and warn if it is
# configured in a way that will hurt (huge context, or not fully on the GPU).
set -euo pipefail

MODEL="${FINEPRINT_MODEL:-qwen3.5}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
MAX_SANE_CONTEXT=32768

if ollama show "$MODEL" >/dev/null 2>&1; then
  echo "model $MODEL present"
else
  echo "pulling $MODEL"
  ollama pull "$MODEL"
fi

# A one-token request loads the model so `ollama ps` can report how it was placed.
curl -sf "$OLLAMA_URL/api/generate" \
  -d "{\"model\": \"$MODEL\", \"prompt\": \"hi\", \"stream\": false, \"options\": {\"num_predict\": 1}}" \
  >/dev/null

ollama ps
row=$(ollama ps | awk -v m="$MODEL" 'index($1, m) == 1')

# Columns: NAME ID SIZE PROCESSOR CONTEXT UNTIL; PROCESSOR ends in "GPU" or "CPU".
context=$(echo "$row" | grep -oE '(GPU|CPU) +[0-9]+' | grep -oE '[0-9]+$' || true)
if [ -n "$context" ] && [ "$context" -gt "$MAX_SANE_CONTEXT" ]; then
  echo "warning: context length is $context; lower it to 16384 in the Ollama app settings"
fi

if ! echo "$row" | grep -q "100% GPU"; then
  echo "warning: $MODEL is not fully on the GPU; it will run slower"
fi
