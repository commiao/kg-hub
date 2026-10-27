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
import threading
from concurrent.futures import ThreadPoolExecutor

from tools import agent_extract as ax, extract_campaign as ec, claim_registry, nas_gate
from utils import process_receipts as receipts

CAPACITY_MESSAGE = 'Selected model is at capacity. Please try a different model.'
CAPACITY_WAIT_SECONDS = 300
MAX_CAPACITY_RETRIES = 3


def capacity_terminal(outcome, terminal):
    return (terminal.get('state') == 'completed' and terminal.get('exit_code') == 1
            and bool(terminal.get('ended_at')) and outcome.get('terminal') == 'turn.failed'
            and outcome.get('capacity_refused') is True and not outcome.get('hard_stop')
            and not outcome.get('approval_refused')
            and normal_recovery(outcome.get('recovery_kind')))


def normal_recovery(kind):
    return kind in (None, 'none', 'diagnostic_unavailable', 'migration_drained')


def completed(c: dict, d: Path, model: str, slot: int | None = None) -> int:
    batches = {b["id"]: b for b in c["batches"] if b["model"] == model
               and (slot is None or ec.batch_slot(c, b) == slot)}
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
    """Stream identity and all hard stops without loading the transcript in memory."""
    if not path.is_file():
        return {}
    result = {}
    native_errors = []
    with path.open("rb") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if row.get("type") == "thread.started":
                result["session"] = row.get("thread_id")
            if row.get("type") in ("turn.completed", "turn.failed", "error"):
                result["terminal"] = row["type"]
            if row.get('type') == 'error':
                native_errors.append(row.get('message'))
            elif row.get('type') == 'turn.failed':
                error = row.get('error')
                native_errors.append(error.get('message') if isinstance(error, dict) else None)
            item = row.get("item", {})
            if (item.get('type') == 'command_execution' and item.get('exit_code')
                    and 'This action was rejected due to unacceptable risk' in str(item.get('aggregated_output', ''))):
                result['approval_refused'] = True
            if (row.get("type") == "item.completed" and item.get("type") == "command_execution"
                    and item.get("exit_code") in (75, 76)
                    and re.search(r"(?:/tools/extract|extract_cli\.py)[\"']?\s+campaign\s+submit\b", item.get("command", ""))):
                result["hard_stop"] = item["exit_code"]
                result.setdefault("hard_stops", []).append({"id": item.get("id"),
                    "command": item.get("command", ""), "exit_code": item["exit_code"]})
            if item.get("type") == "agent_message":
                try:
                    report = json.loads(item.get("text", ""))
                except ValueError:
                    continue
                if isinstance(report, dict) and report.get("recovery_kind"):
                    result["recovery_kind"] = report["recovery_kind"]
    if native_errors:
        result['capacity_refused'] = all(message == CAPACITY_MESSAGE for message in native_errors)
    return result


def bind_slot_session(d: Path, model: str, slot: int | None, session: str | None) -> None:
    if slot is None or session is None:
        return
    with ec.locked(d):
        c = ec.load(d)
        bindings = c.setdefault("slot_sessions", {})
        own = bindings.setdefault(model, {})
        if own.get(str(slot)) not in (None, session):
            raise ValueError("slot session differs from its recorded owner")
        for other_model, slots in bindings.items():
            for other_slot, existing in slots.items():
                if existing == session and (other_model, other_slot) != (model, str(slot)):
                    raise ValueError("native session already belongs to another slot")
        own[str(slot)] = session
        ec.save(d, c)


def native_args(c: dict, model: str, session: str | None, slot: int | None = None) -> list[str]:
    # Reuse the launch contract's rejection of model/executor overrides.
    ec.launch_command("codex", c["id"], model, cfg=c, slot=slot)
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
    prompt = ec.brief(c, model, slot)
    prompt += ("\n由公共supervise管理本执行线；先核对原批次与operation-status，复用未完成批次。"
               "不要重复领取或重交终态不明的请求。可选诊断不可用时读持久回执；真实审批拒绝不得换路径重试。"
               "若因真实审批拒绝、NAS75/76、基础设施失败、业务权限拒绝或提交终态不明结束，"
               "最终JSON的recovery_kind分别写approval_rejected/nas_refused/infrastructure_failed/"
               "business_permission_denied/uncertain_submit。正常完成或只因诊断不可用写none/diagnostic_unavailable。"
               "继续原配额；仍须逐条质量审核、整批预览、正式入库及随机复查，不新增冒烟或准入调用。")
    prompt += ("\n本会话只拥有冻结绑定的原模型/槽位，不能接手其他槽位的批次或已提交来源返工。"
               "收到跨槽任务先保存scope_mismatch并交协调器，不尝试campaign review或repair apply，"
               "不清除或改写KG_HUB_CAMPAIGN_*环境绑定来绕过拒绝。"
               "逐条读完整正文和lookahead，针对当前一条明确写出四项语义判断及具体理由/引文；"
               "禁止approve()默认全true、循环将pending改ok或以关键词/引文命中替代判断。"
               "整批预览的每个复用候选也须分别判读，禁止模板或脚本批量确认候选。"
               "可原样合并已逐条明确保存的决定，合并阶段不能生成新的通过。"
               "发现批量默认批准即停止新领取并如实报告quality_process_violation，不把格式检查当语义正确。")
    return args + [prompt]


