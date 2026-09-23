# FinePrint — Requirements

> Status: locked 2026-09-23. Design rationale and explanations: [architecture.md](architecture.md).
> Requirement IDs are stable; reference them in commits, tests and issues.

## 0. Scope

**In scope:** one product per run, 1–3 photos of that product, one report per run, one
seeded profile, deployed to Floci (EKS + AWS services) with Terraform.

**Out of scope:** meal logging, calorie or workout tracking, history/trend dashboards,
several products per run, barcode or product-database lookup, multiple users or auth, web UI.
Check any addition against this list before building it.

## 1. API

- **API-1** `POST /runs` accepts multipart `files` (1–3, JPEG or PNG, ≤ 5 MB each) and optional
  `profile_id` (default `default`). Returns `202 {run_id, status_url}`.
  Errors: `400` wrong file count, `413` file too large, `415` wrong type, `404` unknown profile.
- **API-2** Optional `Idempotency-Key` header. A repeat with the same key returns the original
  `run_id`, enforced by a conditional write on `IDEMP#<key>` (TTL 24 h).
- **API-3** `POST /runs` order: upload images to S3 → write `RUN#<id>/META` as `PENDING` →
  `StartExecution` with `name=run_id` (Step Functions rejects a duplicate name).
- **API-4** `GET /runs/{run_id}` returns status (`PENDING | RUNNING | SUCCEEDED | DEGRADED |
  FAILED`), per-agent status, and the report once finished. `404` for unknown ids.
- **API-5** `GET /healthz` reports the process is alive; `GET /readyz` checks DynamoDB, S3 and
  Step Functions are reachable.
- **API-6** The API is asynchronous and never calls a model.

## 2. Orchestration (Step Functions)

- **ORC-1** State machine: `Extract → Analyze (Parallel: Claims, Ingredients, Processing) →
  Synthesize`, plus `MarkFailed`, which uses the direct `dynamodb:updateItem` integration.
- **ORC-2** Every agent Task uses `arn:aws:states:::sqs:sendMessage.waitForTaskToken` with a
  message body of `{run_id, task_token, attempt}` only.
- **ORC-3** Every Task sets `TimeoutSeconds`, `HeartbeatSeconds`, and `Retry` on `ModelError`
  and `States.Timeout` with exponential backoff. Timeouts are generous, because Floci starts
  the timeout clock at schedule time.
- **ORC-4** Extraction failure → `MarkFailed` → run `FAILED`. Analysis branch failure → `Pass`
  state emitting `{status: "degraded"}`; Synthesis still runs; run ends `DEGRADED`.
- **ORC-5** Workflow state carries pointers only; payloads stay far below 256 KB.
- **ORC-6** The state machine definition lives in `infra/statemachine/fineprint.asl.json`
  and is rendered by Terraform `templatefile` with queue URLs and table name.

## 3. Workers (shared behaviour)

- **WRK-1** One container image; the entrypoint argument selects the agent.
- **WRK-2** Long-poll SQS (`WaitTimeSeconds=20`), one message at a time. Delete the message
  only after reporting the result to Step Functions, or on `TaskTimedOut`.
- **WRK-3** Idempotent: check `RUN#<id>/AGENT#<name>` first; write output with a conditional
  put; on conflict, return the stored output.
- **WRK-4** While a task runs, a heartbeat thread calls `SendTaskHeartbeat` and extends the SQS
  message's visibility timeout.
- **WRK-5** Every queue has a DLQ with `maxReceiveCount = 3`.
- **WRK-6** Graceful shutdown: on SIGTERM stop polling, finish the current task, exit.
  `terminationGracePeriodSeconds` exceeds the longest task timeout.
- **WRK-7** Each output record stores `prompt_version`, `model_id`, `input_tokens`,
  `output_tokens`, `latency_ms`, `attempts`.
- **WRK-8** Workers expose `/healthz` including the time of the last poll, so a stuck poll
  loop fails the liveness probe.

## 4. Model access

- **MOD-1** All model calls go through `ModelClient` with three implementations:
  `BedrockConverseClient`, `OpenAICompatClient`, `FakeModelClient` (ADR-001).
- **MOD-2** Provider, model id and timeout are set per agent through environment variables.
  Locally: extraction uses `openai_compat`; all other agents use `bedrock`.
- **MOD-3** Structured output: Bedrock uses a forced tool call (`toolChoice`); the
  OpenAI-compatible client uses tool calling or JSON-schema response format, whichever the
  day-0 check shows works reliably.
- **MOD-4** Every response is validated with Pydantic. On failure, retry **once** with the
  validation errors appended to the prompt; after a second failure raise `ModelError`.
- **MOD-5** `stopReason == "max_tokens"` (or `finish_reason == "length"`) is a failure.
- **MOD-6** Temperature 0 for extraction and classification; ≤ 0.3 for the synthesis narrative.
  Model "thinking" is disabled for structured calls unless the day-0 check shows it helps.
