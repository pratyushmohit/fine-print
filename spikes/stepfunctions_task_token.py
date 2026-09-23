"""Do Floci's Step Functions support the orchestration pattern FinePrint depends on?

Exercises, in one small state machine:
  - Parallel fan-out to two branches, joined back into one result
  - arn:aws:states:::sqs:sendMessage.waitForTaskToken, the pattern for driving workers
    Step Functions can't invoke directly
  - Retry on ModelError, HeartbeatSeconds, Catch
  - the direct arn:aws:states:::dynamodb:updateItem integration, for recording run status

A fake worker in this script plays the role of the EKS pods. Scenarios:
  success -> worker answers SendTaskSuccess     -> run SUCCEEDED, item status SUCCEEDED
  fail    -> worker answers SendTaskFailure     -> Retry, then Catch -> item FAILED
  silent  -> worker never answers               -> heartbeat expires -> Catch -> item FAILED
  stale   -> timed-out worker answers after a Retry was issued -> answer is ignored

Semantics confirmed by the first run (see spikes/README.md):
  - When one Parallel branch fails for good, the other branches are aborted, so a branch
    still waiting out its retry interval never gets its retry.
  - Floci reports heartbeat expiry as States.Timeout; on AWS, States.Timeout matches both
    TimeoutSeconds and HeartbeatSeconds expiry, so Retry/Catch on States.Timeout is portable.
  - A late answer on an expired token: AWS raises TaskTimedOut; Floci 2.1.0 accepts it but
    ignores it. The 'stale' scenario checks it never completes a retried task.

Run: uv run python spikes/stepfunctions_task_token.py
"""

import json
import time
from collections import Counter

import boto3

FLOCI_URL = "http://localhost:4566"
PREFIX = "spike-sfn"
HEARTBEAT_S = 10

aws = dict(
    endpoint_url=FLOCI_URL,
    region_name="us-east-1",
    aws_access_key_id="test",
    aws_secret_access_key="test",
)
sqs = boto3.client("sqs", **aws)
ddb = boto3.client("dynamodb", **aws)
sfn = boto3.client("stepfunctions", **aws)
iam = boto3.client("iam", **aws)


def branch(name: str, queue_url: str) -> dict:
    return {
        "StartAt": name,
        "States": {
            name: {
                "Type": "Task",
                "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
                "Parameters": {
                    "QueueUrl": queue_url,
                    "MessageBody": {
                        "run_id.$": "$.run_id",
                        "mode.$": "$.mode",
                        "branch": name,
                        "task_token.$": "$$.Task.Token",
                    },
                },
                "TimeoutSeconds": 60,
                "HeartbeatSeconds": HEARTBEAT_S,
                "Retry": [
                    {"ErrorEquals": ["ModelError"], "IntervalSeconds": 1, "MaxAttempts": 1, "BackoffRate": 1.0}
                ],
                "End": True,
            }
        },
    }


def mark(status: str, table: str, next_state: dict) -> dict:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::dynamodb:updateItem",
        "Parameters": {
            "TableName": table,
            "Key": {"PK": {"S.$": "$.run_id"}, "SK": {"S": "META"}},
            "UpdateExpression": "SET #s = :s",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":s": {"S": status}},
        },
        "ResultPath": None,
        **next_state,
    }


def definition(queue_url: str, table: str) -> dict:
    return {
        "StartAt": "Fanout",
        "States": {
            "Fanout": {
                "Type": "Parallel",
                "Branches": [branch("BranchA", queue_url), branch("BranchB", queue_url)],
                "ResultPath": "$.results",
                "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "MarkFailed"}],
                "Next": "MarkSucceeded",
            },
            "MarkSucceeded": mark("SUCCEEDED", table, {"End": True}),
            "MarkFailed": mark("FAILED", table, {"Next": "Failed"}),
            "Failed": {"Type": "Fail", "Error": "RunFailed"},
        },
    }


