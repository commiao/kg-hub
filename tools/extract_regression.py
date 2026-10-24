"""Register canonical semantic cases and check independently completed reviews.

No extraction calls, external transmission, graph writes or implicit pass values.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from utils import semantic_regression as semantic


def main(argv=None):
    from tools.extract_bench import _bench, bench_root, load_run
    parser = argparse.ArgumentParser(description="固定旧错语义回归：登记 → 选定 → 独立评审 → 校验；无需重抽原输出")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--bench", required=True)
    create.add_argument("--spec", required=True)
    create.add_argument("--out", required=True)
    activate = sub.add_parser("activate")
    activate.add_argument("--suite", required=True)
    activate.add_argument("--reviewer", required=True, help="操作者姓名声明；不等于机器验证真人授权")
    activate.add_argument("--confirm-suite-sha", required=True)
    for name in ("sheet", "check"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--bench", required=True)
        cmd.add_argument("--run", required=True)
        cmd.add_argument("--out", required=True)
        if name == "check":
            cmd.add_argument("--review", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "activate":
            result = semantic.activate(bench_root(), args.suite, reviewer=args.reviewer,
                                       confirm_suite_sha=args.confirm_suite_sha)
        else:
            p, manifest = _bench(args.bench)
            if args.command == "create":
                result = semantic.create_suite(p, manifest, semantic.read(args.spec))
            else:
                suite = semantic.active_suite(bench_root(), manifest["bench_id"])
                run = load_run(Path(args.run).expanduser())
                if args.command == "sheet":
                    result = semantic.review_sheet(p, manifest, run, suite)
                else:
                    result = semantic.check_review(p, manifest, run, suite, semantic.read(args.review))
            semantic.write_new(args.out, result)
        # Review正文只写入用户指定的私有文件，不回显到终端报告。
        print(json.dumps({k: v for k, v in result.items() if k not in ("review", "cases", "context")},
                         ensure_ascii=False, indent=2))
        return 1 if result.get("verdict") == "fail" else 0
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
