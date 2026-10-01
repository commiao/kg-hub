"""Six predeclared isolated calls; no production routing or graph writes."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from pydantic import ValidationError
from tools import attribute_prompt_probe as probe

SIDS = ("4f07a2a43ec36385", "127ffc0852bd180f", "64352f1327e96dc8")
CAMPAIGN = "thinking-v3-medium-20261001"


def atomic(path, data):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def run(directory):
    directory = Path(directory)
    prepared = []
    for i, sid in enumerate(SIDS):
        sample = json.loads((directory / (
            sid + "-explicit-evidence-temp0-quotes-grounded-review-candidate-only-comparison.json"
        )).read_text())["sample"]
        if sample["sid"] != sid:
            raise RuntimeError("snapshot identity changed")
        request = asyncio.run(probe.capture(sample, 16))[0]
        probe.omit_history(request)
        probe.add_unified_contract(request, sample["episode"].get("valid_at"), revision=3)
        request["temperature"] = 0
        schema = request["model"].model_json_schema()
        name = request["model"].__name__
        historical_body = dict(model="kg_hub.entity_extract", max_tokens=4096, temperature=0.0,
            system=request["messages"][0]["content"],
            messages=[{"role": "user", "content": request["messages"][1]["content"]}],
            tools=[{"name": name, "description": schema.get("description", f"Extract {name} information"),
                    "input_schema": schema}], tool_choice={"type": "tool", "name": name},
            extra_body={"thinking": {"type": "disabled"}})
        historical_digest = hashlib.sha256(json.dumps(historical_body, ensure_ascii=False,
                                                      sort_keys=True).encode()).hexdigest()
        historical_path = directory / (historical_digest + ".json")
        if not historical_path.exists() or json.loads(historical_path.read_text()).get("phase") != "completed":
            raise RuntimeError("candidate differs from completed v3 control: " + sid)
        # Preserve the original numeric spelling for exact request provenance.
        request["temperature"] = 0.0
        frozen = {"messages": request["messages"], "schema": request["model"].model_json_schema(),
                  "uuids": request["uuids"], "temperature": 0}
        digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        arms = ("disabled", "enabled") if i % 2 == 0 else ("enabled", "disabled")
        prepared.extend((sid, arm, sample, request, digest) for arm in arms)
    plan = {"campaign": CAMPAIGN, "model": "qwen3.8-flash", "max_tokens": 8192,
            "tool_choice": "auto", "enabled_effort": "medium", "thinking_budget_tokens": 2048,
            "max_calls": 6,
            "steps": [{"sid": sid, "arm": arm, "frozen_digest": digest}
                      for sid, arm, _, _, digest in prepared]}
    # The original no-budget plan remains as evidence of its local 400 rejection.
    # Ledger/http-start/witness audit confirmed only the disabled arm went upstream.
    plan_path = directory / (CAMPAIGN + "-plan-budget2048.json")
    if plan_path.exists():
        if json.loads(plan_path.read_text()) != plan:
            raise RuntimeError("frozen campaign changed; refusing new calls")
    else:
        atomic(plan_path, plan)
    results = []
    for sid, arm, sample, request, digest in prepared:
        # Unknown/transport outcomes stop here. Completed receipt replay is safe.
        saved = probe.call(request, directory / CAMPAIGN, thinking_arm=arm)
        result = {"sid": sid, "arm": arm, "frozen_digest": digest,
                  **{key: saved.get(key) for key in (
                      "body_digest", "elapsed", "usage", "stop_reason", "response_model",
                      "thinking_chars", "response_block_types", "text_blocks", "payload")}}
        try:
            output = probe.flatten(request, saved["payload"])
            result.update(schema_valid=True, output=output,
                          existing_value_losses=probe.lost_existing_values(sample, output),
                          missing_source_quotes=probe.missing_source_quotes(request, output))
        except ValidationError as exc:
            result.update(schema_valid=False, errors=exc.errors(include_input=False, include_url=False))
        results.append(result)
        atomic(directory / (CAMPAIGN + "-results.json"), {"plan": plan, "results": results})
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("payload", "output", "text_blocks", "missing_source_quotes")},
                         ensure_ascii=False), flush=True)


def collect_default_comparison(directory):
    """Audit completed default-effort calls. This mode can NEVER send a request.

    Two medium-effort provider 400s consumed the bounded campaign's slots. The
    three remaining slots were explicitly assigned to hard/on, config/off,
    config/on. Hard/off reuses its original completed receipt, not a new trial.
    """
    directory = Path(directory)
    os.environ["ANTHROPIC_BASE_URL"] = "http://kg-attribute-thinking-probe:39000"
    os.environ["ANTHROPIC_MODEL"] = "kg_hub.attribute_thinking_probe"
    frozen_plan = json.loads((directory / (CAMPAIGN + "-plan-budget2048.json")).read_text())
    results = []
    for sid in (SIDS[0], SIDS[2]):
        sample = json.loads((directory / (
            sid + "-explicit-evidence-temp0-quotes-grounded-review-candidate-only-comparison.json"
        )).read_text())["sample"]
        request = asyncio.run(probe.capture(sample, 16))[0]
        probe.omit_history(request)
        probe.add_unified_contract(request, sample["episode"].get("valid_at"), revision=3)
        request["temperature"] = 0.0
        frozen = {"messages": request["messages"], "schema": request["model"].model_json_schema(),
                  "uuids": request["uuids"], "temperature": 0}
        digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if not any(step["sid"] == sid and step["frozen_digest"] == digest for step in frozen_plan["steps"]):
            raise RuntimeError("comparison source changed")
        for arm in ("disabled", "enabled-default"):
            saved = probe.call(request, directory / CAMPAIGN, replay_only=True, thinking_arm=arm)
            result = {"sid": sid, "arm": arm, "frozen_digest": digest,
                      **{k: saved.get(k) for k in ("body_digest", "elapsed", "usage", "stop_reason",
                         "thinking_chars", "response_block_types", "text_blocks", "payload")}}
            try:
                result["output"] = probe.flatten(request, saved["payload"])
                result["schema_valid"] = True
            except ValidationError as exc:
                result.update(schema_valid=False, errors=exc.errors(include_input=False, include_url=False))
            results.append(result)
    atomic(directory / "thinking-v3-default-20261001-results.json", {"results": results})
    print(json.dumps([{k: v for k, v in result.items() if k not in ("output", "payload", "text_blocks")}
                      for result in results], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect-default-results", action="store_true",
                        help="Only audit existing receipts; never make model calls")
    parser.add_argument("--execute", action="store_true", help="Execute frozen medium-effort campaign")
    args = parser.parse_args()
    if args.collect_default_results:
        collect_default_comparison("/tmp/kg-attribute-probe")
    elif args.execute:
        run("/tmp/kg-attribute-probe")
