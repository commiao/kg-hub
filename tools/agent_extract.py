"""tools/agent_extract.py — Claude Code 会话自行抽取观测、经 /api/ingest_extracted 入主库的执行器。

背景（2026-10-17 用户决定）：对比旧模型（qwen/deepseek 经网关）存量与 Claude 系列抽取的新内容，
期间 kg_hub.entity_extract 断路器保持断开，抽取由会话自己做、会话模型在 haiku/opus/fable 间切换，
按 docs/DIGEST-RETRO-PROTOCOL.md 每 500 条复盘。本工具是那条循环里「取观测 → 提交抽取」的两头，
中间那一步（读观测、按 config/agent_extract_prompt.md 抽成 JSON）由会话自己完成。

子命令：
  next    取下一批待处理观测：从 NAS 的 claude-mem.db 副本按 id 游标读、过 utils/ingest_filter 质量闸
          （与 refinery 同一份 config）、再问 /api/ingest_extracted/check 跳过已入库的；写 batch 文件
  submit  把会话写好的抽取结果（按 id 对应 batch 里的观测）逐条 POST；每条的终态、纪律指标、耗时
          追加到 log.jsonl；游标推进到本批最大 id。**正式提交前必须对同一批做过 --dry-run**（四条红线之 2，
          docs/EXTRACTION-EVAL-FRAMEWORK.md §2）：--dry-run 成功后在状态目录记 dryrun-<batch_id>.json，
          正式提交时逐 id 核对，没预览过的 id 拒绝提交；确需跳过用 --skip-dryrun-check 明确表态
  dryrun-check  只做上面那个核对（rc 0 = 全部预览过，1 = 有没预览的），给 codex / qoder 的钩子用--executor / --run-id 是来源标记的另外两个维度
          （执行工具、实验 run；docs/EXTRACTION-EVAL-FRAMEWORK.md §1），缺省从 KG_HUB_EXECUTOR /
          KG_HUB_RUN_ID 取，都没有就不打
  status  按 extractor_model（或 --group-by run_id / executor）汇总 log.jsonl：条数、纪律指标均值、每条耗时、
          reuse_mismatch/条
  run     运行清单（框架 §3）：run start 建 ~/.kg-hub/agent-extract/runs/<run_id>.json（执行器、模型、
          规则版本、服务端 commit、旋钮快照、样本）；run end 写 ended_at 与自报成本（token、wall）。
          旋钮名必须在 tools/digest_retro.KNOBS 登记过
  prompt  打印抽取规则（给会话重读）

环境：KG_HUB_URL（默认 http://100.123.208.32:17171）、KG_HUB_API_TOKEN（submit/check 必需）、
      KG_HUB_NAS_SSH（默认 commiao@100.123.208.32）、KG_HUB_NAS_CLAUDE_MEM_DB
      （默认 /volume2/4T/kg-hub-data/claude-mem/claude-mem.db）、KG_HUB_AGENT_EXTRACT_DIR
      （状态与日志目录，默认 ~/.kg-hub/agent-extract）、KG_HUB_EXECUTOR / KG_HUB_RUN_ID
      （submit / run 的缺省来源标记）。token 只从环境变量读，不打印、不落日志。

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
PROMPT_VERSION = "agent-v4"   # v1→v2 第 1 轮（haiku）复盘；v2→v3 第 2 轮（opus）复盘；v3→v4 三模型 2000 条 chain-v8 定稿，见 config/agent_extract_prompt.md 末尾
DEFAULT_URL = "http://100.123.208.32:17171"
DEFAULT_NAS = "commiao@100.123.208.32"
DEFAULT_DB = "/volume2/4T/kg-hub-data/claude-mem/claude-mem.db"
DEFAULT_WATERMARK = "/volume2/4T/kg-hub-data/refinery-state/watermark.json"
# NAS 端的选取逻辑在 tools/agent_extract_remote.py（只依赖标准库，经 ssh 标准输入送过去执行）：
# 按 refinery 的 watermark（boundary_id 之后、不在 ingested/rejected/held 里）取「真正待处理」的观测。
_REMOTE_SCRIPT_PATH = Path(__file__).resolve().parent / "agent_extract_remote.py"


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


def _retro_bundle(stdin_text: str | None) -> bytes:
    """把本机的 tools/digest_retro.py（和可选的 lessons）打成 tar 送进容器。

    容器里那份 digest_retro 是上次发布的版本；复盘逻辑的修正不该等一次排空发布。
    digest_retro 只依赖标准库（schema/graphiti_client 在 /app 上按需 import）。"""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        src = Path(__file__).resolve().parent / "digest_retro.py"
        tar.add(src, arcname="retro/digest_retro.py")
        if stdin_text is not None:
            data = stdin_text.encode()
            info = tarfile.TarInfo("retro/lessons.md")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def remote_retro(args: list[str], stdin_text: str | None = None, runner=subprocess.run) -> int:
    """在容器内跑 digest_retro，透传退出码（gate: 2 = 该复盘了）。"""
    import shlex
    nas = os.environ.get("KG_HUB_NAS_SSH", DEFAULT_NAS)
    docker = os.environ.get("KG_HUB_NAS_DOCKER", "sudo -n /usr/local/bin/docker")
    inner = ("rm -rf /tmp/retro && mkdir -p /tmp/retro && tar -x -C /tmp && "
             "cd /app && PYTHONPATH=/app python3 /tmp/retro/digest_retro.py "
             + " ".join(shlex.quote(a) for a in args)
             + f" --state {RETRO_STATE} --journal {RETRO_JOURNAL} --report-dir {RETRO_REPORTS}"
             + (" --graph-count" if args and args[0] != "final" else "")
             + (" --lessons-file /tmp/retro/lessons.md" if stdin_text is not None else ""))
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", nas,
           f"{docker} exec -i kg-hub-server sh -c {shlex.quote(inner)}"]
    out = runner(cmd, input=_retro_bundle(stdin_text), capture_output=True, timeout=600)
    sys.stdout.write(out.stdout.decode(errors="replace"))
    if out.returncode not in (0, 2):
        sys.stderr.write(out.stderr.decode(errors="replace")[-2000:])
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

def fetch_remote_pending(after_id: int, limit: int) -> dict:
    """返回 {"rows": [...], "boundary", "after", "scanned", "skipped_by_watermark", "exhausted"}。"""
    nas = os.environ.get("KG_HUB_NAS_SSH", DEFAULT_NAS)
    db = os.environ.get("KG_HUB_NAS_CLAUDE_MEM_DB", DEFAULT_DB)
    wm = os.environ.get("KG_HUB_NAS_WATERMARK", DEFAULT_WATERMARK)
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", nas,
         "python3", "-", db, wm, str(after_id), str(limit)],
        input=_REMOTE_SCRIPT_PATH.read_text(), text=True, capture_output=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"ssh/sqlite 读取失败 rc={out.returncode}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def fetch_remote_rows(after_id: int, limit: int) -> list[dict]:
    return fetch_remote_pending(after_id, limit)["rows"]


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
                  prompt_version: str = PROMPT_VERSION, dry_run: bool = False,
                  executor: str | None = None, run_id: str | None = None) -> dict:
    return {
        **({"dry_run": True} if dry_run else {}),
        "name": item["name"], "episode_body": item["episode_body"],
        "source_description": item["source_description"], "source_obs_id": item["source_obs_id"],
        "reference_time": item["reference_time"], "sync": True,
        "extractor_model": model, "extractor_session": session, "prompt_version": prompt_version,
        # 服务端把 None 当「未打标」，与旧 episode 一致；只在给了值时才有分组意义
        "extractor_tool": executor, "run_id": run_id,
        "entities": [{"name": e["name"], "type": e["type"], "summary": e.get("summary", "")}
                     for e in result.get("entities", [])],
        "edges": [{"source": e["source"], "target": e["target"], "name": e["name"], "fact": e["fact"]}
                  for e in result.get("edges", [])],
    }


def submit_results(batch: dict, results: dict, *, model: str, session: str | None, url: str,
                   token: str | None, post=_http, log_path: Path | None = None,
                   dry_run: bool = False, names_path: Path | None = None,
                   executor: str | None = None, run_id: str | None = None,
                   prompt_version: str = PROMPT_VERSION) -> list[dict]:
    """dry_run：只让服务端做消解不写图，返回每个实体会并到谁；不记 log。
    prompt_version：随 episode 入图的规则版本；对比战役传开局快照的版本（+ 补充约定版本），不随仓库变。
    names_path：追加本会话已用过的实体名（第 2 轮复盘：靠记忆维护避坑名单不可靠）。
    executor / run_id：随 payload 入图，也记进 log.jsonl，status / eval_report 按它们分组。"""
    by_id = {int(i["id"]): i for i in batch["items"]}
    out = []
    # dry-run 按提交顺序累积前面条目会新建的实体，让服务端把它们算进消解候选（同批内误并，2026-10-22 补交 b0024）
    pending: list[dict] = []
    for res in results.get("items", [results] if "entities" in results else []):
        oid = int(res["id"])
        item = by_id.get(oid)
        if item is None:
            out.append({"id": oid, "status": "unknown_id"})
            continue
        if not res.get("entities"):
            rec = {"id": oid, "status": "empty", "model": model}
        else:
            payload = build_payload(item, res, model=model, session=session, dry_run=dry_run,
                                    executor=executor, run_id=run_id, prompt_version=prompt_version)
            if dry_run and pending:
                payload["dry_run_pending"] = list(pending)
            t0 = time.monotonic()
            code, d = post("POST", f"{url}/api/ingest_extracted", payload, token)
            if dry_run:
                pending.extend(d.get("new_entities") or [])
            # 消解到了不同名字的旧实体：这是误并的高发区，必须让执行者看见
            mismatch = [f"{k} → {v.get('matched_name')}{'（同批新建）' if v.get('in_batch') else ''}"
                        for k, v in (d.get("resolved") or {}).items()
                        if v.get("reused") and (v.get("matched_name") or "").lower() != k]
            rec = {"id": oid, "status": d.get("status") or f"http_{code}", "http": code, "model": model,
                   "latency_s": round(time.monotonic() - t0, 2), "episode_uuid": d.get("episode_uuid"),
                   "nodes_new": d.get("nodes_new"), "nodes_reused": d.get("nodes_reused"),
                   "edges_new": d.get("edges_new"), "edges_reused": d.get("edges_reused"),
                   "reuse_mismatch": mismatch,
                   "stats": d.get("stats"), "error": d.get("code") if code >= 400 else None}
        rec.update({"ts": datetime.now(tz=timezone.utc).isoformat(), "batch": batch.get("batch_id"),
                    "project": item.get("project"), "type": item.get("type"),
                    "executor": executor, "run_id": run_id})
        out.append(rec)
        if dry_run:
            continue
        if log_path is not None:
            with log_path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if names_path is not None and rec.get("status") == "ok":
            with names_path.open("a") as f:
                for e in res.get("entities", []):
                    f.write(f"{e.get('type')}\t{e.get('name')}\n")
    return out


# ---------- 提交前必须 dry-run：由工具自己执行，不靠执行器记得 ----------

def dryrun_marker_path(sd: Path, batch_id: str) -> Path:
    return sd / f"dryrun-{batch_id}.json"


def record_dryrun(sd: Path, batch_id: str, recs: list[dict], now: str | None = None) -> Path:
    """把这次 dry-run 预览过的 id（服务端真的回了 dry_run 的那些）追加进标记文件。
    同一批可以分几次 dry-run，取并集。"""
    p = dryrun_marker_path(sd, batch_id)
    seen: set[int] = set()
    if p.exists():
        seen.update(int(x) for x in json.loads(p.read_text()).get("ids", []))
    seen.update(int(r["id"]) for r in recs if r.get("status") == "dry_run")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"batch_id": batch_id, "ids": sorted(seen),
                             "ts": now or datetime.now(tz=timezone.utc).isoformat()}, ensure_ascii=False))
    return p


def dryrun_missing(sd: Path, batch_id: str, results: dict) -> list[int]:
    """results 里带实体的 id 中，哪些还没在这一批的 dry-run 标记里。空 entities 的 id 不要求（它不会提交）。"""
    p = dryrun_marker_path(sd, batch_id)
    seen = {int(x) for x in json.loads(p.read_text()).get("ids", [])} if p.exists() else set()
    items = results.get("items", [results] if "entities" in results else [])
    return sorted({int(r["id"]) for r in items if r.get("entities")} - seen)


SUMMARY_KEYS = ("model", "run_id", "executor")


def summarize_log(records: list[dict], key: str = "model") -> dict:
    """按 key（model / run_id / executor）汇总提交口径。旧记录没有 run_id / executor 字段时归入 '?'。"""
    if key not in SUMMARY_KEYS:
        raise ValueError(f"key 只能是 {SUMMARY_KEYS}")
    by_model: dict[str, dict] = {}
    for r in records:
        m = by_model.setdefault(r.get(key) or "?", {"n": 0, "ok": 0, "empty": 0, "error": 0,
                                                   "latency": [], "offschema": [], "dirty": [],
                                                   "unknown_type": [], "entities": [], "edges": [],
                                                   "mismatch": 0})
        m["n"] += 1
        st = r.get("status")
        if st == "ok":
            m["ok"] += 1
            m["latency"].append(r.get("latency_s") or 0)
            m["mismatch"] += len(r.get("reuse_mismatch") or [])
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
                  "entities_per_obs": avg(m["entities"]), "edges_per_obs": avg(m["edges"]),
                  # 消解口径：每条 ok 提交里「抽出的名字 → 复用到的不同名旧实体」的对数（框架 §4 表「消解」列）
                  "reuse_mismatch_per_obs": round(m["mismatch"] / m["ok"], 3) if m["ok"] else None}
    return out


# ---------- 运行清单（框架 §3）：一次实验一个 run_id，本地 runs/<run_id>.json ----------

# 执行器侧旋钮的当前快照：run start 时写进清单，之后改任何一项都该开新 run_id。
# 网关侧四个旋钮（gateway.*）是服务端环境变量，这里只记「本次实验假定的值」，真值以发布时的 compose 为准。
DEFAULT_KNOBS = {
    "type_guard_on_resolution": True, "resolution_strictness": "v3", "submit_dry_run": True,
    "gateway.post_normalize": True, "gateway.custom_extraction": True, "gateway.max_tokens": 8192,
    "gateway.edge_empty_retry": False,
}
RUN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}"


def runs_dir() -> Path:
    return state_dir() / "runs"


def run_path(run_id: str) -> Path:
    return runs_dir() / f"{run_id}.json"


def _known_knobs() -> set[str]:
    from tools.digest_retro import KNOBS  # 只依赖标准库，容器外也能 import
    return set(KNOBS)


def _parse_knobs(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"--knob 需要 key=value：{it}")
        k, v = it.split("=", 1)
        try:
            out[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            out[k.strip()] = v
    return out


def run_start(run_id: str, *, executor: str, model: str, sample: str | None = None,
              server_commit: str | None = None, executor_version: str | None = None,
              knobs: dict | None = None, prompt_version: str = PROMPT_VERSION,
              now: str | None = None, path: Path | None = None, known_knobs: set[str] | None = None) -> dict:
    """新建运行清单。旋钮名必须已在 digest_retro.KNOBS 登记（与 checkpoint 同一道闸：改了没登记的不算）。
    已存在同名 run 直接拒绝——一个 run_id 只对应一次实验。"""
    import re
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
        raise ValueError(f"run_id 只能用字母数字 . _ : / -，最长 80：{run_id}")
    if not executor or not model:
        raise ValueError("run start 需要 --executor 与 --model")
    known = _known_knobs() if known_knobs is None else known_knobs
    merged = {**DEFAULT_KNOBS, **(knobs or {})}
    unknown = [k for k in merged if k not in known]
    if unknown:
        raise ValueError(f"未登记的旋钮：{unknown}（先在 tools/digest_retro.KNOBS 登记）")
    p = path or run_path(run_id)
    if p.exists():
        raise FileExistsError(f"run 已存在：{p}（一个 run_id 只对应一次实验，改动请开新 run_id）")
    manifest = {
        "run_id": run_id, "executor": executor, "executor_version": executor_version,
        "model": model, "prompt_version": prompt_version, "server_commit": server_commit,
        "knobs": merged, "sample": sample,
        "started_at": now or datetime.now(tz=timezone.utc).isoformat(), "ended_at": None,
        "self_reported": {"input_tokens": 0, "output_tokens": 0, "wall_s": 0},
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def run_end(run_id: str, *, input_tokens: int | None = None, output_tokens: int | None = None,
            wall_s: float | None = None, now: str | None = None, path: Path | None = None) -> dict:
    """收尾：写 ended_at 与自报成本。wall_s 不给就按 started_at → ended_at 算。
    会话型执行器的成本账本里没有，只能自报（框架 §9）；报表里单列，不与账本口径相加。"""
    p = path or run_path(run_id)
    if not p.exists():
        raise FileNotFoundError(f"run 不存在：{p}（先 run start）")
    manifest = json.loads(p.read_text())
    ended = now or datetime.now(tz=timezone.utc).isoformat()
    manifest["ended_at"] = ended
    sr = manifest.setdefault("self_reported", {"input_tokens": 0, "output_tokens": 0, "wall_s": 0})
    if input_tokens is not None:
        sr["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        sr["output_tokens"] = int(output_tokens)
    if wall_s is not None:
        sr["wall_s"] = float(wall_s)
    elif manifest.get("started_at"):
        a = datetime.fromisoformat(manifest["started_at"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        sr["wall_s"] = round((b - a).total_seconds(), 1)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def load_run(run_id: str, path: Path | None = None) -> dict | None:
    p = path or run_path(run_id)
    return json.loads(p.read_text()) if p.exists() else None


# ---------- CLI ----------

def _load_state(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {"cursor": 0, "batches": 0}


def _save_state(p: Path, st: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["next", "submit", "status", "prompt", "retro", "run", "dryrun-check"])
    ap.add_argument("retro_args", nargs="*", help="retro：透传给 tools.digest_retro 的子命令与参数"
                    "（gate / status / final / checkpoint --adjust k=v --applied ...）；run：start / end")
    ap.add_argument("--executor", default=os.environ.get("KG_HUB_EXECUTOR") or None,
                    help="submit/run：执行工具名（claude-code / codex / qoder / script），缺省 KG_HUB_EXECUTOR")
    ap.add_argument("--run-id", default=os.environ.get("KG_HUB_RUN_ID") or None,
                    help="submit/run：实验 run_id，缺省 KG_HUB_RUN_ID")
    ap.add_argument("--group-by", choices=list(SUMMARY_KEYS), default="model", help="status：汇总键")
    ap.add_argument("--executor-version", help="run start：执行器版本（如 claude-code 2.1.x）")
    ap.add_argument("--sample", help="run start：冻结样本 id（如 ab-sample-20261017）或 id 区间")
    ap.add_argument("--server-commit", help="run start：服务端 kg-hub-server 的 commit")
    ap.add_argument("--knob", action="append", default=[], help="run start：key=value 旋钮快照，可多次；名字须在 KNOBS 登记")
    ap.add_argument("--input-tokens", type=int, help="run end：执行器自报输入 token")
    ap.add_argument("--output-tokens", type=int, help="run end：执行器自报输出 token")
    ap.add_argument("--wall-s", type=float, help="run end：执行器自报总耗时（秒），缺省按 started_at→ended_at")
    ap.add_argument("--lessons-file", help="retro checkpoint：经验教训 markdown（本机路径）")
    ap.add_argument("--dry-run", action="store_true", help="submit：只让服务端消解不写图，看每个实体会并到谁")
    ap.add_argument("--skip-dryrun-check", action="store_true",
                    help="submit：跳过「本批必须先 dry-run」的核对（默认拒绝没预览过的 id；跳过要写进 lessons）")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--after-id", type=int, help="next：覆盖游标起点")
    ap.add_argument("--out", help="next：batch 文件路径（默认 状态目录/batch-<id>.json）")
    ap.add_argument("--batch", help="submit：next 写出的 batch 文件")
    ap.add_argument("--file", help="submit：会话写的抽取结果 JSON（{items:[{id, entities, edges}]}）")
    ap.add_argument("--model", help="submit：extractor_model，如 claude-fable-5-1")
    ap.add_argument("--session", help="submit：extractor_session（会话 id）")
    ap.add_argument("--url", default=os.environ.get("KG_HUB_URL", DEFAULT_URL))
    # retro 的参数要原样透传给 digest_retro（如 --every 500 / --adjust k=v），所以用 parse_known_args
    a, extra = ap.parse_known_args(argv)
    if a.cmd == "retro":
        a.retro_args = list(a.retro_args) + list(extra)
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    token = api_token()
    sd = state_dir()
    state_path, log_path = sd / "state.json", sd / "log.jsonl"

    if a.cmd == "retro":
        lessons = Path(a.lessons_file).read_text() if a.lessons_file else None
        return remote_retro(a.retro_args, stdin_text=lessons)
    if a.cmd == "prompt":
        sys.stdout.write(PROMPT_PATH.read_text())
        return 0
    if a.cmd == "run":
        action = a.retro_args[0] if a.retro_args else None
        if action not in ("start", "end") or len(a.retro_args) != 1:
            ap.error("run 需要 start 或 end")
        if not a.run_id:
            ap.error("run 需要 --run-id（或 KG_HUB_RUN_ID）")
        try:
            if action == "start":
                if not (a.executor and a.model):
                    ap.error("run start 需要 --executor（或 KG_HUB_EXECUTOR）与 --model")
                m = run_start(a.run_id, executor=a.executor, model=a.model, sample=a.sample,
                              server_commit=a.server_commit, executor_version=a.executor_version,
                              knobs=_parse_knobs(a.knob))
            else:
                m = run_end(a.run_id, input_tokens=a.input_tokens, output_tokens=a.output_tokens, wall_s=a.wall_s)
        except (ValueError, FileExistsError, FileNotFoundError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps({"run": str(run_path(a.run_id)), **m}, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "status":
        recs = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()] if log_path.exists() else []
        print(json.dumps({"state": _load_state(state_path), "group_by": a.group_by,
                          "by_model": summarize_log(recs, a.group_by)},
                         ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "next":
        st = _load_state(state_path)
        after = a.after_id if a.after_id is not None else int(st.get("cursor", 0))
        cfg = load_config()
        remote = fetch_remote_pending(after, max(a.n * 4, 40))
        rows = remote["rows"]
        if not rows:
            print(json.dumps({"picked": 0, "message": "游标之后没有待处理观测了", "cursor": remote["after"],
                              "boundary": remote["boundary"], "exhausted": remote["exhausted"]}))
            return 0
        idents = [to_identity(r) for r in rows]
        existing = check_existing(idents, a.url, token)
        picked, stats = select_batch(rows, cfg, a.n, existing)
        stats.update({"boundary": remote["boundary"], "skipped_by_watermark": remote["skipped_by_watermark"]})
        st["batches"] = int(st.get("batches", 0)) + 1
        batch_id = f"b{st['batches']:04d}"
        # 游标只推进到「被挑中的最后一条」或「全部看过的最后一条」（没挑到 n 条时），
        # 中间被闸拒/已入库的不会再看第二遍。
        last_considered = picked[-1]["id"] if len(picked) >= a.n else rows[-1]["id"]
        # 前瞻：本批之后紧接着的观测标题（第 2 轮复盘：跨批的更正在下一批才出现，抽取者看不到）
        lookahead = [{"id": r["id"], "type": r.get("type"), "title": (r.get("title") or "")[:120]}
                     for r in rows if r["id"] > last_considered][:a.n]
        batch = {"batch_id": batch_id, "created_at": datetime.now(tz=timezone.utc).isoformat(),
                 "after_id": remote["after"], "last_considered_id": last_considered, "prompt": str(PROMPT_PATH),
                 "prompt_version": PROMPT_VERSION, "stats": stats, "items": picked, "lookahead": lookahead}
        out = Path(a.out) if a.out else sd / f"batch-{batch_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(batch, ensure_ascii=False, indent=1))
        st["cursor"] = last_considered
        st["last_batch"] = str(out)
        _save_state(state_path, st)
        print(json.dumps({"batch": str(out), "picked": len(picked), "cursor": last_considered,
                          **stats}, ensure_ascii=False))
        return 0
    if a.cmd == "dryrun-check":
        if not (a.batch and a.file):
            raise SystemExit("dryrun-check 需要 --batch --file")
        batch = json.loads(Path(a.batch).read_text())
        results = json.loads(Path(a.file).read_text())
        missing = dryrun_missing(sd, batch.get("batch_id") or "?", results)
        print(json.dumps({"batch": batch.get("batch_id"), "missing": missing, "ok": not missing}))
        return 1 if missing else 0
    # submit
    if not (a.batch and a.file and a.model):
        raise SystemExit("submit 需要 --batch --file --model")
    batch = json.loads(Path(a.batch).read_text())
    results = json.loads(Path(a.file).read_text())
    batch_id = batch.get("batch_id") or "?"
    if not a.dry_run and not a.skip_dryrun_check:
        missing = dryrun_missing(sd, batch_id, results)
        if missing:
            raise SystemExit(f"本批 {batch_id} 有 {len(missing)} 条还没 dry-run 过（id {missing[:10]}{'…' if len(missing) > 10 else ''}）；"
                             f"先 submit --dry-run，确需跳过加 --skip-dryrun-check 并写进 lessons")
    recs = submit_results(batch, results, model=a.model, session=a.session, url=a.url,
                          token=token, log_path=log_path, dry_run=a.dry_run,
                          names_path=sd / f"names-{a.model}.tsv",
                          executor=a.executor, run_id=a.run_id)
    if a.dry_run:
        record_dryrun(sd, batch_id, recs)
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(json.dumps({"submitted": len(recs), "by_status": counts, "dry_run": a.dry_run,
                      "executor": a.executor, "run_id": a.run_id,
                      "items": [{k: r.get(k) for k in ("id", "status", "nodes_new", "nodes_reused",
                                                        "edges_new", "edges_reused", "reuse_mismatch", "error")}
                                for r in recs]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
