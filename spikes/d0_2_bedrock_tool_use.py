"""D0-2: does a forced tool call through Floci's Bedrock proxy return a structured toolUse block?

Path under test: boto3 bedrock-runtime `converse` -> Floci (proxy backend)
-> Ollama /v1/chat/completions -> fineprint-qwen3.5, and back.

Run: uv run python spikes/d0_2_bedrock_tool_use.py
"""

import json
import time
from typing import Literal

import boto3
from botocore.config import Config
from pydantic import BaseModel, ValidationError

FLOCI_URL = "http://localhost:4566"
# Any Bedrock id works: Floci resolves unmapped ids to its default model (fineprint-qwen3.5).
MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
RUNS = 3


class ClassifiedClaim(BaseModel):
    claim: str
    claim_type: Literal["nutrient_content", "comparative", "implied", "unregulated"]
    nutrient: str | None


class ClaimTypes(BaseModel):
    claims: list[ClassifiedClaim]


TOOL_NAME = "record_claim_types"
TOOL = {
    "toolSpec": {
        "name": TOOL_NAME,
        "description": "Record the type of each front-of-package food label claim.",
        "inputSchema": {"json": ClaimTypes.model_json_schema()},
    }
}
SYSTEM = (
    "You classify front-of-package food label claims. "
    "nutrient_content: states a nutrient level (e.g. 'high protein', 'low fat'). "
    "comparative: compares to another food (e.g. '25% less sugar'). "
    "implied: suggests a health benefit without a number (e.g. 'heart healthy'). "
    "unregulated: marketing term with no legal definition (e.g. 'multigrain', 'natural'). "
    "nutrient is the nutrient the claim refers to, or null."
)
CLAIMS = ["High Protein", "Multigrain", "25% less sugar than our original", "Heart healthy"]


def main() -> None:
    client = boto3.client(
        "bedrock-runtime",
        endpoint_url=FLOCI_URL,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        # Local models can take a while on the first call; don't let boto3 give up at 60s.
        config=Config(read_timeout=300, retries={"max_attempts": 1}),
    )

    passed = 0
    for run in range(1, RUNS + 1):
        started = time.perf_counter()
        resp = client.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [{"text": f"Claims: {json.dumps(CLAIMS)}"}]}],
            toolConfig={"tools": [TOOL], "toolChoice": {"tool": {"name": TOOL_NAME}}},
            inferenceConfig={"maxTokens": 1024, "temperature": 0},
        )
        elapsed = time.perf_counter() - started

        content = resp["output"]["message"]["content"]
        tool_use = next((b["toolUse"] for b in content if "toolUse" in b), None)
        print(f"\n--- run {run}: {elapsed:.1f}s, stopReason={resp['stopReason']}, usage={resp['usage']}")
        print("blocks:", [next(iter(b)) for b in content])

        if tool_use is None:
            print("FAIL: no toolUse block. Content:", json.dumps(content)[:500])
            continue

        print("tool name:", tool_use["name"])
        try:
            result = ClaimTypes.model_validate(tool_use["input"])
        except ValidationError as e:
            print("FAIL: toolUse input does not match schema:\n", e)
            print("input was:", json.dumps(tool_use["input"])[:500])
            continue

        for c in result.claims:
            print(f"  {c.claim!r:40} {c.claim_type:17} nutrient={c.nutrient}")
        if len(result.claims) != len(CLAIMS):
            print(f"FAIL: expected {len(CLAIMS)} claims, got {len(result.claims)}")
            continue
        passed += 1
        print("PASS")

    print(f"\nD0-2 result: {passed}/{RUNS} runs returned a valid, schema-conforming toolUse block")


if __name__ == "__main__":
    main()
