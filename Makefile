# Run from Git Bash on Windows (see requirements IAC-4).
# `make up` grows one step at a time as the build progresses (IAC-3).
SHELL := bash

# Model configuration (IAC-8). make up creates $(FINEPRINT_MODEL) from the base model with
# this context length; make down deletes it. Global Ollama settings are never changed.
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
