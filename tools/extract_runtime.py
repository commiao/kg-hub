"""Data-free native CLI readiness, independent of bench/scoring dependencies."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import cli_runtime as runtime

NATIVE_EXECUTORS = ("claude", "codex", "qodercli")


def execute(argv, *, proxy_mode="auto", receipt_dir: Path | None = None) -> int:
    """Run an unchanged argv with the same child-only environment as a probe."""
    if not argv:
        raise ValueError("runtime exec requires a command after --")
    env, _ = runtime.cli_environment(proxy_mode=proxy_mode)
    # Rust CLIs enumerate the full environment; inherited surrogate bytes in
    # optional IDE variables must not crash their tools. Never mutate the host.
    for key, value in list(env.items()):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in key + value):
            if key.endswith("_VM_OPTIONS"):
                env.pop(key)
            else:
                raise ValueError("invalid UTF-8 environment; configuration must be repaired")
    if receipt_dir is not None:
        from utils import process_receipts as receipts
        with receipts.attempt(receipt_dir, kind="native_cli") as update:
            code = execute_child(argv, env, update)
            update(exit_code=code)
            return code
    return execute_child(argv, env)


def execute_child(argv, env, update=None) -> int:
    try:
        child = subprocess.Popen(list(argv), env=env)
        if update:
            update(phase="native_running", child_pid=child.pid)
        code = child.wait()
    except OSError as exc:
        print("runtime exec could not start child: " + type(exc).__name__, file=sys.stderr)
        return 127
    return code if code >= 0 else 128 - code


def inspect(*, executor: str | None = None, proxy_mode="auto") -> dict:
    _, route = runtime.cli_environment(proxy_mode=proxy_mode)
    return {"version": "cli-runtime-inspection-v1", "status": "not_probed", "route": route,
            "executor": executor, "native_supported": executor in NATIVE_EXECUTORS if executor else None,
            "cli_path": shutil.which(executor) if executor in NATIVE_EXECUTORS else None,
            "note": "Inspection is not model readiness. Manual tool executors require a tool-side probe."}


def probe_model(executor: str, model: str, *, effort="medium", proxy_mode="auto") -> dict:
    if executor not in NATIVE_EXECUTORS:
        return {"status": "unsupported_tool_side_probe_required", "executor": executor,
                "requested_model": model, "model_verified": False}
    # Lazy import: inspect is usable in stdlib-only peer consumers.  The native
    # adapters themselves do not load numpy/fastembed (only scoring does).
    from tools.extract_bench import ADAPTERS
    env, route = runtime.cli_environment(proxy_mode=proxy_mode)
    with tempfile.TemporaryDirectory(prefix="kg-hub-canary-") as work:
        return runtime.probe(lambda system, query: ADAPTERS[executor](model, system, query,
                             cwd=Path(work), effort=effort, env=env), executor=executor, model=model,
                             effort=effort, proxy_mode=proxy_mode, environ=env, route=route)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    launcher = commands.add_parser("exec", help="execute native argv with probe-equivalent child-only proxy environment")
    launcher.add_argument("--proxy-mode", choices=("auto", "env-only", "none"), default="auto")
    launcher.add_argument("--receipt-dir", type=Path, help="durable native attempt/exit receipt and single-owner lease")
    launcher.add_argument("argv", nargs=argparse.REMAINDER, help="-- <native CLI argv...>; argv is never interpreted by a shell")
    state = commands.add_parser("status", help="read a managed process lease/exit receipt without ps")
    state.add_argument("--receipt-dir", type=Path, required=True)
    for name in ("inspect", "probe"):
        part = commands.add_parser(name)
        part.add_argument("--executor", required=name == "probe")
        part.add_argument("--proxy-mode", choices=("auto", "env-only", "none"), default="auto")
        part.add_argument("--out", help="new receipt path; existing files are never overwritten")
        if name == "probe":
            part.add_argument("--model", required=True)
            part.add_argument("--effort", default="medium")
    args = parser.parse_args(argv)
    if args.command == "status":
        from utils.process_receipts import status
        print(json.dumps(status(args.receipt_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == "exec":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not command:
            parser.error("runtime exec requires -- <native CLI argv...>")
        return execute(command, proxy_mode=args.proxy_mode, receipt_dir=args.receipt_dir)
    if args.out and Path(args.out).expanduser().exists():
        parser.error("receipt path already exists; choose a new --out before probing")
    result = (inspect(executor=args.executor, proxy_mode=args.proxy_mode) if args.command == "inspect" else
              probe_model(args.executor, args.model, effort=args.effort, proxy_mode=args.proxy_mode))
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command == "inspect" or result.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
