# Run from Git Bash on Windows (see requirements IAC-4).
# `make up` grows one step at a time as the build progresses (IAC-3).
SHELL := bash

export FINEPRINT_MODEL ?= qwen3.5

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
