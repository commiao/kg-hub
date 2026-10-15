"""Isolated drill worker, streamed to a fresh process in the deployed watchdog.

Never call watchdog.main(): only synthetic snapshots and private state are used.
The Mac entry point is drill_codex_hooks.py; this file needs deployed dependencies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

KIND = "codex_hooks_unhealthy"
RECEIPT_PREFIX = "HOOK_DRILL_RECEIPT "


class DrillFailure(RuntimeError):
    """A safe, fixed diagnostic, never a transport exception message."""


@contextmanager
def replaced(obj, **values):
    previous = {key: getattr(obj, key) for key in values}
    try:
        for key, value in values.items():
            setattr(obj, key, value)
        yield
    finally:
        for key, value in previous.items():
            setattr(obj, key, value)


def run_drill(watchdog, drill_id, *, live=False):
    """Return a secret-free receipt, including failures. No automatic resend."""
    if not re.fullmatch(r"HOOK-DRILL-[0-9]{8}-[0-9]{6}-[a-f0-9]{12}", drill_id):
        raise ValueError("invalid drill id")
    root = Path(tempfile.mkdtemp(prefix="codex-hook-drill-", dir="/tmp"))
    root.chmod(0o700)
    state_path, log_path = root / "watchdog.json", root / "alerts.log"
    proof = {
        "schema_version": 1, "drill_id": drill_id,
        "mode": "live" if live else "dry-run", "ok": False,
        "production_state_used": False, "state_directory": str(root),
        "production_watchdog_file": str(Path(watchdog.__file__).resolve()),
        "production_watchdog_sha256": hashlib.sha256(Path(watchdog.__file__).read_bytes()).hexdigest(),
        "messages": [], "stage": "prepare", "final_pending": {},
    }
    messages = proof["messages"]
    original_save, original_log = watchdog.save_state, watchdog.write_alert_log
    original_emit, original_post = watchdog.emit_alert, watchdog.httpx.post
    original_state = Path(watchdog.STATE_FILE).resolve()

    def guard():
        if (watchdog.STATE_DIR != root or watchdog.STATE_FILE != state_path
                or watchdog.ALERTS_LOG != log_path or state_path.resolve() == original_state):
            raise DrillFailure("state isolation lost")

    def save(state):
        guard()
        original_save(state)

    def log(line):
        guard()
        original_log(line)

    def no_http(*args, **kwargs):
        raise DrillFailure("network forbidden in dry-run")

    def record_post(*args, **kwargs):
        # Do not retain request URLs, exception messages, or response bodies.
        receipt = messages[-1]
        try:
            response = original_post(*args, **kwargs)
            receipt["http_status"] = response.status_code
            try:
                payload = response.json()
                code = payload.get("code", payload.get("StatusCode"))
                receipt["feishu_code"] = code if type(code) is int else None
            except Exception:
                receipt["response_error"] = "invalid-json"
            return response
        except Exception as exc:
            receipt["transport_error"] = type(exc).__name__
            raise

    def emit(severity, kind, message, attempt=1):
        guard()
        if kind != KIND or severity not in ("fire", "clear") or attempt != 1:
            raise DrillFailure("unexpected alert or retry")
        if len(messages) >= 2:
            raise DrillFailure("unexpected duplicate delivery")
        receipt = {"severity": severity, "delivered": False, "simulated": not live,
                   "at": datetime.now(timezone.utc).isoformat()}
        messages.append(receipt)
        delivered = original_emit(severity, "【演练】" + kind, message, attempt=attempt)
        receipt["delivered"] = bool(live and delivered is True)
        receipt["accepted"] = delivered is True
        return delivered

    def cycle(hooks, expected_bad):
        guard()
        previous = watchdog.load_state()
        sample = {"_host": "DRILL_ONLY（演练）", "_age_s": 0, "blockers": [],
                  "hook_inventory": [{"tool": "codex", "hooks": copy.deepcopy(hooks)}]}
        decision = watchdog.judge_snapshots([sample], {}, None)
        anomalies, details, counters = {}, {}, {}
        watchdog.apply_codex_hook_decision(
            decision, previous.get("anomalies", {}), anomalies, details,
            previous.get("counters", {}), counters)
        if anomalies.get(KIND) is not expected_bad:
            raise DrillFailure("deployed rule did not recognize drill snapshot")
        if expected_bad and (len(decision.codex_hooks) != 2 or len(decision.codex_hook_ids) != 2):
            raise DrillFailure("both missing approval and changed hash must be detected")
        if expected_bad:
            # Keep the actual rule's key/hash diagnostic, remove its real repair advice.
            details[KIND] = ("【模拟异常告警｜无需操作】\n演练编号：" + drill_id
                + "\n虚拟的未批准 / hash 失效；真实 hooks、采集和生产告警状态未改动。\n"
                + "\n".join(decision.codex_hooks)
                + "\n这是演练，请勿实际修改 hook。")
        else:
            details[KIND + ":clear"] = ("【模拟恢复通知｜演练结束】\n演练编号：" + drill_id
                + "\n虚拟的未批准 / hash 失效已在临时状态中恢复。"
                  "真实采集未作变更，本消息仅验证恢复通知投递。")
        watchdog.deliver_alerts(previous, anomalies, details, [], hook_ids=decision.codex_hook_ids)
        current = watchdog.load_state()
        proof["final_pending"] = current.get("pending_alerts", {})
        if proof["final_pending"]:
            raise DrillFailure("delivery unacknowledged; no automatic retry or recovery")
        if current.get("anomalies", {}).get(KIND) is not expected_bad:
            raise DrillFailure("isolated state did not persist verdict")

    faults = [
        {"id": "DRILL_ONLY:PostCompact", "key": "DRILL_ONLY:claude-mem:post_compact:0:0",
         "event": "PostCompact", "state": "red", "approval": "missing",
         "current_hash": "sha256:" + "0" * 64, "status_detail": "模拟升级后 hook 未批准"},
        {"id": "DRILL_ONLY:PostToolUse", "key": "DRILL_ONLY:claude-mem:post_tool_use:0:0",
         "event": "PostToolUse", "state": "red", "approval": "modified",
         "current_hash": "sha256:" + "1" * 64, "status_detail": "模拟升级后 hook hash 失效"},
    ]
    try:
        with replaced(watchdog, STATE_DIR=root, STATE_FILE=state_path, ALERTS_LOG=log_path,
                      save_state=save, write_alert_log=log, emit_alert=emit):
            guard()
            webhook = "DRY_RUN_ONLY"
            if live:
                config = watchdog.load_notify_config()
                if config.get("enabled") is False:
                    raise DrillFailure("production notification channel disabled")
                webhook = str(config.get("feishu_webhook") or watchdog.FEISHU_WEBHOOK).strip()
                if not webhook:
                    raise DrillFailure("Feishu webhook missing; local fallback forbidden")
            with replaced(watchdog, FEISHU_WEBHOOK=webhook), \
                 replaced(watchdog.httpx, post=record_post if live else no_http), \
                 replaced(watchdog, **({} if live else {"send_feishu": lambda text: True})):
                save({"anomalies": {}, "pending_alerts": {}, "last_run": watchdog.now_iso()})
                proof["stage"] = "fire"
                cycle(faults, True)
                proof["stage"] = "fault-dedup"
                cycle(faults, True)
                if len(messages) != 1:
                    raise DrillFailure("fault deduplication failed")
                proof["duplicate_fault_suppressed"] = True
                if live:
                    # Stay below group-bot burst limits; do not share any production timer.
                    time.sleep(6)
                recovered = [dict(h, state="green", approval="approved") for h in faults]
                proof["stage"] = "clear"
                cycle(recovered, False)
                proof["stage"] = "recovery-dedup"
                cycle(recovered, False)
                if [m["severity"] for m in messages] != ["fire", "clear"]:
                    raise DrillFailure("recovery or deduplication failed")
                if live and not all(m["delivered"] and 200 <= m.get("http_status", 0) < 300
                                    and m.get("feishu_code") == 0 for m in messages):
                    raise DrillFailure("Feishu receipt not confirmed")
                proof["duplicate_recovery_suppressed"] = True
                proof.update(ok=True, stage="complete")
    except Exception as exc:
        # Exception text can contain a webhook token. Persist only its type.
        proof["error_type"] = type(exc).__name__
        if isinstance(exc, DrillFailure):
            proof["error"] = str(exc)
    finally:
        if state_path.exists():
            proof["final_pending"] = json.loads(state_path.read_text()).get("pending_alerts", {})
        proof["finished_at"] = datetime.now(timezone.utc).isoformat()
        receipt_path = root / "receipt.json"
        receipt_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        for path in root.iterdir():
            path.chmod(0o600)
    return proof


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill-id", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    from tools import watchdog
    proof = run_drill(watchdog, args.drill_id, live=args.live)
    print(RECEIPT_PREFIX + json.dumps(proof, ensure_ascii=False))
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
