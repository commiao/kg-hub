#!/usr/bin/env python3
"""Read-only post-upgrade acceptance: native hook trust + isolated default drill.

The scheduled entry watches the effective release/hooks, never upgrades or
restarts anything. --force rechecks; this entry deliberately has no --live.
Failures use their own durable notification queue, never watchdog.json.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from .codex_hooks import read_hooks
except ImportError:
    from codex_hooks import read_hooks


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inspect_installation(home, code):
    """Only active native sources; never guess the latest cached plugin."""
    reasons, hooks = [], []
    try:
        native = read_hooks(home)
        for hook in native["hooks"]:
            if (hook["key"].split("@")[0] == "claude-mem"
                    or "claude-mem" in str(hook.get("pluginId") or "")
                    or "claude-mem" in hook.get("command", "")):
                hooks.append({k: hook[k] for k in
                              ("key", "currentHash", "trustStatus", "sourcePath", "enabled")})
        hooks.sort(key=lambda h: h["key"])
        if not hooks:
            reasons.append("claude-mem-hooks-missing")
        if any(not h["enabled"] or h["trustStatus"] not in ("trusted", "managed") for h in hooks):
            reasons.append("native-hook-trust-unhealthy")
    except Exception as exc:
        reasons.append("native-inspection-" + type(exc).__name__)
    release_file = home / ".local/share/claude-mem/current/RELEASE.json"
    release = None
    if release_file.exists():
        try:
            release = json.loads(release_file.read_text())["commit"]
        except Exception as exc:
            reasons.append("release-metadata-" + type(exc).__name__)
    sources = {}
    for source in sorted({h["sourcePath"] for h in hooks}):
        try:
            path = Path(source)
            sources[source] = hashlib.sha256(path.read_bytes()).hexdigest()
            package = path.parent.parent / "package.json"
            if package.is_file():
                sources[str(package)] = hashlib.sha256(package.read_bytes()).hexdigest()
        except Exception as exc:
            reasons.append("plugin-source-" + type(exc).__name__)
    runner_hashes = {}
    for name in ("drill_codex_hooks.py", "codex_hook_drill_runner.py", "claude_mem_upgrade_acceptance.py"):
        try:
            runner_hashes[name] = hashlib.sha256((code / name).read_bytes()).hexdigest()
        except Exception as exc:
            reasons.append("drill-source-" + type(exc).__name__)
    evidence = {"release": release, "hooks": hooks, "sources": sources,
                "runner_hashes": runner_hashes, "inspection_errors": sorted(set(reasons))}
    return digest(evidence), evidence, sorted(set(reasons))


def run_default_drill(code, runs):
    """Run exactly the default entry; reject any receipt claiming live delivery."""
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    before = set(runs.glob("*/receipt.json"))
    try:
        result = subprocess.run([sys.executable, str(code / "drill_codex_hooks.py"),
                                 "--output-dir", str(runs)],
                                capture_output=True, text=True, timeout=150, check=False)
    except Exception as exc:
        return {"ok": False, "reason": "drill-" + type(exc).__name__}
    created = set(runs.glob("*/receipt.json")) - before
    if len(created) != 1:
        return {"ok": False, "reason": "drill-receipt-missing", "exit_code": result.returncode}
    path = created.pop()
    try:
        receipt = json.loads(path.read_text())
        messages = receipt.get("messages", [])
        safe = (receipt.get("mode") == "dry-run" and receipt.get("production_state_used") is False
                and len(messages) == 2 and all(m.get("simulated") is True
                                               and m.get("delivered") is False for m in messages)
                and receipt.get("duplicate_fault_suppressed") is True
                and receipt.get("duplicate_recovery_suppressed") is True
                and receipt.get("final_pending") == {})
        ok = result.returncode == 0 and receipt.get("ok") is True and safe
        return {"ok": ok, "reason": None if ok else "drill-failed-or-unsafe-receipt",
                "receipt": str(path), "exit_code": result.returncode}
    except Exception as exc:
        return {"ok": False, "reason": "drill-receipt-" + type(exc).__name__, "receipt": str(path)}


def notify_failure(home, message):
    """Use the installed feishu-notify skill, with a named channel, no secrets in argv/logs."""
    script = home / ".claude/skills/feishu-notify/scripts/send.py"
    try:
        result = subprocess.run([sys.executable, str(script), message, "--webhook", "kg-hub",
                                 "--title", "🔴 kg-hub claude-mem 升级验收失败"],
                                capture_output=True, timeout=20, check=False)
        # Never print bundled sender stderr: transport failures may include the URL.
        return result.returncode == 0
    except Exception:
        return False


def save_state(path, state):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temp.replace(path)


def accept(home, code, state_dir, *, force=False):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    path = state_dir / "acceptance.json"
    with (state_dir / ".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75, {"status": "busy"}
        # Unreadable state is fatal. Preserve it; never silently erase a pending alert.
        state = json.loads(path.read_text()) if path.exists() else {}
        fingerprint, evidence, reasons = inspect_installation(home, code)
        if state.get("fingerprint") != fingerprint:
            state.update(pending_alert=None, notified_fingerprint=None)
        if (not force and not reasons and state.get("status") == "passed"
                and state.get("fingerprint") == fingerprint and not state.get("pending_alert")):
            return 0, {"status": "unchanged", "fingerprint": fingerprint}
        drill = run_default_drill(code, state_dir / "runs")
        if not drill["ok"]:
            reasons.append(drill["reason"])
        passed = not reasons
        state.update(schema_version=1, fingerprint=fingerprint, evidence=evidence,
                     checked_at=time.time(), status="passed" if passed else "failed",
                     reasons=reasons, drill=drill)
        if passed:
            # Obsolete failed checks must not be delivered after acceptance succeeds.
            state.update(pending_alert=None, notified_fingerprint=None)
        elif state.get("notified_fingerprint") != fingerprint:
            pending = state.get("pending_alert") or {}
            attempts = pending.get("attempts", 0) if pending.get("fingerprint") == fingerprint else 0
            state["pending_alert"] = {"fingerprint": fingerprint, "attempts": attempts,
                                      "reasons": reasons, "receipt": drill.get("receipt")}
        save_state(path, state)  # Checkpoint before notification; transport errors remain pending.
        pending = state.get("pending_alert")
        if pending:
            pending["attempts"] += 1
            save_state(path, state)
            message = ("升级后的自动验收未通过（本消息不是两条 --live 演练通知）。\n"
                       + "版本/定义指纹：" + fingerprint[:16]
                       + "\n原因：" + ", ".join(reasons)
                       + "\n默认预演回执：" + str(drill.get("receipt") or "未生成")
                       + "\n验收状态：" + str(path)
                       + "\n未批准/hash 变化请在 Codex /hooks 审阅；不要绕过批准。"
                         "本验收不修改真实 hooks、采集数据或生产 watchdog 状态。")
            if notify_failure(home, message):
                state["notified_fingerprint"] = fingerprint
                state["pending_alert"] = None
            save_state(path, state)
        return (0 if passed else 1), state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="忽略成功缓存，立即重新验收；仍不发送演练通知")
    parser.add_argument("--state-dir", type=Path, help="独立验收状态目录，默认 ~/.kg-hub/state/claude-mem-upgrade-acceptance")
    args = parser.parse_args(argv)
    home, code = Path.home(), Path(__file__).resolve().parent
    state_dir = args.state_dir or home / ".kg-hub/state/claude-mem-upgrade-acceptance"
    try:
        rc, state = accept(home, code, state_dir, force=args.force)
    except Exception as exc:
        # State/storage faults must also speak up; keep the original file untouched.
        print("upgrade acceptance controller failed: " + type(exc).__name__, file=sys.stderr)
        notify_failure(home, "升级验收控制器失败：" + type(exc).__name__
                       + "；原有状态保留，请检查 " + str(state_dir))
        return 2
    print(json.dumps({"status": state["status"], "fingerprint": state.get("fingerprint"),
                      "state_file": str(state_dir / "acceptance.json"),
                      "notification_pending": bool(state.get("pending_alert"))}, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
