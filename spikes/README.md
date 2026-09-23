# Day-0 checks

Small experiments that test the assumptions the design depends on before building on them
(requirements §11). Each script prints PASS/FAIL with evidence. Run against a live
environment (`make up`) with `uv run python spikes/<script>.py`.

| Check | Script | Status |
|-------|--------|--------|
| D0-1 Vision via Ollama direct | — | pending (needs label photos in `spikes/images/`) |
| D0-2 Forced tool use via Floci Bedrock proxy | `bedrock_tool_use.py` | ✅ PASS |
| D0-3 Step Functions task-token orchestration | `stepfunctions_task_token.py` | ✅ PASS |
| D0-4 Pod → Floci / Ollama networking | — | pending (needs EKS) |
| D0-5 Terraform creates EKS on Floci | — | pending |

## D0-2: forced tool use through Floci's Bedrock proxy — PASS (2026-09-23)

`converse` with `toolConfig` + `toolChoice: {tool: {name}}` via Floci 2.1.0 (proxy backend)
→ Ollama `/v1/chat/completions` → `fineprint-qwen3.5` (16k context).

- 3/3 runs: `stopReason=tool_use`, a single `toolUse` block with the forced tool name,
  input validated against the Pydantic schema, identical and correct classifications
  ("Multigrain" → `unregulated`, "25% less sugar…" → `comparative`, nutrient `sugar`).
- Latency: 7.5 s first call, 3.3 s warm. Usage: 532 input / 284 output tokens.

**Observation:** 284 output tokens for ~100 tokens of JSON suggests `qwen3.5` is "thinking"
before answering. It works, but costs latency. The model client should disable thinking for
structured calls (MOD-6) once we confirm how to do that through each path.

**Design impact:** none. MOD-3 (structured output via forced tool use on Bedrock) holds.

## D0-3: Step Functions task-token orchestration — PASS (2026-09-23)

One state machine: `Parallel` (2 branches, each `sqs:sendMessage.waitForTaskToken`,
`HeartbeatSeconds: 10`, `Retry` on `ModelError`) → `Catch` → direct `dynamodb:updateItem`.
A fake worker in the script plays the EKS pods.

| Scenario | Result |
|----------|--------|
| success: worker calls `SendTaskSuccess` | SUCCEEDED in 0.3 s; both branch outputs joined into one array; DynamoDB item → SUCCEEDED |
| fail: worker calls `SendTaskFailure(ModelError)` | Retry sent a new message; then Catch → DynamoDB item → FAILED |
| silent: worker never answers | heartbeat expired at 10.5 s (TimeoutSeconds was 60) → Catch → FAILED |
| stale: attempt 1 times out, Retry issues a new token, attempt-1 worker answers late | stale answer ignored; execution completed only with attempt 2's answer |

**Semantics learned (all match AWS unless noted):**

1. **A failed Parallel branch aborts its siblings.** In the fail scenario both branches failed
   at once; while BranchB waited out its 1 s retry interval, BranchA's retry failed and the
   Parallel state aborted BranchB before its retry was sent. Expect uneven retry counts
   across branches; the first branch to fail for good ends the whole group.
2. **Heartbeat expiry is reported as `States.Timeout`** (Floci), not
   `States.HeartbeatTimeout`. On AWS `States.Timeout` matches both timeout kinds, so
   `Retry`/`Catch` on `States.Timeout` (as ORC-3 specifies) is portable.
3. **Floci deviation: late answers on expired tokens are accepted.** AWS returns
   `TaskTimedOut`; Floci 2.1.0 returns success but ignores the answer. The stale scenario
   shows this is safe: a stale token never completes a retried task.

**Design impact:**
- WRK-2's `except TaskTimedOut` branch stays (correct on AWS) but never fires on Floci.
  Workers must not treat an accepted `SendTaskSuccess` as proof their result was used;
  idempotent writes (WRK-3) already make that safe.
- TST-5 "worker stalls past HeartbeatSeconds" is reworded to assert that the late answer does
  not change the result, rather than that the old worker receives `TaskTimedOut`.
