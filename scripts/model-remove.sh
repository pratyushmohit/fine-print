#!/usr/bin/env bash
# make down: unload and delete this repo's derived model. The base model and the global
# Ollama settings are left exactly as they were.
set -uo pipefail

BASE="${FINEPRINT_BASE_MODEL:-qwen3.5}"
MODEL="${FINEPRINT_MODEL:-fineprint-$BASE}"

if ! command -v ollama >/dev/null 2>&1 || ! ollama list >/dev/null 2>&1; then
  echo "ollama not running; skipping removal of $MODEL"
  exit 0
fi

if ollama show "$MODEL" >/dev/null 2>&1; then
  ollama stop "$MODEL" >/dev/null 2>&1 || true
  ollama rm "$MODEL" >/dev/null
  echo "removed $MODEL (base $BASE kept)"
else
  echo "$MODEL not present"
fi