- **MOD-7** Prompts are files at `src/fineprint/prompts/<agent>/v<N>.md`; the version used is
  recorded on every output (WRK-7).

## 5. Agents

### Extraction
- **EXT-1** Input: all images for the run. Output schema: `claims[]` (verbatim text + which
  image), `nutrition` (serving size and unit, servings per container, per-nutrient amount,
  unit and %DV), `ingredients[]` in label order with nested sub-ingredients, `contains[]`
  (allergen statement), and per-field `confidence` or `unreadable`.
- **EXT-2** Deterministic check: %DV values are consistent with gram amounts against the
  FDA daily values; mismatches are flagged, not silently corrected.
- **EXT-3** Missing Nutrition Facts panel or ingredient list → output still valid, with the
  section marked `unreadable`; downstream agents report `insufficient_data`.

### Claims
- **CLM-1** The model classifies each claim into: nutrient-content, comparative, implied,
  or unregulated; and names the nutrient it refers to.
- **CLM-2** Verdicts come from `rules/fda_claims.yaml`, never from the model:
  `supported`, `not_supported`, `not_a_regulated_term`, `insufficient_data`.
- **CLM-3** Each verdict cites its regulation section (e.g. 21 CFR 101.54).
- **CLM-4** Thresholds are transcribed from the CFR when the rule table is built, not recalled
  from memory. Known points to pin: "good source" 10–19% DV and "high / excellent source"
  ≥ 20% DV (101.54); protein claims require a declared, quality-corrected (PDCAAS) %DV
  (101.9(c)(7)); thresholds are per reference amount customarily consumed (RACC), not per
  label serving; "no added sugar" conditions (101.60(c)(2)).

### Ingredients
- **ING-1** Match every ingredient against `rules/sugar_aliases.yaml` and
  `rules/categories.yaml` (artificial sweeteners, sugar alcohols, partially hydrogenated oils).
- **ING-2** Report each match's position in the list, and the count of distinct sugar aliases.
- **ING-3** Match allergens against both the ingredients and the "Contains" statement.
- **ING-4** The model is called only for ingredients the lists can't match, to categorise them.

### Processing
- **PRO-1** `rules/nova_markers.yaml` markers set a minimum NOVA group; the model may raise it,
  never lower it below the marker minimum.
- **PRO-2** Output: NOVA group 1–4, evidence (which ingredients), and an explicit "estimate"
  label.

### Synthesis
- **SYN-1** Score 0–100 is computed by code. Placeholder formula: start at 100; subtract per
  `not_supported` claim, per sugar alias beyond 2, for NOVA 3 or 4, and per profile
  violation. A matching allergen overrides the result to "not suitable for your profile".
- **SYN-2** The model writes the narrative only, from the computed results.
- **SYN-3** Banned-phrase check on the narrative (e.g. "is unhealthy", "will cause",
  "dangerous", "cures"). One regeneration on a hit; then fall back to a templated summary.
- **SYN-4** The report lists degraded agents and carries a fixed non-diagnostic disclaimer.
- **SYN-5** Synthesis writes the final run status (`SUCCEEDED` or `DEGRADED`).

## 6. Data

- **DAT-1** One DynamoDB table `fineprint`, on-demand billing, keys `PK`/`SK` as in
  architecture §3.5, TTL attribute `expires_at` (used by `IDEMP#` items).
- **DAT-2** One S3 bucket `fineprint-uploads`, keys `runs/<run_id>/images/<n>.<ext>`.
- **DAT-3** `PROFILE#default` is seeded by `make up`:
  allergens `[peanuts, milk]`, `max_added_sugar_g: 6`, `min_protein_g: 10`,
  `max_sodium_mg: 480`, `avoid_categories: [artificial_sweeteners, sugar_alcohols]`.

## 7. Infrastructure (Terraform)

- **IAC-1** Terraform creates: VPC and subnets; S3 bucket; DynamoDB table; 5 work queues and
  5 DLQs (`for_each`); the state machine; IAM roles for the api, each agent and Step
  Functions, with least-privilege policies; an ECR repository; the EKS cluster and node group;
  CloudWatch log groups.
- **IAC-2** Terraform outputs (queue URLs, table, bucket, state machine ARN, ECR URL, cluster
  name) are rendered into the Kubernetes ConfigMap.
