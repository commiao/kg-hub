"""Offline outbound scope and evaluation-index CLI (no model/API calls)."""
import argparse
import json
from pathlib import Path

from utils import evaluation_receipts as receipts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="cmd", required=True)
    for command in ("scope", "index"):
        sub = subs.add_parser(command)
        sub.add_argument("--spec", required=True)
        sub.add_argument("--out", required=True)
    check = subs.add_parser("scope-check")
    check.add_argument("--scope", required=True)
    report = subs.add_parser("report")
    report.add_argument("--index", required=True)
    report.add_argument("--json")
    report.add_argument("--markdown")
    args = parser.parse_args(argv)
    try:
        if args.cmd in ("scope", "index"):
            if args.cmd == "scope" and Path(args.out).expanduser().exists():
                raise ValueError("scope path already exists; preserve it and choose a new --out")
            spec = receipts.read_json(args.spec)
            result = receipts.create_scope(spec) if args.cmd == "scope" else receipts.build_index(spec)
            receipts.write_json(args.out, result)
        elif args.cmd == "scope-check":
            result = receipts.verify_scope(receipts.read_json(args.scope))
        else:
            result = receipts.read_json(args.index)
            receipts.verify_index(result)
            if args.json:
                receipts.write_json(args.json, result)
            if args.markdown:
                Path(args.markdown).expanduser().write_text(receipts.render_markdown(result), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
