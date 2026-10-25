"""Concurrent quota and native-process tests; synthetic data, no API or models."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tools import agent_extract as ax, extract_campaign as ec, campaign_supervision as s
from utils import process_receipts as receipts


def gather(after, n, **kwargs):
    items = [{"id": i, "source_description": "synthetic", "source_obs_id": str(i),
              "episode_body": f"Synthetic observation {i}"} for i in range(after + 1, after + n + 1)]
    return {"picked": items, "stats": {}, "last": after + n, "lookahead": [], "seen": n}


class ParallelCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "KG_HUB_AGENT_EXTRACT_DIR": str(self.root / "state"),
            "KG_HUB_CLAIMS_DIR": str(self.root / "claims"),
            "KG_HUB_NAS_GATE_DIR": str(self.root / "gate")}))
        self.stack.enter_context(mock.patch.object(ec, "workspace_dir", return_value=self.workspace))
        self.stack.enter_context(mock.patch.object(ec, "tracking_guard", return_value={"excluded": True, "tracked": False}))
        self.stack.enter_context(mock.patch.object(ax, "gather_batch", side_effect=gather))
        self.stack.enter_context(mock.patch.object(ax, "load_config", return_value={}))
        self.stack.enter_context(mock.patch.object(ax, "api_token", return_value=None))
        self.d = ec.campaign_dir("parallel-test")
        self.d.mkdir(parents=True)
        for sub in ["batches", "results"]:
            (self.d / sub).mkdir()
        self.c = ec.new_campaign("parallel-test", ["m", "n"], executor="codex", quota=2000,
                                 batch=50, after_id=0, parallel=4)
        (self.d / "prompt.md").write_text(ax.PROMPT_PATH.read_text())
        ec.save(self.d, self.c)

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def claim(self, model="m", slot=1):
        return ec.cmd_claim(self.c["id"], model, slot=slot, url="http://invalid", token=None,
                            check=lambda rows: {})

    def finish(self, bid):
        with ec.locked(self.d):
            c = ec.load(self.d)
            b = ec.find_batch(c, bid)
            b["status"] = "submitted"
            with (self.d / "log.jsonl").open("a") as log:
                for oid in b["ids"]:
                    log.write(json.dumps({"batch": bid, "id": oid, "model": b["model"], "executor": "codex",
                                          "run_id": c["models"][b["model"]]["run_id"], "status": "ok"}) + "\n")
            ec.save(self.d, c)

    def test_eight_concurrent_slots_share_each_models_quota_without_overlaps(self):
        def worker(pair):
            model, slot = pair
            while True:
                out = self.claim(model, slot)
                if out["done"]:
                    return
                self.finish(out["batch"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, [(model, slot) for model in ["m", "n"] for slot in range(1, 5)]))
        c = ec.load(self.d)
        ids = [oid for b in c["batches"] for oid in b["ids"]]
        self.assertEqual(len(ids), 4000)
        self.assertEqual(len(set(ids)), 4000)
        for model in c["models"]:
            batches = [b for b in c["batches"] if b["model"] == model]
            self.assertEqual(len(batches), 40)
            self.assertEqual(s.completed(c, self.d, model), 2000)
            self.assertTrue(all(len(b["ids"]) == 50 for b in batches))

    def test_claim_is_idempotent_per_slot_and_last_group_is_short(self):
        self.c["quota"] = 175
        ec.save(self.d, self.c)
        first = self.claim(slot=1)
        again = self.claim(slot=1)
        self.assertEqual(first["batch"], again["batch"])
        self.assertTrue(again["resumed"])
        rest = [self.claim(slot=i) for i in [2, 3, 4]]
        self.assertEqual([x["items"] for x in [first, *rest]], [50, 50, 50, 25])
        self.finish(first["batch"])
        self.assertTrue(self.claim(slot=1)["done"])
        self.assertEqual(s.completed(ec.load(self.d), self.d, "m"), 50)

    def test_invalid_configuration_and_wrong_owner_are_rejected_before_writes(self):
        for parallel in [0, -1, True, 1.5]:
            with self.assertRaises(SystemExit):
                ec.new_campaign("x", ["m"], executor="codex", quota=2000, batch=50, after_id=0, parallel=parallel)
        for slot in [None, 0, 5]:
            with self.assertRaises(SystemExit):
                self.claim(slot=slot)
        own, other = self.claim(slot=1), self.claim(slot=2)
        with mock.patch.dict(os.environ, {"KG_HUB_CAMPAIGN_ID": self.c["id"],
                    "KG_HUB_CAMPAIGN_MODEL": "m", "KG_HUB_CAMPAIGN_SLOT": "1"}):
            with self.assertRaisesRegex(SystemExit, "binding mismatch"):
                self.claim(slot=2)
            with self.assertRaisesRegex(SystemExit, "different worker slot"):
                ec.cmd_submit(self.c["id"], other["batch"], dry_run=True, url="http://invalid", token=None)
            self.assertEqual(self.claim()["batch"], own["batch"])
        self.assertFalse((self.d / "operations").exists())

    def test_status_separates_configured_slots_from_actual_live_native_leases(self):
        self.claim(slot=3)
        self.assertEqual(ec.cmd_status(self.c["id"])["active_workers"], 0)
        with receipts.attempt(ec.supervisor_dir(self.d, "m", 3) / "native", kind="native_cli"):
            state = ec.cmd_status(self.c["id"])
            self.assertEqual((state["parallel"], state["configured_workers"], state["active_workers"]), (4, 8, 1))
            self.assertTrue(state["worker_slots"]["m"]["3"]["native"]["lease_held"])

    def test_subagent_definitions_and_briefs_use_each_slots_identity(self):
        defs = ec.agent_defs(self.c)
        self.assertEqual(len(defs), 8)
        for slot in range(1, 5):
            text = defs[f"kg-extract-m-slot-{slot}"]["prompt"]
            self.assertIn(f"claim --id parallel-test --model m --slot {slot}", text)
            self.assertIn("所有槽位合计配额 2000 条", text)
        self.assertEqual(ec.parallelism({}), 1)

    def test_cli_routes_parallel_start_and_supervision(self):
        with mock.patch.object(ax, "runtime_preflight", return_value={"ok": True}), \
                mock.patch.object(ec, "cmd_start", return_value={}) as start, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ec.main(["start", "--id", "new", "--models", "m", "--executor", "codex",
                "--parallel", "4", "--batch", "50", "--quota", "2000"]), 0)
            self.assertEqual((start.call_args.kwargs["parallel"], start.call_args.kwargs["batch"],
                              start.call_args.kwargs["quota"]), (4, 50, 2000))
        with mock.patch.object(s, "run_pool", return_value={"state": "complete"}) as pool, \
                contextlib.redirect_stdout(io.StringIO()):
            ec.main(["supervise", "--id", self.c["id"], "--model", "m"])
            self.assertEqual(pool.call_args.kwargs["stagger"], 300)

    def test_pool_starts_all_four_workers_concurrently_and_preserves_distinct_sessions(self):
        barrier = threading.Barrier(4)
        calls = []
        lock = threading.Lock()

        def spawn(args, **kwargs):
            env = kwargs["env"]
            slot = int(env["KG_HUB_CAMPAIGN_SLOT"])
            directory = Path(args[args.index("--receipt-dir") + 1])
            session = "native-slot-" + str(slot)
            with receipts.attempt(directory, kind="native_cli") as update:
                with lock:
                    calls.append((slot, args))
                barrier.wait(timeout=10)
                state = ec.cmd_status(self.c["id"])
                self.assertEqual(state["active_workers"], 4)
                # Keep all leases held until every worker observed the four-way overlap.
                barrier.wait(timeout=10)
                while True:
                    out = self.claim(slot=slot)
                    if out["done"]:
                        break
                    self.finish(out["batch"])
                for event in [{"type": "thread.started", "thread_id": session}, {"type": "turn.completed"}]:
                    kwargs["stdout"].write((json.dumps(event) + "\n").encode())
                kwargs["stdout"].flush()
                update(exit_code=0)
            return mock.Mock(poll=lambda: 0)

        out = s.run_pool(self.c["id"], "m", poll=0.01, stagger=0, spawn=spawn, preflight=lambda *a: {"ok": True})
        self.assertEqual((out["state"], out["success"]), ("complete", 2000))
        self.assertEqual({slot for slot, _ in calls}, {1, 2, 3, 4})
        for slot, _ in calls:
            worker = receipts.read(ec.supervisor_dir(self.d, "m", slot) / "worker.json")
            self.assertEqual(s.events(Path(worker["events"]))["session"], "native-slot-" + str(slot))

    def test_blocked_slot_prevents_delayed_launch_and_pool_does_not_fake_completion(self):
        calls = []
        def blocked(cid, model, **kwargs):
            calls.append(kwargs["slot"])
            return {"state": "blocked", "reason": "approval_rejected"}
        with mock.patch.object(s, "run", side_effect=blocked):
            out = s.run_pool(self.c["id"], "m", poll=0.01, stagger=0.05)
        self.assertEqual(calls, [1])
        self.assertEqual(out["state"], "blocked")
        self.assertEqual(out["success"], 0)

    def test_real_native_processes_receive_slot_bindings_and_resume_without_overrunning_quota(self):
        binary, code = self.root / "bin", self.root / "code"
        binary.mkdir()
        (code / "tools").mkdir(parents=True)
        repo = Path(ec.__file__).resolve().parent.parent
        entry = code / "tools/extract"
        entry.write_text("#!" + sys.executable + "\nimport sys\nsys.path.insert(0," + repr(str(repo)) + ")\n"
                         "from tools.extract_cli import main\nraise SystemExit(main())\n")
        entry.chmod(0o700)
        worker = binary / "codex"
        worker.write_text("#!" + sys.executable + "\n" +
            "import os,sys,json,time,pathlib\n"
            "sys.path[:0]=" + repr([str(repo), str(repo / "tests")]) + "\n"
            "from test_campaign_parallel import gather\n"
            "from tools import extract_campaign as ec,agent_extract as ax\n"
            "args=sys.argv[1:];slot=int(os.environ['KG_HUB_CAMPAIGN_SLOT']);model=os.environ['KG_HUB_CAMPAIGN_MODEL']\n"
            "assert 'model_reasoning_effort=medium' in args\n"
            "session='process-slot-'+str(slot)\n"
            "if 'resume' in args:assert args[args.index('resume')+1]==session\n"
            "d=ec.campaign_dir(os.environ['KG_HUB_CAMPAIGN_ID'])\n"
            "(d/('ready-'+str(slot))).write_text(session)\n"
            "deadline=time.monotonic()+10\n"
            "while len(list(d.glob('ready-*')))<4:\n"
            " if time.monotonic()>deadline:raise RuntimeError('four workers never overlapped')\n"
            " time.sleep(.005)\n"
            "ec.tracking_guard=lambda *a,**k:{'tracked':False}\n"
            "ax.gather_batch=gather;ax.load_config=lambda:{}\n"
            "out=ec.cmd_claim(d.name,model,url='http://invalid',token=None,check=lambda rows:{})\n"
            "if not out['done']:\n"
            " with ec.locked(d):\n"
            "  c=ec.load(d);b=ec.find_batch(c,out['batch']);b['status']='submitted'\n"
            "  with (d/'log.jsonl').open('a') as log:\n"
            "   for oid in b['ids']:log.write(json.dumps(dict(id=oid,batch=b['id'],model=model,executor='codex',run_id=c['models'][model]['run_id'],status='ok'))+'\\n')\n"
            "  ec.save(d,c)\n"
            "print(json.dumps(dict(type='thread.started',thread_id=session)))\n"
            "print(json.dumps(dict(type='turn.completed')))\n")
        worker.chmod(0o700)
        self.c["quota"] = 225
        ec.save(self.d, self.c)
        with mock.patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"], "KG_HUB_CODE": str(code)}):
            out = s.run_pool(self.c["id"], "m", poll=0.01, stagger=0, preflight=lambda *a: {"ok": True})
        errors = {str(p.relative_to(self.d)): p.read_text()[-1800:]
                  for p in self.d.rglob("*.stderr.log") if p.stat().st_size}
        self.assertEqual((out["state"], out["success"]), ("complete", 225), {"result": out, "stderr": errors})
        c = ec.load(self.d)
        self.assertEqual(ec.claimed_items(c, "m"), 225)
        self.assertEqual(sorted(len(b["ids"]) for b in c["batches"]), [25, 50, 50, 50, 50])
        self.assertEqual(set(c["slot_sessions"]["m"].values()), {f"process-slot-{i}" for i in range(1, 5)})

    def test_session_cannot_be_reused_by_another_slot_or_model(self):
        s.bind_slot_session(self.d, "m", 1, "native-one")
        for model, slot in [("m", 2), ("n", 1)]:
            with self.assertRaisesRegex(ValueError, "another slot"):
                s.bind_slot_session(self.d, model, slot, "native-one")
        with self.assertRaisesRegex(ValueError, "recorded owner"):
            s.bind_slot_session(self.d, "m", 1, "different-session")

    def test_existing_unslotted_history_is_not_silently_migrated_or_duplicated(self):
        self.c["batches"] = [{"id": "legacy", "model": "m", "ids": [1], "status": "claimed"}]
        ec.save(self.d, self.c)
        with mock.patch.object(s, "run") as worker, self.assertRaisesRegex(ValueError, "explicit migration"):
            s.run_pool(self.c["id"], "m", stagger=0)
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
