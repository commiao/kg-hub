"""Official, model-free entry point for source-scoped extraction repairs.

Preview is read-only. Applying uses exactly the reviewed request file, including
its expected snapshot digest; the CLI never silently refreshes that precondition.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import agent_extract as ax
from utils.extraction_repair import parse_request, selector, _ID


def main(argv=None, *, http=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("preview", "apply", "status"))
    ap.add_argument("--file", help="JSON selector or complete reviewed repair request")
    ap.add_argument("--repair-id", help="status: original repair_id")
    ap.add_argument("--out", help="save full response JSON (including snapshots/audit)")
    ap.add_argument("--request-out", help="preview: save unapproved request template bound to snapshot")
    ap.add_argument("--url", default=ax.DEFAULT_URL)
    args = ap.parse_args(argv)
    post = http or ax._http
    token = ax.api_token()
    if args.command == "status":
        if not args.repair_id or not _ID.fullmatch(args.repair_id):
            ap.error("status requires a valid --repair-id")
        method, path, payload = "GET", "/api/extraction/repair?" + urlencode({"repair_id": args.repair_id}), None
    else:
        if not args.file:
            ap.error("preview/apply require --file")
        payload = json.loads(Path(args.file).read_text())
        selector(payload)
        if args.command == "apply":
            payload = parse_request(payload)
        elif not payload.get("expected_snapshot_sha256"):
            # Obtain the initial source snapshot before binding a repair plan.
            # Do not send a partially filled request as if it were approved.
            payload = {k: payload[k] for k in ("source_description", "source_obs_id")}
        method = "POST"
        path = "/api/extraction/repair" + ("/preview" if args.command == "preview" else "")
    code, body = post(method, args.url.rstrip("/") + path, payload, token)
    output = {"http": code, **body}
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    if args.command == "preview" and args.request_out and code == 200:
        original = json.loads(Path(args.file).read_text())
        template = {"repair_id": "", "reason": "", "reviewer": "", "remove_edge_uuids": [],
                    "replacement": None, **original,
                    "expected_snapshot_sha256": body["snapshot"]["snapshot_sha256"]}
        target = Path(args.request_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(template, ensure_ascii=False, indent=2))
    # Full source data belongs in the explicit --out artifact, not terminal logs.
    summary = {k: v for k, v in output.items() if k not in ("snapshot", "plan", "repair")}
    if "snapshot" in body:
        summary["snapshot_sha256"] = body["snapshot"]["snapshot_sha256"]
        summary["source_status"] = body["snapshot"]["key"].get("status")
        summary["episode_uuid"] = (body["snapshot"].get("episode") or {}).get("uuid")
        summary["edge_count"] = len(body["snapshot"].get("edges", []))
    if "repair" in body:
        summary["repair"] = {k: body["repair"].get(k) for k in ("repair_id", "state", "request_sha256", "result")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if 200 <= code < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
