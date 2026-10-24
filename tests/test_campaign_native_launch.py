"""Real local executable + stdlib entry; no models, network or graph writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from tools import extract_campaign as campaign
from tools import extract_runtime
from utils import cli_runtime


class NativeLaunchTest(unittest.TestCase):
    def test_probe_and_formal_launch_share_system_proxy_and_frozen_effort(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary, code, workspace, state = [root / name for name in ("bin", "code space", "workspace space", "state")]
            for directory in (binary, workspace, state, code / "tools"):
                directory.mkdir(parents=True)
            log = root / "native.jsonl"
            worker = binary / "codex"
            worker.write_text("#!/usr/bin/python3\nimport sys,json,os,pathlib\n"
                "args=sys.argv[1:]\n"
                "record={'args':args,'proxy':os.environ.get('HTTPS_PROXY')}\n"
                "with open(os.environ['FAKE_NATIVE_LOG'],'a') as f: f.write(json.dumps(record)+'\\n')\n"
                "if '-o' in args:\n"
                " pathlib.Path(args[args.index('-o')+1]).write_text(sys.stdin.read())\n"
                " print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n"
                "else: print(json.dumps(record))\n")
            worker.chmod(0o700)
            proxy = "http://127.0.0.1:4621"
            entry = code / "tools/extract"
            entry.write_text("#!/usr/bin/python3 -S\nimport sys\n"
                "sys.path.insert(0," + repr(str(campaign.REPO)) + ")\n"
                "if sys.argv[1]=='campaign': print('synthetic public brief',end='')\n"
                "else:\n"
                " from utils import cli_runtime\n"
                " cli_runtime.sys.platform='darwin'\n"
                " cli_runtime.system_proxies=lambda: {'status':'system_proxy_configured','values':{'HTTPS_PROXY':" + repr(proxy) + "}}\n"
                " from tools.extract_cli import main\n"
                " raise SystemExit(main())\n")
            entry.chmod(0o700)
            env = {"PATH": str(binary), "KG_HUB_CODE": str(code), "FAKE_NATIVE_LOG": str(log)}
            cfg = {"executor": "codex", "models": {"synthetic-model": {}}, "start_contract": {
                "effort": "high", "proxy_mode": "auto", "model_preflight": {"synthetic-model": {"executor": "codex"}}}}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(cli_runtime.sys, "platform", "darwin"), \
                    mock.patch.object(cli_runtime, "system_proxies", return_value={
                        "status": "system_proxy_configured", "values": {"HTTPS_PROXY": proxy}}):
                probe = extract_runtime.probe_model("codex", "synthetic-model", effort="high", proxy_mode="auto")
                self.assertNotIn("HTTPS_PROXY", os.environ)
                command = campaign.launch_command("codex", "synthetic", "synthetic-model", repo=workspace,
                    state=state, cfg=cfg)
                formal = subprocess.run(["/bin/sh", "-c", command], env=env, text=True,
                                         capture_output=True, check=True)
            self.assertEqual(probe["status"], "ready")
            formal_record = json.loads(formal.stdout)
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual({record["proxy"] for record in records}, {proxy})
            self.assertTrue(all(any(arg in ('model_reasoning_effort="high"', 'model_reasoning_effort=high')
                                    for arg in record["args"]) for record in records))
            self.assertEqual(formal_record["proxy"], proxy)
            self.assertIn("runtime exec --proxy-mode auto -- codex", command)

    def test_none_and_env_only_are_passed_and_legacy_defaults_are_stable(self):
        for mode in ("none", "env-only"):
            cfg = {"executor": "qoder", "models": {"m": {}}, "start_contract": {
                "effort": "high", "proxy_mode": mode, "model_preflight": {"m": {"executor": "qodercli"}}}}
            cmd = campaign.launch_command("qodercli", "c", "m", cfg=cfg)
            self.assertIn("runtime exec --proxy-mode " + mode + " -- qodercli", cmd)
            self.assertIn("--reasoning-effort high", cmd)
        self.assertEqual(campaign.runtime_settings({}), {"effort": "medium", "proxy_mode": "auto"})
        self.assertIn("model_reasoning_effort=medium", campaign.launch_command("codex", "legacy", "m"))

    def test_new_campaign_rejects_executor_or_model_override_before_launch(self):
        cfg = {"executor": "codex", "models": {"m": {}, "injected": {}}, "start_contract": {
            "effort": "high", "proxy_mode": "auto", "model_preflight": {"m": {"executor": "codex"}}}}
        with self.assertRaisesRegex(SystemExit, "frozen executor"):
            campaign.launch_command("qodercli", "c", "m", cfg=cfg)
        with self.assertRaisesRegex(SystemExit, "frozen start contract"):
            campaign.launch_command("codex", "c", "injected", cfg=cfg)
        changed = {**cfg, "executor": "qodercli"}
        with self.assertRaisesRegex(SystemExit, "frozen executor"):
            campaign.launch_command("codex", "c", "m", cfg=changed)
        # Legacy campaigns remain readable and retain their prior launch choices.
        self.assertIn("qodercli -p", campaign.launch_command("qodercli", "old", "m", cfg={"executor": "codex"}))