def setup() -> tuple[str, str, str]:
    queue_url = sqs.create_queue(QueueName=f"{PREFIX}-work")["QueueUrl"]
    sqs.purge_queue(QueueUrl=queue_url)

    table = f"{PREFIX}-table"
    if table not in ddb.list_tables()["TableNames"]:
        ddb.create_table(
            TableName=table,
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.get_waiter("table_exists").wait(TableName=table)

    role_name = f"{PREFIX}-sfn-role"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    try:
        role_arn = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]

    sm_name = f"{PREFIX}-machine"
    body = json.dumps(definition(queue_url, table))
    existing = [m for m in sfn.list_state_machines()["stateMachines"] if m["name"] == sm_name]
    if existing:
        sm_arn = existing[0]["stateMachineArn"]
        sfn.update_state_machine(stateMachineArn=sm_arn, definition=body, roleArn=role_arn)
    else:
        sm_arn = sfn.create_state_machine(name=sm_name, definition=body, roleArn=role_arn)["stateMachineArn"]

    print(f"queue:   {queue_url}\ntable:   {table}\nmachine: {sm_arn}")
    return queue_url, table, sm_arn


def run_scenario(mode: str, queue_url: str, table: str, sm_arn: str) -> bool:
    run_id = f"run-{mode}-{int(time.time())}"
    ddb.put_item(TableName=table, Item={"PK": {"S": run_id}, "SK": {"S": "META"}, "status": {"S": "PENDING"}})
    exec_arn = sfn.start_execution(stateMachineArn=sm_arn, name=run_id, input=json.dumps({"run_id": run_id, "mode": mode}))[
        "executionArn"
    ]
    print(f"\n=== scenario '{mode}' ({run_id})")

    received: Counter[str] = Counter()
    unanswered_tokens: list[str] = []
    started = time.perf_counter()
    status = "RUNNING"
    while status == "RUNNING" and time.perf_counter() - started < 90:
        # The fake worker: long-poll, act according to the scenario, delete.
        for msg in sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=2, MaxNumberOfMessages=10).get("Messages", []):
            body = json.loads(msg["Body"])
            received[body["branch"]] += 1
            token = body["task_token"]
            if body["mode"] == "success":
                sfn.send_task_success(taskToken=token, output=json.dumps({"branch": body["branch"], "ok": True}))
            elif body["mode"] == "fail":
                sfn.send_task_failure(taskToken=token, error="ModelError", cause="simulated model failure")
            else:  # "silent": never answer, so the heartbeat timer expires.
                unanswered_tokens.append(token)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        status = sfn.describe_execution(executionArn=exec_arn)["status"]

    desc = sfn.describe_execution(executionArn=exec_arn)
    elapsed = time.perf_counter() - started
    item = ddb.get_item(TableName=table, Key={"PK": {"S": run_id}, "SK": {"S": "META"}}).get("Item", {})
    item_status = item.get("status", {}).get("S")

    history = sfn.get_execution_history(executionArn=exec_arn, maxResults=200)["events"]
    errors = sorted(
        {
            d.get("error")
            for e in history
            for k, d in e.items()
            if k.endswith("EventDetails") and isinstance(d, dict) and d.get("error")
        }
    )

    print(f"execution status: {desc['status']} after {elapsed:.1f}s")
    print(f"messages received per branch: {dict(received)}")
    print(f"errors seen in history: {errors}")
    print(f"dynamodb item status: {item_status}")
    if desc["status"] == "SUCCEEDED":
        print("output:", desc.get("output"))

    checks = {
        "execution status": desc["status"] == ("SUCCEEDED" if mode == "success" else "FAILED"),
        "dynamodb updateItem": item_status == ("SUCCEEDED" if mode == "success" else "FAILED"),
    }
    if mode == "success":
        checks["one message per branch"] = received["BranchA"] == 1 and received["BranchB"] == 1
        checks["both branch outputs joined"] = len(json.loads(desc["output"])["results"]) == 2
    elif mode == "fail":
        checks["ModelError raised"] = "ModelError" in errors
        checks["retry sent a new message"] = max(received.values()) == 2
    else:
        checks["timeout error raised"] = bool({"States.Timeout", "States.HeartbeatTimeout"} & set(errors))
        checks["heartbeat fired before TimeoutSeconds"] = elapsed < 60
        # AWS rejects this with TaskTimedOut; Floci 2.1.0 accepts it. Either way it must not
        # change the (already failed) execution.
        late = [late_answer(t) for t in unanswered_tokens]
        print(f"late answers on expired tokens: {late} (AWS would say TaskTimedOut)")
        checks["late answer leaves execution FAILED"] = (
            bool(late) and sfn.describe_execution(executionArn=exec_arn)["status"] == "FAILED"
        )
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}: {name}")
    return all(checks.values())


