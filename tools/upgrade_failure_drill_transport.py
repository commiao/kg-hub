"""Private transport for drill_upgrade_acceptance_failure.py. No --live support."""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import re
import runpy
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def message(drill_id):
    return ("【演练】🔴 kg-hub claude-mem 升级验收失败\n"
            "【模拟故障｜无需操作】\n演练编号：" + drill_id
            + "\n这是独立临时状态中的验收失败与投递重试演练。"
              "\n真实 hook、采集和生产告警状态未被修改，无需重新批准或修复 hook。"
              "\n重复失败和恢复后均应静默，本演练只允许一条真实失败告警。")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def deliver(drill_id, *, loopback_url=None, send_feishu=False, home=None):
    proof = {"drill_id": drill_id, "destination": "feishu" if send_feishu else "loopback", "ok": False,
             "at": datetime.now(timezone.utc).isoformat(), "http_attempts": 0}
    try:
        if not re.fullmatch(r"UPGRADE-DRILL-\d{8}-\d{6}-[a-f0-9]{12}", drill_id):
            raise ValueError("invalid drill id")
        if send_feishu and loopback_url:
            raise ValueError("conflicting destinations")
        if not send_feishu:
            target = urlsplit(loopback_url or "")
            if (target.scheme != "http" or target.hostname != "127.0.0.1"
                    or not target.port or target.username or target.password):
                raise ValueError("loopback target required")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            request = urllib.request.Request(loopback_url,
                data=json.dumps({"msg_type": "text", "content": {"text": message(drill_id)}}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            proof["http_attempts"] = 1
            with opener.open(request, timeout=10) as response:
                proof["http_status"] = response.status
                payload = json.load(response)
            code = payload.get("code", payload.get("StatusCode"))
            proof["feishu_code"] = code if type(code) is int else None
            proof["ok"] = 200 <= proof["http_status"] < 300 and type(code) is int and code == 0
        else:
            # Execute the installed skill unmodified, but discard all its output:
            # failures can contain a webhook URL. Trace only status and business code.
            sender = (home or Path.home()) / ".claude/skills/feishu-notify/scripts/send.py"
            original_open = urllib.request.urlopen
            original_argv = sys.argv[:]
            def traced(request, *args, **kwargs):
                if proof["http_attempts"]:
                    proof["extra_attempt_blocked"] = True
                    raise RuntimeError("one HTTP attempt only")
                proof["http_attempts"] += 1
                try:
                    with original_open(request, *args, **kwargs) as response:
                        proof["http_status"] = response.status
                        body = response.read()
                except urllib.error.HTTPError as exc:
                    proof.update(http_status=exc.code, error_type="HTTPError")
                    raise
                payload = json.loads(body)
                code = payload.get("code", payload.get("StatusCode"))
                proof["feishu_code"] = code if type(code) is int else None
                return io.BytesIO(body)
            try:
                urllib.request.urlopen = traced
                sys.argv = [str(sender), message(drill_id), "--webhook", "kg-hub"]
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        runpy.run_path(str(sender), run_name="__main__")
                    except SystemExit as exc:
                        proof["sender_exit_code"] = exc.code
                proof["ok"] = (proof.get("sender_exit_code", 0) == 0
                    and not proof.get("extra_attempt_blocked")
                    and proof["http_attempts"] == 1 and 200 <= proof.get("http_status", 0) < 300
                    and type(proof.get("feishu_code")) is int and proof["feishu_code"] == 0)
            finally:
                urllib.request.urlopen = original_open
                sys.argv = original_argv
    except urllib.error.HTTPError as exc:
        proof.update(http_status=exc.code, error_type="HTTPError")
    except Exception as exc:
        proof["error_type"] = type(exc).__name__
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--drill-id", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--loopback-url")
    group.add_argument("--send-feishu", action="store_true")
    args = parser.parse_args(argv)
    proof = deliver(args.drill_id, loopback_url=args.loopback_url, send_feishu=args.send_feishu)
    print(json.dumps(proof, ensure_ascii=False))
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
