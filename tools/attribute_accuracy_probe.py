"""Isolated attribute accuracy screening. Explicit execution, immutable inputs.

A successful API/schema result is NOT a semantic pass. Human/source review must
pass all frozen development gates before starting an independent evaluation.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from pydantic import ValidationError
from tools import attribute_prompt_probe as probe

SIDS = ("127ffc0852bd180f", "4f07a2a43ec36385", "64352f1327e96dc8")
CAMPAIGN = "accuracy-structured-v3-20261001"


def freeze(path, value):
    """Never replace prior evidence or silently change an experiment's inputs."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if path.exists():
        if path.read_text() != encoded:
            raise RuntimeError("frozen evidence changed: " + path.name)
        return
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(directory, revision=3):
    prepared = []
    for sid in SIDS:
        sample = json.loads((directory / (
            sid + "-explicit-evidence-temp0-quotes-grounded-review-candidate-only-comparison.json"
        )).read_text())["sample"]
        if sample["sid"] != sid:
            raise RuntimeError("snapshot identity mismatch")
        request = asyncio.run(probe.capture(sample, 16))[0]
        probe.omit_history(request)
        if revision == 5:
            probe.add_evidence_first_contract(request, sample["episode"].get("valid_at"))
        else:
            probe.add_unified_contract(request, sample["episode"].get("valid_at"), revision=revision)
        request["temperature"] = 0.0
        frozen = {"messages": request["messages"], "schema": request["model"].model_json_schema(),
                  "uuids": request["uuids"], "temperature": request["temperature"]}
        digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        prepared.append((sid, sample, request, digest))
    return prepared


def screen(directory, execute=False, revision=3, model_arm="flash", thinking=False):
    if model_arm not in ("flash", "max"):
        raise ValueError("invalid model arm")
    if thinking and model_arm != "max":
        raise ValueError("thinking experiment requires Max")
    directory = Path(directory)
    campaign = CAMPAIGN.replace("v3", "v" + str(revision))
    if model_arm == "max":
        campaign = campaign.replace("20261001", "max-thinking-20261002" if thinking else "max-20261002")
    output = directory / campaign
    output.mkdir(mode=0o700, exist_ok=True)
    prepared = prepare(directory, revision)
    plan = {"campaign": campaign, "stage": "development-screen", "max_new_calls": 3,
            "thinking": "enabled" if thinking else "disabled",
            **({"thinking_budget_tokens": 2048} if thinking else {}), "output_format": "json_schema", "max_tokens": 8192,
            **({"provider_model": "qwen3.8-max"} if model_arm == "max" else {}),
            "temperature_requested": 0.0,
            "steps": [{"sid": sid, "input_sha256": digest} for sid, _, _, digest in prepared]}
    freeze(output / "plan.json", plan)
    if not execute:
        print(json.dumps(plan, ensure_ascii=False))
        return
    expected_key = "kg_hub.attribute_max_probe" if model_arm == "max" else "kg_hub.attribute_accuracy_probe"
    if os.environ.get("ANTHROPIC_MODEL") != expected_key:
        raise RuntimeError("isolated business key required")
    for sid, sample, request, digest in prepared:
        saved = probe.call(request, output, structured_output=True, structured_thinking=thinking)
        result = {"sid": sid, "input_sha256": digest, "semantic_review": "pending",
                  **{key: saved.get(key) for key in ("body_digest", "elapsed", "usage",
                      "stop_reason", "parse_error", "response_block_types", "response_model", "thinking_chars")}}
        try:
            if revision == 5:
                result["output"], result["evidence_errors"] = probe.evidence_first_output(request, saved["payload"])
            else:
                result["output"] = probe.flatten(request, saved["payload"])
            result["schema_valid"] = True
            result["existing_value_losses"] = probe.lost_existing_values(sample, result["output"])
        except ValidationError as exc:
            result.update(schema_valid=False, errors=exc.errors(include_input=False, include_url=False))
        freeze(output / (sid + "-result.json"), result)
        print(json.dumps({k: v for k, v in result.items() if k != "output"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/backup/attribute-probe"))
    parser.add_argument("--execute", action="store_true", help="Send up to three isolated model requests")
    parser.add_argument("--revision", type=int, choices=(3, 4, 5), default=3)
    parser.add_argument("--model-arm", choices=("flash", "max"), default="flash")
    parser.add_argument("--thinking", action="store_true")
    args = parser.parse_args()
    screen(args.directory, execute=args.execute, revision=args.revision, model_arm=args.model_arm, thinking=args.thinking)
