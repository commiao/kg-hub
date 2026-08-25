#!/usr/bin/env python3
"""统一 hook 清单与配置核对。

职责和期望项只维护在 config/hook_registry.json；本模块读取各工具真实配置，
生成可直接上报给拓扑面板的结构化快照。配置存在不等于实际执行，因此
configured / approval / runtime_evidence 分开表达，避免“配置在、数据断”。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "config" / "hook_registry.json"
PUSH_LOG = ROOT / "data" / ".push_hook.log"

EVENT_NORM = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "Stop": "stop",
}


def _expand(value: str) -> Path:
    return Path(value.replace("~", str(HOME), 1)) if value.startswith("~") else Path(value)


def _source_paths(source: dict) -> list[Path]:
    pattern = str(_expand(source["path"]))
    if source.get("kind") == "latest_glob":
        paths = [p for p in Path("/").glob(pattern.lstrip("/")) if p.is_file()]
        return [max(paths, key=lambda p: p.stat().st_mtime)] if paths else []
    path = Path(pattern)
    return [path] if path.is_file() else []


def _flatten_hooks(path: Path, scope: str) -> list[dict]:
    """兼容 Claude/Codex 的嵌套 hooks 与 Cursor 的扁平 hooks。"""
    try:
        hooks = json.loads(path.read_text(errors="replace")).get("hooks", {})
    except Exception as exc:  # noqa: BLE001
        return [{"event": "配置解析", "matcher": "", "command": "",
                 "source": str(path), "scope": scope, "group_index": 0,
                 "hook_index": 0, "parse_error": type(exc).__name__}]
    out = []
    for event, groups in hooks.items():
        for gi, group in enumerate(groups if isinstance(groups, list) else []):
            matcher = str(group.get("matcher") or "")
            nested = group.get("hooks")
            entries = nested if isinstance(nested, list) else [group]
            for hi, item in enumerate(entries):
                command = str(item.get("command") or item.get("prompt") or "")
                if not command:
                    continue
                out.append({
                    "event": event, "matcher": matcher, "command": command,
                    "source": str(path), "scope": scope,
                    "group_index": gi, "hook_index": hi,
                    "timeout": item.get("timeout"),
                    "async": bool(item.get("async", False)),
                })
    return out


def _action(command: str) -> str:
    m = re.search(r"\bhook\s+(?:claude-code|codex|cursor)\s+([a-z-]+)", command)
    if m:
        return m.group(1)
    if "version-check" in command:
        return "version-check"
    if "worker-service" in command and re.search(r"\bstart\b", command):
        return "start"
    if "kg_push_hook.py" in command:
        return "push"
    if "session_context.py" in command or re.search(r"task-hub\.py\s+session-start", command):
        return "session-context"
    if "heartbeat.py" in command or re.search(r"task-hub\.py\s+heartbeat", command):
        return "heartbeat"
    if "ops-hook-context.sh" in command:
        return "ops-context"
    return "custom"


def _component(command: str, components: list[dict]) -> dict | None:
    for comp in components:
        if any(p in command for p in comp.get("patterns", [])):
            return comp
    return None


def _codex_approved(config: str, item: dict) -> bool:
    event = EVENT_NORM.get(item["event"], re.sub(r"(?<!^)(?=[A-Z])", "_", item["event"]).lower())
    key = f"codex-hooks.json:{event}:{item['group_index']}:{item['hook_index']}"
    return key in config


def _push_last_seen() -> dict[str, datetime]:
    """按 fmt 取最后一次 OK；COUNT 不用于存活判断。"""
    out: dict[str, datetime] = {}
    if not PUSH_LOG.exists():
        return out
    for line in PUSH_LOG.read_text(errors="replace").splitlines():
        if " OK " not in f" {line} ":
            continue
        tm = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
        fm = re.search(r"fmt=(\S+)", line)
        if not tm or not fm:
            continue
        try:
            out[fm.group(1)] = datetime.fromisoformat(tm.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
    return out


def _runtime_evidence(tool: str, component: str, tool_seen: dict,
                      push_seen: dict[str, datetime]) -> dict:
    now = datetime.now(tz=timezone.utc)
    if component == "kg-push":
        dt = push_seen.get(tool)
        if dt:
            age = max(0, int((now - dt).total_seconds()))
            return {"kind": "hook-log", "last_seen": dt.isoformat(), "age_s": age,
                    "detail": "kg_push_hook OK 日志"}
        return {"kind": "none", "detail": "未见该工具的 PUSH 成功日志"}
    if component == "claude-mem" and tool in tool_seen:
        age = tool_seen[tool].get("idle_seconds")
        return {"kind": "downstream-data", "age_s": age,
                "detail": "claude-mem 中存在该工具观察；这是整条采集链证据，不代表每个事件都执行"}
    return {"kind": "none", "detail": "该 hook 尚无独立执行日志，只能确认配置"}


def collect(tool_seen: dict | None = None) -> list[dict]:
    registry = json.loads(REGISTRY_PATH.read_text())
    components = registry.get("components", [])
    actions = registry.get("actions", {})
    tool_seen = tool_seen or {}
    push_seen = _push_last_seen()
    inventories = []

    for tool in registry.get("tools", []):
        raw: list[dict] = []
        source_status = []
        for source in tool.get("sources", []):
            paths = _source_paths(source)
            source_status.append({
                "path": source["path"], "scope": source.get("scope", ""),
                "found": bool(paths), "resolved": [str(p) for p in paths],
            })
            for path in paths:
                raw.extend(_flatten_hooks(path, source.get("scope", "")))

        approval_text = ""
        approval_path = tool.get("approval")
        if approval_path:
            p = _expand(approval_path)
            if p.exists():
                approval_text = p.read_text(errors="replace")

        items = []
        found_components = set()
        for item in raw:
            if item.get("parse_error"):
                items.append({
                    **item, "id": f"parse:{item['source']}", "component": "unknown",
                    "label": "配置解析失败", "action": "parse-error",
                    "purpose": "无法读取 hook 配置", "configured": False,
                    "approval": "unknown", "state": "red",
                    "runtime_evidence": {"kind": "none", "detail": "配置无法解析"},
                })
                continue
            comp = _component(item["command"], components)
            cid = comp["id"] if comp else "custom"
            found_components.add(cid)
            action = _action(item["command"])
            approved = _codex_approved(approval_text, item) if approval_path else True
            state = "green" if approved else "red"
            items.append({
                **{k: v for k, v in item.items() if k != "command"},
                "id": f"{tool['id']}:{item['event']}:{item['group_index']}:{item['hook_index']}:{cid}",
                "component": cid,
                "label": comp["label"] if comp else "自定义 hook",
                "action": action,
                "purpose": actions.get(action) or (comp or {}).get("purpose") or "未登记用途",
                "configured": True,
                "approval": "approved" if approved else "missing",
                "state": state,
                "runtime_evidence": _runtime_evidence(
                    tool["id"], cid, tool_seen, push_seen),
            })

        for expected in tool.get("expected", []):
            cid = expected["component"]
            if cid in found_components:
                if expected.get("scope") == "user" and not any(
                        h.get("component") == cid and h.get("configured")
                        and h.get("scope") == "用户" for h in items):
                    # “某个项目里配了”不能冒充“这个工具全局接入”。Cursor 曾正是
                    # workspace_cursor 有 hook、其它仓库无 hook，导致新会话静默缺失。
                    for hook in items:
                        if hook.get("component") == cid and hook.get("configured"):
                            hook["state"] = "amber"
                            hook["coverage"] = "仅项目级；其它 workspace 不生效"
                continue
            comp = next((c for c in components if c["id"] == cid), {"label": cid, "purpose": ""})
            required = bool(expected.get("required"))
            items.append({
                "id": f"{tool['id']}:missing:{cid}", "event": "—", "matcher": "",
                "source": "注册表期望项", "scope": "", "component": cid,
                "label": comp["label"], "action": "missing",
                "purpose": comp.get("purpose", ""), "configured": False,
                "approval": "not-configured", "state": "red" if required else "grey",
                "runtime_evidence": {"kind": "none", "detail": "未找到匹配配置"},
            })

        states = [x["state"] for x in items]
        overall = ("red" if "red" in states else "amber" if "amber" in states
                   else "green" if items and any(x["configured"] for x in items) else "grey")
        inventories.append({
            "tool": tool["id"], "label": tool["label"], "state": overall,
            "note": tool.get("note", ""), "sources": source_status, "hooks": items,
            "summary": {
                "total": len(items),
                "configured": sum(1 for x in items if x["configured"]),
                "missing": sum(1 for x in items if not x["configured"]),
                "unapproved": sum(1 for x in items if x["approval"] == "missing"),
                "limited_scope": len({
                    x["component"] for x in items if x.get("coverage")
                }),
            },
        })
    return inventories


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