def late_answer(token: str, output: dict | None = None) -> str:
    """What a worker sees when it answers after Step Functions has given up on the token."""
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps(output or {}))
        return "accepted"
    except sfn.exceptions.TaskTimedOut:
        return "TaskTimedOut"
    except Exception as e:  # noqa: BLE001 - report whatever Floci actually returns
        return type(e).__name__


def stale_retry_scenario() -> bool:
    """Attempt 1 times out and is retried with a new token; the attempt-1 worker then answers
    late. The stale answer must not complete the retried task."""
    print("\n=== scenario 'stale' (late answer from a timed-out attempt after a retry)")
    queue_url = sqs.create_queue(QueueName=f"{PREFIX}-stale")["QueueUrl"]
    sqs.purge_queue(QueueUrl=queue_url)
    role_arn = iam.get_role(RoleName=f"{PREFIX}-sfn-role")["Role"]["Arn"]
    body = json.dumps(
        {
            "StartAt": "Work",
            "States": {
                "Work": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
                    "Parameters": {"QueueUrl": queue_url, "MessageBody": {"task_token.$": "$$.Task.Token"}},
                    "TimeoutSeconds": 60,
                    "HeartbeatSeconds": 5,
                    "Retry": [
                        {"ErrorEquals": ["States.Timeout"], "IntervalSeconds": 1, "MaxAttempts": 1, "BackoffRate": 1.0}
                    ],
                    "End": True,
                }
            },
        }
    )
    sm_name = f"{PREFIX}-stale-machine"
    existing = [m for m in sfn.list_state_machines()["stateMachines"] if m["name"] == sm_name]
    if existing:
        sm_arn = existing[0]["stateMachineArn"]
        sfn.update_state_machine(stateMachineArn=sm_arn, definition=body, roleArn=role_arn)
    else:
        sm_arn = sfn.create_state_machine(name=sm_name, definition=body, roleArn=role_arn)["stateMachineArn"]
    exec_arn = sfn.start_execution(stateMachineArn=sm_arn, name=f"stale-{int(time.time())}", input="{}")[
        "executionArn"
    ]

    def next_token() -> str:
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            for msg in sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=5).get("Messages", []):
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
                return json.loads(msg["Body"])["task_token"]
        raise TimeoutError("no message received")

    stale = next_token()  # attempt 1: worker goes silent
    current = next_token()  # attempt 2: sent by the Retry after the heartbeat expired
    late = late_answer(stale, output={"from": "stale attempt 1"})
    time.sleep(1)
    after_stale = sfn.describe_execution(executionArn=exec_arn)["status"]
    sfn.send_task_success(taskToken=current, output=json.dumps({"from": "attempt 2"}))
    time.sleep(1)
    final = sfn.describe_execution(executionArn=exec_arn)
    print(f"late answer on attempt-1 token: {late}")
    print(f"execution after stale answer: {after_stale}; after real answer: {final['status']} {final.get('output')}")

    checks = {
        "retry used a new token": stale != current,
        "stale answer did not complete the task": after_stale == "RUNNING",
        "current answer completed it": final["status"] == "SUCCEEDED" and "attempt 2" in final.get("output", ""),
    }
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}: {name}")
    return all(checks.values())


def main() -> None:
    queue_url, table, sm_arn = setup()
    results = {mode: run_scenario(mode, queue_url, table, sm_arn) for mode in ("success", "fail", "silent")}
    results["stale"] = stale_retry_scenario()
    print("\nresult:", ", ".join(f"{m}={'PASS' if ok else 'FAIL'}" for m, ok in results.items()))


if __name__ == "__main__":
    main()
