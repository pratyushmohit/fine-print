# Run from Git Bash on Windows: from PowerShell, `bash` resolves to WSL, which the preflight
# check rejects.
SHELL := bash

# make up creates $(FINEPRINT_MODEL) from the base model with this context length; make down
# deletes it. Global Ollama settings are never changed.
export FINEPRINT_BASE_MODEL ?= qwen3.5
export FINEPRINT_CONTEXT ?= 16384
export FINEPRINT_MODEL := fineprint-$(FINEPRINT_BASE_MODEL)

.PHONY: up down preflight floci model

up: preflight floci model
	@echo "up: environment ready (floci + model). Terraform and Kubernetes steps come next."

preflight:
	@bash scripts/preflight.sh

floci:
	docker compose up -d
	@bash scripts/wait-floci.sh

model:
	@bash scripts/model.sh

down:
	docker compose down -v --remove-orphans
	@bash scripts/model-remove.sh
