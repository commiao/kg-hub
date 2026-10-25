"""Official, model-free entry point for node-level graph governance.

merge / rename / retype / split nodes across sources and campaigns. Preview is
read-only and returns the complete plan; apply sends exactly the reviewed request
file, including its expected snapshot digest; the CLI never refreshes it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import agent_extract as ax
from utils.graph_governance import parse_scope, parse_request, _ID


def render_markdown(body: dict) -> str:
    """Human review sheet: every edge move, dedupe, dropped self-loop and source."""
    plan, snap = body.get("plan") or {}, body.get("snapshot") or {}
    names = {n["uuid"]: n["props"].get("name") for n in snap.get("nodes", [])}
    lines = [f"# 节点治理预览：{plan.get('operation') or body.get('scope', {}).get('operation')}", "",
             f"- snapshot_sha256：`{snap.get('snapshot_sha256')}`", f"- plan_sha256：`{plan.get('plan_sha256')}`"]
    for item in body.get("resolution") or []:
        lines.append(f"- {item['role']}：{item['name']} → `{item['uuid']}`（{item['matched_name']}，{'/'.join(item['labels'])}）")
    lines += ["", "## 节点", "", "| 动作 | uuid | 名称 | 去掉标签 | 加上标签 | 写入 |", "|---|---|---|---|---|---|"]
    for node in plan.get("nodes", []):
        lines.append(f"| {node['action']} | `{node['uuid']}` | {node['name']} | {','.join(node['labels_remove'])} | "
                     f"{','.join(node['labels_add'])} | {json.dumps(node['set'], ensure_ascii=False)} |")
    lines += ["", "## 边", "", "| 动作 | 边 uuid | 原端点 | 新端点 | 边名 | fact | 背书数 → 之后 | 并入 |",
              "|---|---|---|---|---|---|---|---|"]
    for edge in plan.get("edges", []):
        fact = (edge.get("fact") or "").replace("|", "\\|")
        after = len(edge.get("episodes_after") or edge.get("episodes") or [])
        lines.append(f"| {edge['action']} | `{edge['uuid']}` | {' → '.join(map(str, edge['from_names']))} | "
                     f"{' → '.join(map(str, edge['to_names']))} | {edge.get('name')} | {fact} | "
                     f"{len(edge.get('episodes') or [])} → {after} | {edge.get('survivor_uuid') or ''} |")
    if plan.get("residual_edges"):
        lines += ["", f"留在原节点的边：{len(plan['residual_edges'])} 条"]
    lines += ["", "## 提及（MENTIONS）", "", "| 动作 | episode | 从 | 到 |", "|---|---|---|---|"]
    for m in plan.get("mentions", []):
        lines.append(f"| {m['action']} | `{m['episode_uuid']}` | {names.get(m.get('from'), m.get('from') or '')} | "
                     f"{names.get(m.get('to') or m.get('keep'), m.get('to') or m.get('keep') or '')} |")
    lines += ["", "## 受影响的来源", "", "| episode | 名称 | source_description | 模型 | run_id | 涉及边 |",
              "|---|---|---|---|---|---|"]
    for src in plan.get("sources", []):
        lines.append(f"| `{src['episode_uuid']}` | {src.get('name') or ('(缺失)' if src.get('missing') else '')} | "
                     f"{src.get('source_description') or ''} | {src.get('extractor_model') or ''} | "
                     f"{src.get('run_id') or ''} | {src['edges']} |")
    lines += ["", "## 汇总", "", "```json", json.dumps(plan.get("summary"), ensure_ascii=False, indent=2), "```"]
    if plan.get("warnings"):
        lines += ["", "## 提醒", ""] + [f"- **{w['code']}**：{w['message']}" for w in plan["warnings"]]
    return "\n".join(lines) + "\n"


def main(argv=None, *, http=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("preview", "apply", "status"))
    ap.add_argument("--file", help="JSON scope (nodes by name or uuid) or complete reviewed request")
    ap.add_argument("--governance-id", help="status: original governance_id")
    ap.add_argument("--out", help="save full response JSON (snapshot, plan, audit)")
    ap.add_argument("--request-out", help="preview: save unapproved request template bound to uuids and snapshot")
    ap.add_argument("--report", help="preview: save a Markdown review sheet of the complete plan")
    ap.add_argument("--url", default=ax.DEFAULT_URL)
    args = ap.parse_args(argv)
    post = http or ax._http
    token = ax.api_token()
    if args.command == "status":
        if not args.governance_id or not _ID.fullmatch(args.governance_id):
            ap.error("status requires a valid --governance-id")
        method, path, payload = "GET", "/api/graph/governance?" + urlencode({"governance_id": args.governance_id}), None
    else:
        if not args.file:
            ap.error("preview/apply require --file")
        payload = json.loads(Path(args.file).read_text())
        if args.command == "apply":
            parse_request(payload)
        else:
            parse_scope(payload, bound=False)
        method = "POST"
        path = "/api/graph/governance" + ("/preview" if args.command == "preview" else "")
    code, body = post(method, args.url.rstrip("/") + path, payload, token)
    output = {"http": code, **body}
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    if args.command == "preview" and code == 200:
        if args.request_out:
            original = json.loads(Path(args.file).read_text())
            template = {"governance_id": original.get("governance_id", ""), "reason": original.get("reason", ""),
                        "reviewer": original.get("reviewer", ""), **body["scope"],
                        "expected_snapshot_sha256": body["snapshot"]["snapshot_sha256"]}
            target = Path(args.request_out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(template, ensure_ascii=False, indent=2))
        if args.report:
            target = Path(args.report)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_markdown(body))
    summary = {k: v for k, v in output.items() if k not in ("snapshot", "plan", "governance", "detail", "scope")}
    if "snapshot" in body:
        summary["snapshot_sha256"] = body["snapshot"]["snapshot_sha256"]
    if "plan" in body:
        summary["plan_sha256"] = body["plan"]["plan_sha256"]
        summary["summary"] = body["plan"]["summary"]
        summary["warnings"] = [w["code"] for w in body["plan"]["warnings"]]
    if "detail" in body:
        detail = body["detail"] or {}
        summary["detail"] = detail.get("error_detail", detail) if isinstance(detail, dict) else detail
    if "governance" in body:
        summary["governance"] = {k: body["governance"].get(k) for k in
                                 ("governance_id", "state", "operation", "plan_sha256", "last_error")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if 200 <= code < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