def run(cid: str, model: str, *, session: str | None = None, adopt_exit: Path | None = None,
        adopt_events: Path | None = None, poll: float = 15, max_stalled: int = 3, sleep=time.sleep,
        spawn=subprocess.Popen, preflight=None, slot: int | None = None, stop=None) -> dict:
    if poll <= 0 or max_stalled < 1 or bool(adopt_exit) != bool(adopt_events):
        raise ValueError("positive polling/stall limit and paired adoption receipts required")
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    slot = ec.worker_slot(c, model, slot)
    native_args(c, model, session, slot)
    ec.tracking_guard()
    root = ec.supervisor_dir(d, model, slot)
    native = root / "native"
    identity = {"campaign": cid, "model": model, **({"slot": slot} if slot is not None else {})}
    with receipts.attempt(root, kind="supervisor", identity=identity,
                          allow_abandoned=True) as update:
        saved = receipts.read(root / "worker.json")
        if session and saved.get("session") and session != saved["session"]:
            raise ValueError("resume session differs from managed worker identity")
        session = saved.get("session") or session
        capacity_retries = int(saved.get('capacity_retries', 0))
        bind_slot_session(d, model, slot, session)
        baseline = completed(c, d, model, slot)
        if (baseline < c["quota"] and not saved and adopt_exit is None
                and any(b["model"] == model and (slot is None or ec.batch_slot(c, b) == slot) for b in c["batches"])):
            raise ValueError("existing model history requires its unique exit/event receipts; do not duplicate an unmanaged worker")
        stalled = 0
        child = None
        while True:
            c = ec.load(d)
            count = completed(c, d, model)
            own_count = completed(c, d, model, slot)
            if adopt_exit is not None:
                update(phase="waiting_existing_worker", success=count, session=session)
                if not adopt_exit.is_file():
                    sleep(poll)
                    continue
                terminal = receipts.read(adopt_exit)
                outcome = events(adopt_events)
                from tools.campaign_recovery import stops_reconciled
                cleared = bool(outcome.get("hard_stop")) and stops_reconciled(c, d, model, adopt_events, outcome)
                if outcome.get("hard_stop") and not cleared:
                    update(phase="coordinator_required", exit_code=2, reason="nas_gate_refused", business_exit_code=outcome["hard_stop"])
                    return {"model": model, "state": "blocked", "reason": "nas_gate_refused",
                            "business_exit_code": outcome["hard_stop"]}
                if (terminal.get("exit_code") != 0 or not terminal.get("ended_at") or outcome.get("session") != session
                        or outcome.get("terminal") != "turn.completed"):
                    update(phase="coordinator_required", exit_code=2, reason="adopted_worker_terminal_unverified")
                    return {"model": model, "state": "blocked", "reason": "adopted_worker_terminal_unverified"}
                adopt_exit = None
                if (not normal_recovery(outcome.get("recovery_kind"))
                        and not (cleared and outcome.get("recovery_kind") == "nas_refused")):
                    update(phase="coordinator_required", exit_code=2, reason=outcome["recovery_kind"])
                    return {"model": model, "state": "blocked", "reason": outcome["recovery_kind"]}
            waiting = False
            capacity_safe = True
            own_pending = [b for b in c["batches"] if b["model"] == model
                           and (slot is None or ec.batch_slot(c, b) == slot)
                           and b["status"] not in ("submitted", "released")]
            for batch in own_pending:
                op = ec.cmd_operation_status(cid, batch["id"])
                operation = op['operation']
                capacity_safe = capacity_safe and (
                    operation.get('state') in ('not_started', 'completed', 'reconciled_not_written')
                    and not operation.get('may_have_written') and not operation.get('requires_reconciliation'))
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
                from tools.campaign_recovery import stops_reconciled
                from tools.campaign_continuation import worker_reconciled
                continuation = slot is not None and worker_reconciled(c, d, model, slot)
                cleared = bool(outcome.get("hard_stop")) and stops_reconciled(c, d, model, Path(saved["events"]), outcome)
                if outcome.get("hard_stop") and not cleared:
                    update(phase="coordinator_required", exit_code=2, reason="nas_gate_refused", business_exit_code=outcome["hard_stop"])
                    return {"model": model, "state": "blocked", "reason": "nas_gate_refused",
                            "business_exit_code": outcome["hard_stop"]}
                if not outcome.get("session") or session and outcome["session"] != session:
                    update(phase="coordinator_required", exit_code=2, reason="native_session_mismatch")
                    return {"model": model, "state": "blocked", "reason": "native_session_mismatch"}
                session = outcome.get("session") or session
                bind_slot_session(d, model, slot, session)
                capacity = capacity_terminal(outcome, state)
                if capacity and continuation:
                    # A coordinator may clear an exhausted capacity refusal only
                    # after a fresh same-model probe and exact terminal binding.
                    capacity_retries = 0
                    saved['capacity_retries'] = 0
                    receipts.write(root / 'worker.json', saved)
                elif capacity:
                    if not capacity_safe or capacity_retries >= MAX_CAPACITY_RETRIES:
                        reason = 'capacity_submit_unverified' if not capacity_safe else 'model_capacity_exhausted'
                        update(phase='coordinator_required', exit_code=2, reason=reason,
                               capacity_retries=capacity_retries)
                        return {'model': model, 'state': 'blocked', 'reason': reason}
                    capacity_retries += 1
                    saved['capacity_retries'] = capacity_retries
                    receipts.write(root / 'worker.json', saved)
                    update(phase='waiting_model_capacity', session=session,
                           capacity_retries=capacity_retries, wait_seconds=CAPACITY_WAIT_SECONDS)
                    remaining = CAPACITY_WAIT_SECONDS
                    while remaining > 0:
                        if (ec.load(d).get('dispatch') or {}).get('state') == 'draining' or stop and stop.is_set():
                            update(phase='claims_drained', success=count)
                            return {'model': model, 'state': 'drained', 'success': count}
                        step = min(poll, remaining)
                        sleep(step)
                        remaining -= step
                elif not continuation and (state.get("exit_code", 0) != 0 or outcome.get("terminal") != "turn.completed"
                        or (not normal_recovery(outcome.get("recovery_kind"))
                            and not (cleared and outcome.get("recovery_kind") == "nas_refused"))):
                    update(phase="coordinator_required", exit_code=2, reason="native_worker_blocked")
                    return {"model": model, "state": "blocked", "reason": "native_worker_blocked"}
                if not capacity:
                    capacity_retries = 0
                    stalled = stalled + 1 if own_count <= baseline else 0
                baseline = own_count
                update(stalled_turns=stalled, success=count, session=session)
                if stalled >= max_stalled:
                    update(phase="coordinator_required", exit_code=2, reason="no_formal_progress", success=count)
                    return {"model": model, "state": "blocked", "reason": "no_formal_progress"}
            if count >= c["quota"]:
                update(phase="quota_complete", success=count)
                return {"model": model, "state": "complete", "success": count}
            from tools.campaign_dispatch import pause_reason
            if (c.get('dispatch') or {}).get('state') == 'draining' or (pause_reason(c, model, slot) and not own_pending):
                update(phase='claims_drained', success=count)
                return {'model': model, 'state': 'drained', 'success': count}
            if slot is not None and ec.claimed_items(c, model) >= c["quota"] and not own_pending:
                update(phase="slot_drained", success=count)
                return {"model": model, "slot": slot, "state": "drained", "success": count}
            if stop is not None and stop.is_set():
                update(phase="coordinator_required", exit_code=2, reason="peer_slot_blocked")
                return {"model": model, "slot": slot, "state": "blocked", "reason": "peer_slot_blocked"}
            flight = (preflight or ax.runtime_preflight)(os.environ.get("KG_HUB_URL", ax.DEFAULT_URL), ax.api_token())
            if not flight.get("ok"):
                update(phase="coordinator_required", exit_code=2, reason="preflight_failed")
                return {"model": model, "state": "blocked", "reason": "preflight_failed", "preflight": flight}
            tag = uuid.uuid4().hex
            output, error = root / (tag + ".events.jsonl"), root / (tag + ".stderr.log")
            saved = {"session": session, "events": str(output), "stderr": str(error),
                     "capacity_retries": capacity_retries}
            receipts.write(root / "worker.json", saved)
            entry = Path(os.environ.get("KG_HUB_CODE", "~/.local/share/kg-hub/current")).expanduser() / "tools/extract"
            args = [str(entry), "runtime", "exec", "--proxy-mode", ec.runtime_settings(c)["proxy_mode"],
                    "--receipt-dir", str(native), "--", *native_args(c, model, session, slot)]
            env = {'env': {**os.environ}}
            dependencies = ec.dependency_pythonpath()
            if dependencies:
                env['env']['PYTHONPATH'] = dependencies
            if slot is not None:
                env['env'].update(KG_HUB_CAMPAIGN_ID=cid, KG_HUB_CAMPAIGN_MODEL=model,
                                  KG_HUB_CAMPAIGN_SLOT=str(slot))
            with os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as out, \
                    os.fdopen(os.open(error, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as err:
                child = spawn(args, cwd=ec.workspace_dir(), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                              start_new_session=True, **env)
            # A just-spawned wrapper has not acquired its lease yet. Do not
            # interpret the previous terminal receipt as this child's result.
            update(phase="starting_native", events=str(output), success=count, session=session)
            while receipts.status(native).get("attempt") == state.get("attempt"):
                if child.poll() is not None:
                    update(phase="coordinator_required", exit_code=2, reason="native_wrapper_start_failed")
                    return {"model": model, "state": "blocked", "reason": "native_wrapper_start_failed"}
                sleep(poll)
            sleep(poll)


def run_pool(cid: str, model: str, *, poll: float = 15, max_stalled: int = 3,
             stagger: float = 300, sleep=time.sleep, spawn=subprocess.Popen, preflight=None) -> dict:
    """One model manager starts the configured independent, quota-sharing slots.

    A blocked slot stops further dispatch. Already running native turns are
    allowed to finish; their receipts remain authoritative after manager exit.
    """
    if poll <= 0 or max_stalled < 1 or stagger < 0:
        raise ValueError("positive polling/stall limit and nonnegative stagger required")
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    n = ec.parallelism(c)
    if n == 1:
        return run(cid, model, poll=poll, max_stalled=max_stalled, sleep=sleep,
                   spawn=spawn, preflight=preflight)
    native_args(c, model, None, 1)
    ec.tracking_guard()
    root = ec.supervisor_dir(d, model)
    if (root / "worker.json").exists() or any(
            b["model"] == model and b.get("slot") not in range(1, n + 1) for b in c["batches"]):
        from tools.campaign_migration import valid_migration
        if not valid_migration(c, d):
            raise ValueError("existing unslotted history requires explicit migration; cannot duplicate the original worker")
    stop = threading.Event()

    def work(slot):
        remaining = stagger * (slot - 1)
        while remaining > 0 and not stop.is_set():
            step = min(poll, remaining)
            sleep(step)
            remaining -= step
        if stop.is_set():
            return {"model": model, "slot": slot, "state": "blocked", "reason": "peer_slot_blocked"}
        try:
            result = run(cid, model, slot=slot, poll=poll, max_stalled=max_stalled, sleep=sleep,
                         spawn=spawn, preflight=preflight, stop=stop)
        except (Exception, SystemExit) as exc:
            # Preserve the slot's receipt; never turn an exception into success.
            result = {"model": model, "slot": slot, "state": "blocked",
                      "reason": type(exc).__name__, "message": str(exc)}
        if result["state"] not in ("complete", "drained"):
            stop.set()
        return {**result, "slot": slot}

    with receipts.attempt(root, kind="supervisor_pool", identity={"campaign": cid, "model": model},
                          allow_abandoned=True) as update:
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {slot: pool.submit(work, slot) for slot in range(1, n + 1)}
            while not all(future.done() for future in futures.values()):
                slots = {str(slot): receipts.status(ec.supervisor_dir(d, model, slot) / "native")
                         for slot in futures}
                update(phase="draining_after_slot_block" if stop.is_set() else "running_slots",
                       configured_parallel=n, active_slots=sum(s["lease_held"] is True for s in slots.values()),
                       success=completed(ec.load(d), d, model))
                sleep(poll)
            outcomes = [future.result() for future in futures.values()]
        count = completed(ec.load(d), d, model)
        good = all(o["state"] in ("complete", "drained") for o in outcomes) and count >= c["quota"]
        update(phase="quota_complete" if good else "coordinator_required", exit_code=0 if good else 2,
               success=count, configured_parallel=n, active_slots=0, slots=outcomes)
        return {"model": model, "state": "complete" if good else "blocked", "success": count,
                "parallel": n, "slots": outcomes}
