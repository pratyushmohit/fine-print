#!/usr/bin/env bash
# First step of `make up`: check prerequisites and fail fast with a clear message.
set -uo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
failed=0

fail() { echo "  ✗ $1"; failed=1; }
ok() { echo "  ✓ $1"; }

echo "preflight"

# On Windows, `bash` launched from PowerShell or cmd is WSL's bash, which sees different
# tools and a different Docker context. This repo's scripts expect Git Bash.
if uname -r | grep -qi microsoft; then
  fail "running inside WSL; run make from Git Bash instead"
fi

for tool in docker terraform kubectl aws uv ollama curl; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "$tool"
  else
    fail "$tool not found on PATH"
  fi
done

if docker info >/dev/null 2>&1; then
  ok "docker daemon running"
else
  fail "docker daemon not reachable; start Docker Desktop"
fi

if curl -sf "$OLLAMA_URL/api/version" >/dev/null; then
  ok "ollama running at $OLLAMA_URL"
else
  fail "ollama not reachable at $OLLAMA_URL; start the Ollama app"
fi

if [ "$failed" -ne 0 ]; then
  echo "preflight failed"
  exit 1
fi