- **IAC-3** `make up` brings up the entire pipeline from nothing with one command, assuming
  the prerequisites in IAC-4 are installed. Steps, in order:

  | # | Step | What it does |
  |---|------|--------------|
  | 1 | preflight | Checks prerequisites (IAC-4); fails fast with a message naming what is missing |
  | 2 | floci | `docker compose up -d` using the repo's `docker-compose.yml`: Floci pinned to a version, Bedrock proxy backend pointed at `http://host.docker.internal:11434/v1`, model mapping set; waits until `:4566` responds |
  | 3 | model | `ollama pull <model>` if the configured model is missing (no-op otherwise) |
  | 4 | terraform | `terraform init` + `terraform apply -auto-approve` |
  | 5 | image | `docker build` → push to Floci ECR |
  | 6 | kubernetes | `aws eks update-kubeconfig` → render ConfigMap from Terraform outputs → `kubectl apply -k deploy/k8s` → wait until every Deployment is available |
  | 7 | seed | Writes `PROFILE#default` to DynamoDB (idempotent) |
  | 8 | report | Prints how to reach the API (`make api`) |

  Every step is safe to re-run: running `make up` twice changes nothing the second time.
- **IAC-4** Prerequisites, checked by preflight: Docker Desktop running; Ollama running on
  `:11434`; `terraform`, `kubectl`, `aws`, `uv` on `PATH`. The Makefile runs under a POSIX
  shell (Git Bash on Windows). One-time manual setup, which `make up` cannot do: install the
  tools, start Ollama, set `OLLAMA_CONTEXT_LENGTH=16384`.
- **IAC-5** `make down` reverses `make up`: `kubectl delete -k` → `terraform destroy
  -auto-approve` → `docker compose down -v`. `make down && make up` must succeed.
- **IAC-6** `make api` runs `kubectl port-forward svc/api 8080:80` in the foreground. It is kept
  out of `make up` so no background process is left orphaned.
- **IAC-7** The repo owns Floci's configuration (`docker-compose.yml`); no externally started
  Floci container is assumed. The Floci image is pinned to a version, not `latest`.

## 8. Kubernetes

- **K8S-1** Namespace `fineprint`; one ServiceAccount per component.
- **K8S-2** `api` Deployment with 2 replicas and a ClusterIP Service; 5 worker Deployments
  with 1 replica each.
- **K8S-3** Every pod has liveness and readiness probes and resource requests and limits.
- **K8S-4** All configuration comes from the ConfigMap and Secret; nothing
  environment-specific is baked into the image.
- **K8S-5** Pod credentials via Pod Identity if Floci's webhook works; otherwise a Secret with
  Floci credentials.

## 9. Observability

- **OBS-1** Structured JSON logs with `run_id`, `agent`, `attempt`, `event`, to stdout and to
  CloudWatch Logs group `/fineprint/<component>`.
- **OBS-2** Events logged: `task.received`, `model.call` (tokens, latency),
  `validation.failed`, `task.succeeded`, `task.failed`, `task.timed_out`.
- **OBS-3** *(optional)* CloudWatch metrics per agent: latency, failures, retries,
  schema-repair rate.

## 10. Testing

- **TST-1** Unit tests: rule tables, alias matching, score formula, profile checks,
  banned-phrase check.
- **TST-2** Contract tests: each agent's Pydantic schemas accept the fixtures in
  `tests/fixtures/`.
- **TST-3** Integration tests against Floci with `FakeModelClient`: one full run end to end,
  deterministic, no GPU.
- **TST-4** Evaluation: about 10 photographed products with hand-written correct answers, run
  against the real model, producing an accuracy table (extraction fields, claim verdicts).
- **TST-5** Fault injection:

  | Fault | Expected behaviour |
  |-------|--------------------|
  | Kill a worker mid-task | SQS redelivers; idempotent write prevents a duplicate |
  | Model returns malformed JSON | repair retry, then `ModelError` → Step Functions retry |
  | Worker stalls past `HeartbeatSeconds` | Step Functions retries with a new token; old worker gets `TaskTimedOut` |
  | Claims agent always fails | run ends `DEGRADED` |
  | Message fails 3 times | it lands in the DLQ |

## 11. Day-0 checks (before building)

Each of these could break the design, so check them first.

| # | Check | If it fails |
|---|-------|-------------|
| D0-1 | `qwen3.5` reads a label photo via Ollama `/v1/chat/completions` and returns valid JSON; record latency and GPU/CPU split (`ollama ps`) | try another vision model |
| D0-2 | Floci Bedrock proxy: `converse` with forced `toolChoice` returns a `toolUse` block from `qwen3.5` | JSON-mode prompt + validation instead of tool use |
| D0-3 | Floci Step Functions: `sqs:sendMessage.waitForTaskToken` and `dynamodb:updateItem` integrations work | worker writes `FAILED` itself |
| D0-4 | A pod in Floci's k3s reaches Floci (`:4566`) and Ollama (`host.docker.internal:11434`) | `hostAliases` or IP addresses |
| D0-5 | `aws_eks_cluster` and node group create via Terraform on Floci | create the cluster with the AWS CLI, keep the rest in Terraform |

## 12. Cut order if time runs short

1. OBS-3 metrics
2. TST-5 fault injection
3. K8S-5 Pod Identity (use the Secret)
4. TST-4 evaluation set reduced to 3–5 products

Never cut: Extraction, Claims, Synthesis, Step Functions orchestration, Terraform, idempotency.
