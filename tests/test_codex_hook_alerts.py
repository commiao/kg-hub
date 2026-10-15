"""Upgrade alerts must survive an existing incident, restarts and delivery failure."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W

KIND = "codex_hooks_unhealthy"
KEY = "claude-mem@local:hooks/codex-hooks.json:post_compact:0:0"


def hook(approval="modified", digest="sha256:new", **kwargs):
    return dict({"key": KEY, "id": "codex:"+KEY, "event": "PostCompact",
                 "approval": approval, "current_hash": digest,
                 "state": "green" if approval in ("approved", "managed") else "red"}, **kwargs)


def snapshot(hooks, blockers=None, **kwargs):
    return dict({"_host": "mac-office", "_age_s": 10,
                 "hook_inventory": [{"tool": "codex", "hooks": hooks}],
                 "blockers": blockers or [], "overall": "red"}, **kwargs)


class HookAlertTests(unittest.TestCase):
    def round(self, state, snap, *, delivered=True):
        decision = W.judge_snapshots([snap], {}, None)
        previous = state.get("anomalies", {})
        anomalies, details, counters = {}, {}, {}
        W.apply_capture_decision(decision, previous, anomalies, details)
        W.apply_codex_hook_decision(decision, previous, anomalies, details,
                                    state.get("counters", {}), counters)
        saved = []
        with patch.object(W, "save_state", side_effect=lambda s: saved.append(copy.deepcopy(s))), \
             patch.object(W, "emit_alert", return_value=delivered) as emit:
            pending, ids, alerted = W.deliver_alerts(state, anomalies, details,
                decision.blocker_ids if decision.blocked is not None else None,
                hook_ids=decision.codex_hook_ids if decision.codex_hooks is not None else None)
        result = saved[-1]
        result["counters"] = counters
        return result, emit.call_args_list, decision

    def test_upgrade_missing_and_modified_hooks_alert_with_actionable_identity(self):
        for status in ("missing", "modified", "unknown", "not-configured"):
            with self.subTest(status=status):
                state, calls, _ = self.round({}, snapshot([hook(status)]))
                alert = next(c for c in calls if c.args[1] == KIND)
                self.assertEqual(alert.args[0], "fire")
                self.assertIn(KEY, alert.args[2]); self.assertIn("sha256:new", alert.args[2])
                self.assertIn("/hooks", alert.args[2])

    def test_existing_capture_incident_cannot_suppress_codex_upgrade(self):
        state = {"anomalies": {"capture_blocked": True},
                 "capture_blocker_ids": ["mac-office:worker"],
                 "capture_blocker_alerted": ["mac-office:worker"]}
        other = {"id": "worker", "label": "worker", "detail": "failed"}
        _, calls, _ = self.round(state, snapshot([hook()], [other]))
        self.assertEqual([c.args[:2] for c in calls], [("fire", KIND)])

    def test_hook_blockers_use_only_the_dedicated_incident(self):
        blockers = [{"id": key, "label": "Codex", "detail": "modified"}
                    for key in ("hook:codex", "hook-inventory:codex")]
        _, calls, decision = self.round({}, snapshot([hook()], blockers))
        self.assertEqual(decision.blocked, [])
        self.assertEqual([c.args[:2] for c in calls], [("fire", KIND)])

    def test_same_issue_silent_new_hash_same_key_refires_and_survives_restart(self):
        state, calls, _ = self.round({}, snapshot([hook()]))
        self.assertEqual(len(calls), 1)
        state = json.loads(json.dumps(state))
        state, calls, _ = self.round(state, snapshot([hook()]))
        self.assertEqual(calls, [])
        state, calls, _ = self.round(state, snapshot([hook(digest="sha256:newer")]))
        self.assertEqual([c.args[:2] for c in calls], [("fire", KIND)])
        _, calls, _ = self.round(state, snapshot([hook()]))
        self.assertEqual(calls, [])  # flickering previously reported definition

    def test_partial_approval_does_not_claim_recovery(self):
        state, _, _ = self.round({}, snapshot([hook(), hook("missing", key="another")]))
        state, calls, _ = self.round(state, snapshot([hook("approved"), hook("missing", key="another")]))
        self.assertTrue(state["anomalies"][KIND]); self.assertEqual(calls, [])
        state, calls, _ = self.round(state, snapshot([hook("approved"), hook("approved", key="another")]))
        self.assertEqual([c.args[:2] for c in calls], [("clear", KIND)])
        _, calls, _ = self.round(state, snapshot([hook("approved")]))
        self.assertEqual(calls, [])

    def test_delivery_failure_retries_and_recovery_supersedes_pending_fire(self):
        state, calls, _ = self.round({}, snapshot([hook()]), delivered=False)
        self.assertIn(KIND, state["pending_alerts"])
        state, calls, _ = self.round(json.loads(json.dumps(state)), snapshot([hook()]))
        self.assertEqual(calls[0].kwargs["attempt"], 2)
        self.assertNotIn(KIND, state["pending_alerts"])
        state, _, _ = self.round({}, snapshot([hook()]), delivered=False)
        _, calls, _ = self.round(state, snapshot([hook("approved")]))
        self.assertEqual([c.args[:2] for c in calls], [("clear", KIND)])

    def test_stale_online_snapshot_does_not_claim_hash_recovered(self):
        state, _, _ = self.round({}, snapshot([hook()]))
        state, calls, _ = self.round(state, snapshot([hook("approved")], _age_s=7200, _device_state="online"))
        self.assertTrue(state["anomalies"][KIND])
        self.assertFalse(any(c.args[:2] == ("clear", KIND) for c in calls))

    def test_missing_inventory_is_monitor_failure_and_keeps_generic_hook_blocker(self):
        snap = snapshot([],[{"id": "hook:codex", "label": "Codex", "detail": "unknown"}])
        del snap["hook_inventory"]
        _, calls, decision = self.round({}, snap)
        self.assertIsNone(decision.codex_hooks)
        self.assertTrue(decision.source_errors)
        self.assertTrue(decision.blocked)

    def test_idle_and_trusted_hooks_do_not_alert(self):
        state, calls, _ = self.round({}, snapshot([hook("approved")], overall="amber"))
        self.assertFalse(state["anomalies"][KIND]); self.assertEqual(calls, [])

    def test_unreadable_api_does_not_clear_prior_hook_incident(self):
        anomalies, details, counters = {}, {}, {}
        W.apply_codex_hook_decision(W.CaptureDecision(None, None), {KIND: True},
                                    anomalies, details, {}, counters)
        self.assertTrue(anomalies[KIND])
        self.assertNotIn(KIND+":clear", details)

    def test_main_persists_hook_identity_between_full_state_rewrites(self):
        state = {"anomalies": {}, "last_run": "2026-10-15T00:00:00+00:00"}
        stats = {"pending": 0, "errored_last_1h": 0, "errored_total": 0,
                 "oldest_pending_age_seconds": None}
        fired = []
        for digest in ("sha256:a", "sha256:a", "sha256:b", "sha256:a"):
            decision = W.judge_snapshots([snapshot([hook(digest=digest)])], {}, None)
            saved = []
            with patch.object(W, "load_notify_config", return_value={}), \
                 patch.object(W, "load_state", return_value=copy.deepcopy(state)), \
                 patch.object(W, "save_state", side_effect=lambda s:saved.append(copy.deepcopy(s))), \
                 patch.object(W, "check_disk_temp", return_value=(None, "")), \
                 patch.object(W, "check_health", return_value=(True, "ok")), \
                 patch.object(W, "check_queue", return_value=(stats, "ok")), \
                 patch.object(W, "check_search_probe", return_value=("ok",0.1,"0.1s")), \
                 patch.object(W, "check_refinery_halt", return_value=None), \
                 patch.object(W, "check_gateway_monitor", return_value={n:False for n in W.GATEWAY_ALERTS}), \
                 patch.object(W, "check_model_gateway_consumer_contract", return_value="ok"), \
                 patch.object(W, "check_capture_chain", return_value=decision), \
                 patch.object(W, "emit_alert", side_effect=lambda sev,kind,msg,**kw:fired.append((sev,kind)) or True):
                W.main()
            state = json.loads(json.dumps(saved[-1]))
        self.assertEqual([v for v in fired if v[1]==KIND], [("fire",KIND),("fire",KIND)])
        self.assertEqual(len(state["codex_hook_alerted"]), 2)

    def test_feishu_business_failure_and_retry_through_real_http_transport(self):
        import tempfile
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        messages = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages.append(payload)
                self.send_response(200);self.end_headers()
                self.wfile.write(json.dumps({"code":19024 if len(messages)==1 else 0}).encode())
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as td, \
                 patch.object(W,"FEISHU_WEBHOOK",f"http://127.0.0.1:{server.server_port}"), \
                 patch.object(W,"STATE_DIR",Path(td)), \
                 patch.object(W,"STATE_FILE",Path(td)/"state.json"), \
                 patch.object(W,"ALERTS_LOG",Path(td)/"alerts.log"):
                W.deliver_alerts({}, {KIND:True}, {KIND:"PostCompact hash mismatch"}, [], hook_ids=[KEY+":hash"])
                state=W.load_state();self.assertIn(KIND,state["pending_alerts"])
                W.deliver_alerts(state,{KIND:True},{},[],hook_ids=[KEY+":hash"])
                state=W.load_state();self.assertNotIn(KIND,state["pending_alerts"])
                W.deliver_alerts(state,{KIND:False},{KIND+":clear":"current hash trusted"},[],hook_ids=[])
                self.assertEqual(len(messages),3)
                self.assertIn("🔴",messages[0]["content"]["text"])
                self.assertIn("✅",messages[2]["content"]["text"])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
