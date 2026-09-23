#!/usr/bin/env bash
# make up, step 3: create this repo's own Ollama model from the base model with the
# context length baked in, so the global Ollama settings are never touched. A derived
# model shares the base model's weights; creating and removing it is near-instant.
set -euo pipefail

BASE="${FINEPRINT_BASE_MODEL:-qwen3.5}"
MODEL="${FINEPRINT_MODEL:-fineprint-$BASE}"
CONTEXT="${FINEPRINT_CONTEXT:-16384}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
BUILD_DIR=".build"

if ollama show "$BASE" >/dev/null 2>&1; then
  echo "base model $BASE present"
else
  echo "pulling $BASE"
  ollama pull "$BASE"
fi

# Recreated every run so a changed FINEPRINT_CONTEXT takes effect.
mkdir -p "$BUILD_DIR"
printf 'FROM %s\nPARAMETER num_ctx %s\n' "$BASE" "$CONTEXT" > "$BUILD_DIR/Modelfile"
ollama create "$MODEL" -f "$BUILD_DIR/Modelfile" >/dev/null 2>&1
echo "model $MODEL ready (from $BASE, context $CONTEXT)"

# A one-token request loads the model so `ollama ps` can report how it was placed.
ollama stop "$MODEL" >/dev/null 2>&1 || true
curl -sf "$OLLAMA_URL/api/generate" \
  -d "{\"model\": \"$MODEL\", \"prompt\": \"hi\", \"stream\": false, \"options\": {\"num_predict\": 1}}" \
  >/dev/null

ollama ps
row=$(ollama ps | awk -v m="$MODEL" 'index($1, m) == 1')

# Columns: NAME ID SIZE PROCESSOR CONTEXT UNTIL; PROCESSOR ends in "GPU" or "CPU".
loaded_context=$(echo "$row" | grep -oE '(GPU|CPU) +[0-9]+' | grep -oE '[0-9]+$' || true)
if [ "$loaded_context" != "$CONTEXT" ]; then
  echo "warning: $MODEL loaded with context '$loaded_context', expected $CONTEXT"
fi

if ! echo "$row" | grep -q "100% GPU"; then
  echo "warning: $MODEL is not fully on the GPU; it will run slower"
fi
