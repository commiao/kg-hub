"""Durable, single-owner Codex worker supervision. Never submits or kills work.

Native turns may finish before quota. Resume the SAME session after its terminal
receipt, rather than relying on an optional ps command or an idle chat to notice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from tools import agent_extract as ax, extract_campaign as ec, claim_registry, nas_gate
from utils import process_receipts as receipts


def completed(c: dict, d: Path, model: str) -> int:
    batches = {b["id"]: b for b in c["batches"] if b["model"] == model}
    latest = {}
    log = d / "log.jsonl"
    if log.exists():
        raw = log.read_text()
        lines = raw.splitlines()
        if raw and not raw.endswith("\n"):
            lines = lines[:-1]  # another submit may still be appending its final record
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            b = batches.get(row.get("batch"))
            if (b and row.get("model") == model and row.get("run_id") == c["models"][model]["run_id"]
                    and row.get("executor") == c["executor"] and row.get("id") in b["ids"]
                    and row["id"] not in (b.get("released_ids") or [])):
                latest[(b["id"], row["id"])] = row
    return sum(row.get("status") == "ok" or ax.confirmed_empty(row) for row in latest.values())


def events(path: Path) -> dict:
    """Read identity and terminal evidence without loading a huge native transcript."""
    if not path.is_file():
        return {}
    result = {}
    with path.open("rb") as stream:
        first = stream.readline()
        try:
            row = json.loads(first)
            if row.get("type") == "thread.started":
                result["session"] = row.get("thread_id")
        except (ValueError, UnicodeError):
            pass
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 262144))
        for line in stream:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if row.get("type") in ("turn.completed", "turn.failed", "error"):
                result["terminal"] = row["type"]
            item = row.get("item", {})
            if (row.get("type") == "item.completed" and item.get("type") == "command_execution"
                    and item.get("exit_code") in (75, 76)
                    and re.search(r"(?:/tools/extract|extract_cli\.py)[\"']?\s+campaign\s+submit\b", item.get("command", ""))):
                result["hard_stop"] = item["exit_code"]
            if item.get("type") == "agent_message":
                try:
                    report = json.loads(item.get("text", ""))
                except ValueError:
                    continue
                if isinstance(report, dict) and report.get("recovery_kind"):
                    result["recovery_kind"] = report["recovery_kind"]
    return result


def native_args(c: dict, model: str, session: str | None) -> list[str]:
    # Reuse the launch contract's rejection of model/executor overrides.
    ec.launch_command("codex", c["id"], model, cfg=c)
    if c["executor"] != "codex":
        raise ValueError("managed supervision currently supports the codex executor")
    settings = ec.runtime_settings(c)
    args = ["codex", "exec", "--json", "-m", model, "-C", str(ec.workspace_dir()),
            "--ignore-user-config", "--approve-for-me", "--skip-git-repo-check",
            "-c", "features.unified_exec=false", "-c", "sandbox_workspace_write.network_access=true"]
    if settings["effort"] is not None:
        args += ["-c", "model_reasoning_effort=" + settings["effort"]]
    for directory in (ax.state_dir(), claim_registry.claims_dir(), Path(nas_gate.load_settings()["lock_dir"]).expanduser()):
        args += ["--add-dir", str(directory)]
    if session:
        args += ["resume", session]
    prompt = ec.brief(c, model)
    prompt += ("\n由公共supervise管理本执行线；先核对原批次与operation-status，复用未完成批次。"
               "不要重复领取或重交终态不明的请求。可选诊断不可用时读持久回执；真实审批拒绝不得换路径重试。"
               "若因真实审批拒绝、NAS75/76、基础设施失败、业务权限拒绝或提交终态不明结束，"
               "最终JSON的recovery_kind分别写approval_rejected/nas_refused/infrastructure_failed/"
               "business_permission_denied/uncertain_submit。正常完成或只因诊断不可用写none/diagnostic_unavailable。"
               "继续原配额；仍须逐条质量审核、整批预览、正式入库及随机复查，不新增冒烟或准入调用。")
    return args + [prompt]


def run(cid: str, model: str, *, session: str | None = None, adopt_exit: Path | None = None,
        adopt_events: Path | None = None, poll: float = 15, max_stalled: int = 3, sleep=time.sleep,
        spawn=subprocess.Popen, preflight=None) -> dict:
    if poll <= 0 or max_stalled < 1 or bool(adopt_exit) != bool(adopt_events):
        raise ValueError("positive polling/stall limit and paired adoption receipts required")
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    native_args(c, model, session)
    ec.tracking_guard()
    root = d / "supervisors" / ec.slug(model)
    native = root / "native"
    with receipts.attempt(root, kind="supervisor", identity={"campaign": cid, "model": model},
                          allow_abandoned=True) as update:
        saved = receipts.read(root / "worker.json")
        if session and saved.get("session") and session != saved["session"]:
            raise ValueError("resume session differs from managed worker identity")
        session = saved.get("session") or session
        baseline = completed(c, d, model)
        if (baseline < c["quota"] and not saved and adopt_exit is None
                and any(b["model"] == model for b in c["batches"])):
            raise ValueError("existing model history requires its unique exit/event receipts; do not duplicate an unmanaged worker")
        stalled = 0
        child = None
        while True:
            c = ec.load(d)
            count = completed(c, d, model)
            if adopt_exit is not None:
                update(phase="waiting_existing_worker", success=count, session=session)
                if not adopt_exit.is_file():
                    sleep(poll)
                    continue
                terminal = receipts.read(adopt_exit)
                outcome = events(adopt_events)
                if outcome.get("hard_stop"):
                    update(phase="coordinator_required", exit_code=2, reason="nas_gate_refused", business_exit_code=outcome["hard_stop"])
                    return {"model": model, "state": "blocked", "reason": "nas_gate_refused",
                            "business_exit_code": outcome["hard_stop"]}
                if (terminal.get("exit_code") != 0 or not terminal.get("ended_at") or outcome.get("session") != session
                        or outcome.get("terminal") != "turn.completed"):
                    update(phase="coordinator_required", exit_code=2, reason="adopted_worker_terminal_unverified")
                    return {"model": model, "state": "blocked", "reason": "adopted_worker_terminal_unverified"}
                adopt_exit = None
                if outcome.get("recovery_kind") not in (None, "none", "diagnostic_unavailable"):
                    update(phase="coordinator_required", exit_code=2, reason=outcome["recovery_kind"])
                    return {"model": model, "state": "blocked", "reason": outcome["recovery_kind"]}
            waiting = False
            for batch in (b for b in c["batches"] if b["model"] == model and b["status"] not in ("submitted", "released")):
                op = ec.cmd_operation_status(cid, batch["id"])
                if op["action"] == "wait_for_original":
                    waiting = True
                elif op["action"] == "legacy_terminal_unverified" and batch["status"] == "dry_run":
                    update(phase="coordinator_required", exit_code=2, reason="legacy_submit_terminal_unverified", batch=batch["id"])
                    return {"model": model, "state": "blocked", "reason": "legacy_submit_terminal_unverified"}
                elif op["action"] in ("reconcile_service_before_replay", "stop_nas_gate_refusal") or (
                        op["operation"].get("error_type") == "PermissionError"):
                    update(phase="coordinator_required", exit_code=2, reason=op["action"], batch=batch["id"])
                    return {"model": model, "state": "blocked", "operation": op}
            state = receipts.status(native)
            if waiting or state["state"] == "running":
                update(phase="waiting_original_operation", success=count, session=session)
                sleep(poll)
                continue
            if state["state"] in ("uncertain", "failed"):
                update(phase="coordinator_required", exit_code=2, reason="native_terminal_unverified")
                return {"model": model, "state": "blocked", "reason": "native_terminal_unverified"}
            if child is not None:
                if child.poll() is None:
                    update(phase="waiting_native_exit", success=count, session=session)
                    sleep(poll)
                    continue
                child.wait()
                child = None
            if saved.get("events"):
                outcome = events(Path(saved["events"]))
                if outcome.get("hard_stop"):
                    update(phase="coordinator_required", exit_code=2, reason="nas_gate_refused", business_exit_code=outcome["hard_stop"])
                    return {"model": model, "state": "blocked", "reason": "nas_gate_refused",
                            "business_exit_code": outcome["hard_stop"]}
                if not outcome.get("session") or session and outcome["session"] != session:
                    update(phase="coordinator_required", exit_code=2, reason="native_session_mismatch")
                    return {"model": model, "state": "blocked", "reason": "native_session_mismatch"}
                session = outcome.get("session") or session
                if (state.get("exit_code", 0) != 0 or outcome.get("terminal") != "turn.completed"
                        or outcome.get("recovery_kind") not in (None, "none", "diagnostic_unavailable")):
                    update(phase="coordinator_required", exit_code=2, reason="native_worker_blocked")
                    return {"model": model, "state": "blocked", "reason": "native_worker_blocked"}
                stalled = stalled + 1 if count <= baseline else 0
                baseline = count
                update(stalled_turns=stalled, success=count, session=session)
                if stalled >= max_stalled:
                    update(phase="coordinator_required", exit_code=2, reason="no_formal_progress", success=count)
                    return {"model": model, "state": "blocked", "reason": "no_formal_progress"}
            if count >= c["quota"]:
                update(phase="quota_complete", success=count)
                return {"model": model, "state": "complete", "success": count}
            flight = (preflight or ax.runtime_preflight)(os.environ.get("KG_HUB_URL", ax.DEFAULT_URL), ax.api_token())
            if not flight.get("ok"):
                update(phase="coordinator_required", exit_code=2, reason="preflight_failed")
                return {"model": model, "state": "blocked", "reason": "preflight_failed", "preflight": flight}
            tag = uuid.uuid4().hex
            output, error = root / (tag + ".events.jsonl"), root / (tag + ".stderr.log")
            saved = {"session": session, "events": str(output), "stderr": str(error)}
            receipts.write(root / "worker.json", saved)
            entry = Path(os.environ.get("KG_HUB_CODE", "~/.local/share/kg-hub/current")).expanduser() / "tools/extract"
            args = [str(entry), "runtime", "exec", "--proxy-mode", ec.runtime_settings(c)["proxy_mode"],
                    "--receipt-dir", str(native), "--", *native_args(c, model, session)]
            out_fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            err_fd = os.open(error, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(out_fd, "wb") as out, os.fdopen(err_fd, "wb") as err:
                child = spawn(args, cwd=ec.workspace_dir(), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                              start_new_session=True)
            # A just-spawned wrapper has not acquired its lease yet. Do not
            # interpret the previous terminal receipt as this child's result.
            update(phase="starting_native", events=str(output), success=count, session=session)
            while receipts.status(native).get("attempt") == state.get("attempt"):
                if child.poll() is not None:
                    update(phase="coordinator_required", exit_code=2, reason="native_wrapper_start_failed")
                    return {"model": model, "state": "blocked", "reason": "native_wrapper_start_failed"}
                sleep(poll)
            sleep(poll)

