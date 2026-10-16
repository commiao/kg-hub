"""tools/agent_extract.py — Claude Code 会话自行抽取观测、经 /api/ingest_extracted 入主库的执行器。

背景（2026-10-17 用户决定）：对比旧模型（qwen/deepseek 经网关）存量与 Claude 系列抽取的新内容，
期间 kg_hub.entity_extract 断路器保持断开，抽取由会话自己做、会话模型在 haiku/opus/fable 间切换，
按 docs/DIGEST-RETRO-PROTOCOL.md 每 500 条复盘。本工具是那条循环里「取观测 → 提交抽取」的两头，
中间那一步（读观测、按 config/agent_extract_prompt.md 抽成 JSON）由会话自己完成。

子命令：
  next    取下一批待处理观测：从 NAS 的 claude-mem.db 副本按 id 游标读、过 utils/ingest_filter 质量闸
          （与 refinery 同一份 config）、再问 /api/ingest_extracted/check 跳过已入库的；写 batch 文件
  submit  把会话写好的抽取结果（按 id 对应 batch 里的观测）逐条 POST；每条的终态、纪律指标、耗时
          追加到 log.jsonl；游标推进到本批最大 id
  status  按 extractor_model 汇总 log.jsonl：条数、纪律指标均值、每条耗时
  prompt  打印抽取规则（给会话重读）

环境：KG_HUB_URL（默认 http://100.123.208.32:17171）、KG_HUB_API_TOKEN（submit/check 必需）、
      KG_HUB_NAS_SSH（默认 commiao@100.123.208.32）、KG_HUB_NAS_CLAUDE_MEM_DB
      （默认 /volume2/4T/kg-hub-data/claude-mem/claude-mem.db）、KG_HUB_AGENT_EXTRACT_DIR
      （状态与日志目录，默认 ~/.kg-hub/agent-extract）。token 只从环境变量读，不打印、不落日志。

只读 claude-mem 副本；写入只经 kg-hub 的治理接口；不调模型、不动断路器。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ingest_filter import evaluate, load_config  # noqa: E402

PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "agent_extract_prompt.md"
PROMPT_VERSION = "agent-v1"
DEFAULT_URL = "http://100.123.208.32:17171"
DEFAULT_NAS = "commiao@100.123.208.32"
DEFAULT_DB = "/volume2/4T/kg-hub-data/claude-mem/claude-mem.db"
# 与 kg_refinery.fetch_rows 逐字段一致；platform_source 供质量闸按平台定阈值。
_REMOTE_SQL = (
    "SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, o.concepts, "
    "o.files_read, o.files_modified, o.created_at, o.content_hash, o.generated_by_model, "
    "o.relevance_count, s.platform_source FROM observations o "
    "LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
    "WHERE o.id > ? ORDER BY o.id ASC LIMIT ?")
_REMOTE_SCRIPT = """
import json, sqlite3, sys
db, after, limit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
c = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True); c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute(%r, (after, limit))]
print(json.dumps(rows, ensure_ascii=False))
""" % _REMOTE_SQL


def state_dir() -> Path:
    return Path(os.environ.get("KG_HUB_AGENT_EXTRACT_DIR", "~/.kg-hub/agent-extract")).expanduser()


# token 依次找：环境变量 → ~/.claude-mem/.env → ~/.openclaw/env.sh（与 kg-use skill 同一顺序）。
# 在 Python 里读而不是在 shell 里 grep，是因为本机的 Bash 钩子会拦截任何提到 .env 的命令；
# 值只用于 Authorization 头，不打印、不落日志、不进 batch/results 文件。
_TOKEN_FILES = ("~/.claude-mem/.env", "~/.openclaw/env.sh")


def api_token(env: dict | None = None, files: tuple[str, ...] = _TOKEN_FILES) -> str | None:
    env = os.environ if env is None else env
    for key in ("KG_HUB_API_TOKEN", "KG_HUB_TOKEN"):
        if env.get(key):
            return env[key].strip()
    for f in files:
        p = Path(f).expanduser()
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            for key in ("KG_HUB_API_TOKEN=", "KG_HUB_TOKEN="):
                if line.startswith(key):
                    val = line[len(key):].strip().strip('"').strip("'")
                    if val:
                        return val
    return None


# 500 条门禁与复盘要读 NAS 上的账本和图库，所以转发到 kg-hub-server 容器里执行。
RETRO_STATE = "/backup/digest-retro.json"
RETRO_JOURNAL = "/backup/model-attempts.sqlite3"
RETRO_REPORTS = "/backup/retro-reports"


def remote_retro(args: list[str], stdin_text: str | None = None, runner=subprocess.run) -> int:
    """在容器内跑 tools.digest_retro，透传退出码（gate: 2 = 该复盘了）。"""
    nas = os.environ.get("KG_HUB_NAS_SSH", DEFAULT_NAS)
    docker = os.environ.get("KG_HUB_NAS_DOCKER", "sudo -n /usr/local/bin/docker")
    inner = (("cat > /tmp/retro-lessons.md; " if stdin_text is not None else "")
             + "python3 -m tools.digest_retro " + " ".join(
                 __import__("shlex").quote(a) for a in args)
             + f" --state {RETRO_STATE} --journal {RETRO_JOURNAL} --report-dir {RETRO_REPORTS}"
             + (" --lessons-file /tmp/retro-lessons.md" if stdin_text is not None else ""))
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", nas,
           f"{docker} exec -i kg-hub-server sh -c {__import__('shlex').quote(inner)}"]
    out = runner(cmd, input=stdin_text, text=True, capture_output=True, timeout=600)
    sys.stdout.write(out.stdout)
    if out.returncode not in (0, 2):
        sys.stderr.write(out.stderr[-2000:])
    return out.returncode


# ---------- 正文/payload：与 refinery 完全一致，保证新旧内容的 episode 名与幂等键相同 ----------

def _jlist(field) -> list[str]:
    if not field:
        return []
    try:
        v = json.loads(field)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def build_episode_body(obs: dict) -> str:
    """镜像 kg_refinery.build_episode_body（不 import 它：那个模块一加载就读 refinery 的环境）。"""
    parts = [f"[{(obs.get('type') or 'obs').upper()}] {obs.get('title') or '(untitled)'}"]
    if obs.get("subtitle"):
        parts.append(obs["subtitle"])
    parts.append("")
    if obs.get("narrative"):
        parts += ["Narrative:", obs["narrative"], ""]
    facts = _jlist(obs.get("facts"))
    if facts:
        parts += ["Key facts:"] + [f"- {f}" for f in facts] + [""]
    concepts = _jlist(obs.get("concepts"))
    if concepts:
        parts.append(f"Concepts: {', '.join(concepts)}")
    fm = _jlist(obs.get("files_modified"))
    if fm:
        parts.append(f"Files modified: {', '.join(fm[:20])}")
    fr = _jlist(obs.get("files_read"))
    if fr:
        parts.append(f"Files read: {', '.join(fr[:20])}")
    parts.append(f"Project: {obs.get('project', '?')}")
    return "\n".join(parts)


def to_identity(obs: dict) -> dict:
    return {
        "id": obs["id"],
        "name": f"claude-mem-obs-{obs['id']}",
        "source_description": (
            f"claude-mem obs id={obs['id']} project={obs.get('project', '?')} "
            f"type={obs.get('type', '?')} platform={obs.get('platform_source') or '_default'}"),
        "source_obs_id": obs.get("content_hash") or f"claude-mem-id-{obs['id']}",
        "reference_time": obs.get("created_at") or datetime.now(tz=timezone.utc).isoformat(),
        "episode_body": build_episode_body(obs),
        "project": obs.get("project"), "type": obs.get("type"),
    }


# ---------- 取观测 ----------

def fetch_remote_rows(after_id: int, limit: int) -> list[dict]:
    nas = os.environ.get("KG_HUB_NAS_SSH", DEFAULT_NAS)
    db = os.environ.get("KG_HUB_NAS_CLAUDE_MEM_DB", DEFAULT_DB)
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", nas,
         "python3", "-", db, str(after_id), str(limit)],
        input=_REMOTE_SCRIPT, text=True, capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"ssh/sqlite 读取失败 rc={out.returncode}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def _http(method: str, url: str, body: dict | None, token: str | None, timeout: int = 120):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def check_existing(items: list[dict], url: str, token: str | None, post=_http) -> dict[str, str | None]:
    """返回 {source_obs_id: status}；None 表示图里没有键。"""
    out: dict[str, str | None] = {}
    for i in range(0, len(items), 200):
        chunk = items[i:i + 200]
        code, d = post("POST", f"{url}/api/ingest_extracted/check",
                       {"items": [{"source_description": x["source_description"],
                                   "source_obs_id": x["source_obs_id"]} for x in chunk]}, token)
        if code != 200:
            raise RuntimeError(f"check 失败 http {code}: {d.get('code') or d.get('message')}")
        for r in d.get("items", []):
            out[r["source_obs_id"]] = r.get("status")
    return out


def select_batch(rows: list[dict], cfg: dict, n: int, existing: dict[str, str | None]) -> tuple[list[dict], dict]:
    """质量闸 + 跳过已入库/抽取中。返回 (batch_items, 统计)。"""
    stats = {"seen": len(rows), "rejected_by_filter": 0, "already_ok": 0, "pending_elsewhere": 0, "picked": 0}
    picked: list[dict] = []
    for obs in rows:
        d = evaluate(obs, cfg, None)
        if not d.accept:
            stats["rejected_by_filter"] += 1
            continue
        ident = to_identity(obs)
        st = existing.get(ident["source_obs_id"])
        if st == "ok":
            stats["already_ok"] += 1
            continue
        if st == "pending":
            stats["pending_elsewhere"] += 1
            continue
        picked.append(ident)
        if len(picked) >= n:
            break
    stats["picked"] = len(picked)
    return picked, stats


def build_payload(item: dict, result: dict, *, model: str, session: str | None,
                  prompt_version: str = PROMPT_VERSION) -> dict:
    return {
        "name": item["name"], "episode_body": item["episode_body"],
        "source_description": item["source_description"], "source_obs_id": item["source_obs_id"],
        "reference_time": item["reference_time"], "sync": True,
        "extractor_model": model, "extractor_session": session, "prompt_version": prompt_version,
        "entities": [{"name": e["name"], "type": e["type"], "summary": e.get("summary", "")}
                     for e in result.get("entities", [])],
        "edges": [{"source": e["source"], "target": e["target"], "name": e["name"], "fact": e["fact"]}
                  for e in result.get("edges", [])],
    }


def submit_results(batch: dict, results: dict, *, model: str, session: str | None, url: str,
                   token: str | None, post=_http, log_path: Path | None = None) -> list[dict]:
    by_id = {int(i["id"]): i for i in batch["items"]}
    out = []
    for res in results.get("items", [results] if "entities" in results else []):
        oid = int(res["id"])
        item = by_id.get(oid)
        if item is None:
            out.append({"id": oid, "status": "unknown_id"})
            continue
        if not res.get("entities"):
            rec = {"id": oid, "status": "empty", "model": model}
        else:
            payload = build_payload(item, res, model=model, session=session)
            t0 = time.monotonic()
            code, d = post("POST", f"{url}/api/ingest_extracted", payload, token)
            rec = {"id": oid, "status": d.get("status") or f"http_{code}", "http": code, "model": model,
                   "latency_s": round(time.monotonic() - t0, 2), "episode_uuid": d.get("episode_uuid"),
                   "nodes_new": d.get("nodes_new"), "nodes_reused": d.get("nodes_reused"),
                   "edges_new": d.get("edges_new"), "edges_reused": d.get("edges_reused"),
                   "stats": d.get("stats"), "error": d.get("code") if code >= 400 else None}
        rec.update({"ts": datetime.now(tz=timezone.utc).isoformat(), "batch": batch.get("batch_id"),
                    "project": item.get("project"), "type": item.get("type")})
        out.append(rec)
        if log_path is not None:
            with log_path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out


def summarize_log(records: list[dict]) -> dict:
    by_model: dict[str, dict] = {}
    for r in records:
        m = by_model.setdefault(r.get("model") or "?", {"n": 0, "ok": 0, "empty": 0, "error": 0,
                                                       "latency": [], "offschema": [], "dirty": [],
                                                       "unknown_type": [], "entities": [], "edges": []})
        m["n"] += 1
        st = r.get("status")
        if st == "ok":
            m["ok"] += 1
            m["latency"].append(r.get("latency_s") or 0)
            s = r.get("stats") or {}
            if s.get("entities_in"):
                m["offschema"].append(s.get("offschema_edge", 0) / max(1, s.get("edges_in", 1)))
                m["dirty"].append(s.get("dirty_name", 0) / s["entities_in"])
                m["unknown_type"].append(s.get("unknown_type", 0) / s["entities_in"])
                m["entities"].append(s.get("entities_out", 0))
                m["edges"].append(s.get("edges_out", 0))
        elif st == "empty":
            m["empty"] += 1
        elif st != "skipped":
            m["error"] += 1
    out = {}
    for k, m in by_model.items():
        avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
        out[k] = {"n": m["n"], "ok": m["ok"], "empty": m["empty"], "error": m["error"],
                  "latency_med_s": round(statistics.median(m["latency"]), 2) if m["latency"] else None,
                  "offschema_edge_share": avg(m["offschema"]), "dirty_name_share": avg(m["dirty"]),
                  "unknown_type_share": avg(m["unknown_type"]),
                  "entities_per_obs": avg(m["entities"]), "edges_per_obs": avg(m["edges"])}
    return out


# ---------- CLI ----------

def _load_state(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {"cursor": 0, "batches": 0}


def _save_state(p: Path, st: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["next", "submit", "status", "prompt", "retro"])
    ap.add_argument("retro_args", nargs="*", help="retro：透传给 tools.digest_retro 的子命令与参数"
                    "（gate / status / final / checkpoint --adjust k=v --applied ...）")
    ap.add_argument("--lessons-file", help="retro checkpoint：经验教训 markdown（本机路径）")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--after-id", type=int, help="next：覆盖游标起点")
    ap.add_argument("--out", help="next：batch 文件路径（默认 状态目录/batch-<id>.json）")
    ap.add_argument("--batch", help="submit：next 写出的 batch 文件")
    ap.add_argument("--file", help="submit：会话写的抽取结果 JSON（{items:[{id, entities, edges}]}）")
    ap.add_argument("--model", help="submit：extractor_model，如 claude-fable-5-1")
    ap.add_argument("--session", help="submit：extractor_session（会话 id）")
    ap.add_argument("--url", default=os.environ.get("KG_HUB_URL", DEFAULT_URL))
    a = ap.parse_args(argv)
    token = api_token()
    sd = state_dir()
    state_path, log_path = sd / "state.json", sd / "log.jsonl"

    if a.cmd == "retro":
        lessons = Path(a.lessons_file).read_text() if a.lessons_file else None
        return remote_retro(a.retro_args, stdin_text=lessons)
    if a.cmd == "prompt":
        sys.stdout.write(PROMPT_PATH.read_text())
        return 0
    if a.cmd == "status":
        recs = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()] if log_path.exists() else []
        print(json.dumps({"state": _load_state(state_path), "by_model": summarize_log(recs)},
                         ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "next":
        st = _load_state(state_path)
        after = a.after_id if a.after_id is not None else int(st.get("cursor", 0))
        cfg = load_config()
        rows = fetch_remote_rows(after, max(a.n * 4, 40))
        if not rows:
            print(json.dumps({"picked": 0, "message": "游标之后没有观测了", "cursor": after}))
            return 0
        idents = [to_identity(r) for r in rows]
        existing = check_existing(idents, a.url, token)
        picked, stats = select_batch(rows, cfg, a.n, existing)
        st["batches"] = int(st.get("batches", 0)) + 1
        batch_id = f"b{st['batches']:04d}"
        # 游标只推进到「被挑中的最后一条」或「全部看过的最后一条」（没挑到 n 条时），
        # 中间被闸拒/已入库的不会再看第二遍。
        last_considered = picked[-1]["id"] if len(picked) >= a.n else rows[-1]["id"]
        batch = {"batch_id": batch_id, "created_at": datetime.now(tz=timezone.utc).isoformat(),
                 "after_id": after, "last_considered_id": last_considered, "prompt": str(PROMPT_PATH),
                 "prompt_version": PROMPT_VERSION, "stats": stats, "items": picked}
        out = Path(a.out) if a.out else sd / f"batch-{batch_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(batch, ensure_ascii=False, indent=1))
        st["cursor"] = last_considered
        st["last_batch"] = str(out)
        _save_state(state_path, st)
        print(json.dumps({"batch": str(out), "picked": len(picked), "cursor": last_considered,
                          **stats}, ensure_ascii=False))
        return 0
    # submit
    if not (a.batch and a.file and a.model):
        raise SystemExit("submit 需要 --batch --file --model")
    batch = json.loads(Path(a.batch).read_text())
    results = json.loads(Path(a.file).read_text())
    recs = submit_results(batch, results, model=a.model, session=a.session, url=a.url,
                          token=token, log_path=log_path)
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(json.dumps({"submitted": len(recs), "by_status": counts,
                      "items": [{k: r.get(k) for k in ("id", "status", "nodes_new", "nodes_reused",
                                                        "edges_new", "edges_reused", "error")} for r in recs]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
