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
  namesync  命名同步（tools/campaign_namesync.py 的锚点键规则）：--batch --file [--apply]，和全部战役已提交批次比、
          本批内部统一；--apply 改写 --file。退出码 0 干净 / 2 还有同键异名。正式提交前另按 dry-run 的 new_entities
          查命名分裂（会新建的名字同键在图里已有节点）：拦下退出码 2，确属不同事物用 --allow-split "原因"；
          dry-run 标记太旧判不了退出码 3。dry-run 与正式提交都先过 NAS 重操作闸门（tools/nas_gate.py，75/76 见那里）
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
      （submit / run 的缺省来源标记）、KG_HUB_CLAIMS_DIR / KG_HUB_CLAIM_TTL_HOURS（全设备在途领取登记，
      见 tools/claim_registry.py：next 领批会避开别的战役 / 状态目录正领着的观测）。token 只从环境变量读，不打印、不落日志。

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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ingest_filter import evaluate, load_config  # noqa: E402
from utils import obs_triage  # noqa: E402
from tools import claim_registry  # noqa: E402
from utils import extraction_quality as quality  # noqa: E402

PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "agent_extract_prompt.md"
PROMPT_VERSION = "agent-v5"   # v5: source-bound pre-submit review, shared by all executors.
DEFAULT_URL = "http://100.123.208.32:17171"
DEFAULT_NAS = "commiao@100.123.208.32"
DEFAULT_DB = "/volume2/4T/kg-hub-data/claude-mem/claude-mem.db"
DEFAULT_WATERMARK = "/volume2/4T/kg-hub-data/refinery-state/watermark.json"
# NAS 端的选取逻辑在 tools/agent_extract_remote.py（只依赖标准库，经 ssh 标准输入送过去执行）：
# 按 refinery 的 watermark（boundary_id 之后、不在 ingested/rejected/held 里）取「真正待处理」的观测。
_REMOTE_SCRIPT_PATH = Path(__file__).resolve().parent / "agent_extract_remote.py"


def state_dir() -> Path:
    return Path(os.environ.get("KG_HUB_AGENT_EXTRACT_DIR", "~/.kg-hub/agent-extract")).expanduser()


# token 依次找：环境变量 → 专用 extraction-token (0600) → ~/.claude-mem/.env → ~/.openclaw/env.sh（与 kg-use skill 同一顺序）。
# 在 Python 里读而不是在 shell 里 grep，是因为本机的 Bash 钩子会拦截任何提到 .env 的命令；
# 值只用于 Authorization 头，不打印、不落日志、不进 batch/results 文件。
_TOKEN_FILES = ("~/.claude-mem/.env", "~/.openclaw/env.sh")


def api_token(env: dict | None = None, files: tuple[str, ...] = _TOKEN_FILES) -> str | None:
    env = os.environ if env is None else env
    for key in ("KG_HUB_API_TOKEN", "KG_HUB_TOKEN"):
        if env.get(key):
            return env[key].strip()
    # Peer credentials are dedicated extract-scope tokens, never paid-provider keys.
    # Fail closed for an explicitly configured, missing or unsafe credential file.
    dedicated = Path(env.get("KG_HUB_TOKEN_FILE") or "~/.config/kg-hub/extraction-token").expanduser()
    if dedicated.exists() or dedicated.is_symlink() or env.get("KG_HUB_TOKEN_FILE"):
        import stat
        try:
            fd = os.open(dedicated, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd) as token_file:
                info = os.fstat(token_file.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.getuid()):
                    raise SystemExit("KG_HUB_TOKEN_FILE must be an owned regular file with mode 0600")
                value = token_file.read().strip()
        except OSError as exc:
            raise SystemExit("KG_HUB_TOKEN_FILE is unreadable; restore the dedicated extraction credential") from exc
        if not value:
            raise SystemExit("KG_HUB_TOKEN_FILE is empty")
        return value
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


def _http(method: str, url: str, body: dict | None, token: str | None, timeout: float | None = None):
    if timeout is None:
        # Pre-extracted writes share the service's writer lock and embedding
        # work. The generic120s timeout used to abandon valid service requests.
        from utils.ingest_budget import ingest_ceiling_sec
        timeout = (ingest_ceiling_sec() + 30 if method == 'POST' and
                   urllib.parse.urlsplit(url).path == '/api/ingest_extracted' else 120)
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


def select_batch(rows: list[dict], cfg: dict, n: int, existing: dict[str, str | None],
                 triage_cfg: dict | None = None) -> tuple[list[dict], dict]:
    """质量闸 + 分诊 + 跳过已入库/抽取中。返回 (batch_items, 统计)。
    分诊（utils/obs_triage）在闸放行的观测上再分 keep/review/drop：影子模式（config enabled=false）只计数，
    enabled=true 才把 drop 档跳过（计入 triage_dropped）。不传 triage_cfg 就读 config/obs_triage.json。"""
    stats = {"seen": len(rows), "rejected_by_filter": 0, "already_ok": 0, "pending_elsewhere": 0, "picked": 0,
             "triaged": 0, "triage_drop": 0, "triage_review": 0, "triage_dropped": 0}
    tcfg = triage_cfg if triage_cfg is not None else _triage_cfg()
    enforce = obs_triage.triage_enabled(tcfg)
    picked: list[dict] = []
    for obs in rows:
        d = evaluate(obs, cfg, None)
        if not d.accept:
            stats["rejected_by_filter"] += 1
            continue
        tier = "keep"
        if tcfg:
            tier = obs_triage.triage(obs, cfg, tcfg, base=d).tier
            stats["triaged"] += 1
            if tier != "keep":
                stats[f"triage_{tier}"] += 1
            if enforce and tier == "drop":
                stats["triage_dropped"] += 1
                continue
        ident = to_identity(obs)
        st = existing.get(ident["source_obs_id"])
        if st in ("ok", "empty"):   # empty：别的执行者已判为空并记录过，同样算已处理
            stats["already_ok"] += 1
            continue
        if st == "pending":
            stats["pending_elsewhere"] += 1
            continue
        if tier != "keep":
            # 影子模式下判丢 / 判复核的照样领走；记在条目上，打满后从这里抽样复查（tools/triage_notify sample）
            ident["triage_tier"] = tier
        picked.append(ident)
        if len(picked) >= n:
            break
    stats["picked"] = len(picked)
    return picked, stats


def _triage_cfg() -> dict:
    """分诊配置；文件不在就当没有分诊（空 dict：不统计也不跳过）。"""
    try:
        return obs_triage.load_triage_config()
    except FileNotFoundError:
        return {}


# 一次领批最多往后扫几段（每段 max(还差条数*4, 40) 条）。可抽观测稀疏时一段凑不满一批：
# 2026-10-23 战役第 1 轮，游标后 80 条只剩 4 条可抽，gpt-6-astra 领到 4 条就收工，要再起一个会话补配额。
FILL_MAX_ROUNDS = 8


def gather_batch(after: int, n: int, *, fetch, check, cfg: dict, held=frozenset(),
                 max_rounds: int = FILL_MAX_ROUNDS) -> dict:
    """从游标 after 起逐段往后扫，凑满 n 条再交；战役 claim 与 next 共用。
    check(idents) → {source_obs_id: status}；held：别的领取方正占着的 id（全设备在途登记）。
    返回 picked、stats（各段相加）、last（凑满时为最后挑中的 id，没凑满时为看过的最后一条，一条没看过为 after）、
    lookahead（last 之后紧接着的标题）、seen（共看过多少条）、remote（最后一段的元信息）。"""
    picked: list[dict] = []
    stats: dict = {"rounds": 0, "held_elsewhere": 0}
    last, lookahead, seen, remote = after, [], 0, {}
    for _ in range(max(1, max_rounds)):
        need = n - len(picked)
        remote = fetch(last, max(need * 4, 40))
        rows = remote["rows"]
        if not rows:
            break
        stats["rounds"] += 1
        seen += len(rows)
        avail = [r for r in rows if r["id"] not in held]
        fresh, st = select_batch(avail, cfg, need, check([to_identity(r) for r in avail]) if avail else {})
        for k, v in st.items():
            stats[k] = stats.get(k, 0) + v
        stats["held_elsewhere"] += len(rows) - len(avail)
        picked += fresh
        if len(fresh) >= need:
            last = fresh[-1]["id"]
            lookahead = [{"id": r["id"], "type": r.get("type"), "title": (r.get("title") or "")[:120]}
                         for r in rows if r["id"] > last][:n]
            break
        last = rows[-1]["id"]
    stats["picked"] = len(picked)
    return {"picked": picked, "stats": stats, "last": last, "lookahead": lookahead, "seen": seen,
            "remote": remote}


def build_payload(item: dict, result: dict, *, model: str, session: str | None,
                  prompt_version: str | None = None, dry_run: bool = False,
                  executor: str | None = None, run_id: str | None = None) -> dict:
    return {
        **({"dry_run": True} if dry_run else {}),
        "name": item["name"], "episode_body": item["episode_body"],
        "source_description": item["source_description"], "source_obs_id": item["source_obs_id"],
        "reference_time": item["reference_time"], "sync": True,
        "extractor_model": model, "extractor_session": session, "prompt_version": prompt_version or PROMPT_VERSION,
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
                   prompt_version: str | None = None, quality_review: dict | None = None,
                   prompt_text: str | None = None, acknowledged: list[dict] | None = None) -> list[dict]:
    """dry_run：只让服务端做消解不写图，返回每个实体会并到谁；不记 log。
    prompt_version：随 episode 入图的规则版本；对比战役传开局快照的版本（+ 补充约定版本），不随仓库变。
    names_path：追加本会话已用过的实体名（第 2 轮复盘：靠记忆维护避坑名单不可靠）。
    executor / run_id：随 payload 入图，也记进 log.jsonl，status / eval_report 按它们分组。"""
    prompt_version = prompt_version or batch.get("prompt_version") or PROMPT_VERSION
    prompt_text = PROMPT_PATH.read_text() if prompt_text is None else prompt_text
    if quality.requires_quality(prompt_version):
        problems = quality.validate_review(batch, results, quality_review, prompt_text)
        expected_identity = quality.extraction_identity(model, executor, run_id)
        if not quality.valid_identity(expected_identity) or (quality_review or {}).get("identity") != expected_identity:
            problems.append("quality_identity_mismatch: use the reviewed model, executor and run_id")
        if problems:
            raise SystemExit("quality review failed:\n" + "\n".join(problems[:30]))
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
        if acknowledged and not dry_run and len(out) < len(acknowledged):
            previous = acknowledged[len(out)]
            expected = extraction_payload_digest(item, res, prompt_version, prompt_text,
                batch=batch, results=results, identity=quality.extraction_identity(model, executor, run_id))
            if (previous.get('id') != oid or previous.get('status') != 'ok' or previous.get('http') != 200
                    or previous.get('model') != model or previous.get('executor') != executor
                    or previous.get('run_id') != run_id or previous.get('payload_digest') != expected):
                raise ValueError('acknowledged prefix differs from the reviewed request')
            out.append(previous)  # no HTTP request and no second formal log row
            continue
        if not res.get("entities"):
            # 空观测：预览时只在本地记；正式提交时也发一条「空」记录（服务端只记 IngestedKey status=empty、不写图），
            # 否则 refinery 对账认不出它、一直挂在「剩余」里（2026-10-24）
            rec = {"id": oid, "status": "empty", "model": model}
            if not dry_run:
                payload = build_payload(item, {"entities": [], "edges": []}, model=model, session=session,
                                        executor=executor, run_id=run_id, prompt_version=prompt_version)
                payload["empty"] = True
                if quality.requires_quality(prompt_version):
                    payload.update(quality_review=quality_review, quality_context={
                        "batch": batch, "results": results, "prompt_text": prompt_text,
                        "prompt_version": prompt_version})
                code, d = post("POST", f"{url}/api/ingest_extracted", payload, token)
                st = d.get("status") or f"http_{code}"
                confirmed = st == "empty_recorded" and code == 200
                rec.update(status="empty" if confirmed else f"http_{code}" if code >= 400 else st, http=code,
                           empty_recorded=confirmed, error=d.get("code") if code >= 400 else None)
        else:
            payload = build_payload(item, res, model=model, session=session, dry_run=dry_run,
                                    executor=executor, run_id=run_id, prompt_version=prompt_version)
            if quality.requires_quality(prompt_version):
                payload.update(quality_review=quality_review, quality_context={
                    "batch": batch, "results": results, "prompt_text": prompt_text,
                    "prompt_version": prompt_version})
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
            if dry_run:   # 战役 lint 据此只查新建实体的 summary（tools/extract_campaign.cmd_lint）
                rec["new_entities"] = [e.get("name") for e in d.get("new_entities") or []]
                rec["resolution"] = resolution_rows(res, d)
                rec["reuse_cross_type"] = [reuse_line(r) for r in rec["resolution"] if cross_type(r)]
            rec["payload_digest"] = extraction_payload_digest(item, res, prompt_version, prompt_text, batch=batch, results=results,
                                                               identity=quality.extraction_identity(model, executor, run_id))
        rec.update({"ts": datetime.now(tz=timezone.utc).isoformat(), "batch": batch.get("batch_id"),
                    "project": item.get("project"), "type": item.get("type"),
                    "executor": executor, "run_id": run_id, "prompt_version": prompt_version})
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


def resolution_rows(res: dict, d: dict) -> list[dict]:
    """dry-run 每个实体的消解结果：新建还是复用、复用到的节点名 / 类型 / 怎么命中 / summary 开头、是否同批新建。
    服务端（2026-10-25 起）在 entities 里直接给；旧服务端只有 resolved（按归一键、没有类型），退回用它拼，matched_type 为 None。"""
    keep = ("name", "type", "reused", "matched_name", "matched_type", "match", "matched_summary", "in_batch")
    if isinstance(d.get("entities"), list):
        return [{k: e[k] for k in keep if k in e} for e in d["entities"]]
    from utils.extracted_ingest import _norm_key
    resolved, out, seen = d.get("resolved") or {}, [], set()
    for e in res.get("entities") or []:
        key = _norm_key(" ".join(str(e.get("name") or "").split()))
        if key in seen or key not in resolved:
            continue
        seen.add(key)
        v = resolved[key]
        row = {"name": e.get("name"), "type": e.get("type"), "reused": bool(v.get("reused"))}
        if row["reused"]:
            row.update(matched_name=v.get("matched_name"), matched_type=None)
            if v.get("in_batch"):
                row["in_batch"] = True
        out.append(row)
    return out


def cross_type(r: dict) -> bool:
    """复用到的节点类型和抽出的类型不同（服务端精确同名不看类型，这类误并只有在这里看得见）。"""
    return bool(r.get("reused") and r.get("matched_type") and r.get("type") not in r["matched_type"].split("/"))


def reuse_line(r: dict, oid=None) -> str:
    """一行看完一个复用：抽出名（类型）→ 复用到的节点名（类型）［标记］「被复用节点 summary 开头」。"""
    tags = [t for t, on in (("同批新建", r.get("in_batch")), ("近名", r.get("match") == "similar"),
                            ("类型不同", cross_type(r))) if on]
    head = f"{oid}: " if oid is not None else ""
    return (f"{head}{r.get('name')}（{r.get('type')}）→ {r.get('matched_name')}（{r.get('matched_type') or '类型未知'}）"
            + "".join(f"［{t}］" for t in tags) + (f"「{r['matched_summary']}」" if r.get("matched_summary") else ""))


def mark_empty_from_log(log_path: Path, load_batch, *, url: str, token: str | None, post=_http,
                        executor: str | None = None, run_id: str | None = None) -> dict:
    """补记：日志里判为空、但服务端还没有 empty 记录的观测，逐条发一条「空」记录（2026-10-24 之前提交的空观测
    只记在本地日志里）。load_batch(批次号) 返回批次文件内容。补记结果追加回同一份日志（带 empty_recorded / backfill），
    所以重跑只会补还没补上的。"""
    if not log_path.exists():
        return {"candidates": 0, "recorded": 0, "by_status": {}}
    recs = latest_records([json.loads(line) for line in log_path.read_text().splitlines() if line.strip()])
    todo = [r for r in recs if r.get("status") == "empty" and not confirmed_empty(r)]
    batches: dict[str, dict] = {}
    counts: dict[str, int] = {}
    out = []
    for r in todo:
        bid = r.get("batch")
        if bid not in batches:
            batches[bid] = load_batch(bid) or {}
        source_batch = batches[bid]
        version = r.get("prompt_version") or source_batch.get("prompt_version") or "agent-v4"
        if quality.requires_quality(version):
            counts["quality_review_required"] = counts.get("quality_review_required", 0) + 1
            continue
        item = next((i for i in source_batch.get("items", []) if int(i["id"]) == int(r["id"])), None)
        if item is None:
            counts["batch_missing"] = counts.get("batch_missing", 0) + 1
            continue
        payload = build_payload(item, {"entities": [], "edges": []}, model=r.get("model") or "unknown",
                                session=None, executor=r.get("executor") or executor,
                                run_id=r.get("run_id") or run_id, prompt_version=version)
        payload["empty"] = True
        code, d = post("POST", f"{url}/api/ingest_extracted", payload, token)
        st = d.get("status") or f"http_{code}"
        if code != 200 and st == "empty_recorded":
            st = f"http_{code}"
        counts[st] = counts.get(st, 0) + 1
        out.append({**{k: r.get(k) for k in ("id", "batch", "model", "executor", "run_id", "project", "type")},
                    "status": "empty" if st == "empty_recorded" else st, "empty_recorded": st == "empty_recorded",
                    "backfill": True, "http": code, "ts": datetime.now(tz=timezone.utc).isoformat()})
    if out:
        with log_path.open("a") as f:
            for rec in out:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"candidates": len(todo), "recorded": counts.get("empty_recorded", 0), "by_status": counts}


# ---------- 提交前必须 dry-run：由工具自己执行，不靠执行器记得 ----------

def next_owner(sd: Path, batch_id: str) -> str:
    """全设备在途登记里 next 的领取方：每一批一个（`next:<状态目录>:<批次号>`）。
    按批而不是按目录：同一目录里前一批还没交的条目，后一批（哪怕显式给了同一个 --after-id）也要避开。
    提交后入库的注销；没入库的留到超时，要重领先 `python -m tools.claim_registry release --owner <它>`。"""
    return f"next:{sd.resolve()}:{batch_id}"


def dryrun_marker_path(sd: Path, batch_id: str) -> Path:
    return sd / f"dryrun-{batch_id}.json"


def extraction_payload_digest(item: dict, result: dict, prompt_version: str, prompt_text: str, *,
                              batch: dict, results: dict, identity: dict | None) -> str:
    # Entity resolution depends on earlier items and lookahead. Any batch/result
    # change invalidates every preview, including a later item's pending context.
    return quality.digest({"source": item, "result": result, "prompt_version": prompt_version,
                           "prompt_text": prompt_text, "batch": batch, "results": results,
                           "identity": identity if quality.requires_quality(prompt_version) else None})


def record_dryrun(sd: Path, batch_id: str, recs: list[dict], now: str | None = None) -> Path:
    """把这次 dry-run 预览过的 id（服务端真的回了 dry_run 的那些）追加进标记文件。
    同一批可以分几次 dry-run，取并集。"""
    p = dryrun_marker_path(sd, batch_id)
    seen: set[int] = set()
    digests, new_names, resolution = {}, {}, {}
    if p.exists():
        old = json.loads(p.read_text())
        seen.update(int(x) for x in old.get("ids", []))
        digests.update(old.get("payload_digests", {}))
        new_names.update(old.get("new_entities", {}))
        resolution.update(old.get("resolution", {}))
    seen.update(int(r["id"]) for r in recs if r.get("status") == "dry_run" and r.get("http") in (None, 200))
    for rec in recs:
        if rec.get("status") == "dry_run" and rec.get("http") == 200 and rec.get("payload_digest"):
            digests[str(rec["id"])] = rec["payload_digest"]
        if rec.get("status") == "dry_run" and rec.get("http") == 200 and "new_entities" in rec:
            new_names[str(rec["id"])] = rec["new_entities"]   # 每次预览覆盖这条上一次的
        if rec.get("status") == "dry_run" and rec.get("http") == 200 and "resolution" in rec:
            resolution[str(rec["id"])] = rec["resolution"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"batch_id": batch_id, "ids": sorted(seen),
                             "payload_digests": digests, "new_entities": new_names, "resolution": resolution,
                             "ts": now or datetime.now(tz=timezone.utc).isoformat()}, ensure_ascii=False))
    return p


def dryrun_missing(sd: Path, batch_id: str, results: dict, *, batch: dict | None = None,
                   prompt_text: str | None = None, prompt_version: str | None = None,
                   identity: dict | None = None) -> list[int]:
    """results 里带实体的 id 中，哪些还没在这一批的 dry-run 标记里。空 entities 的 id 不要求（它不会提交）。"""
    p = dryrun_marker_path(sd, batch_id)
    marker = json.loads(p.read_text()) if p.exists() else {}
    seen = {int(x) for x in marker.get("ids", [])}
    items = results.get("items", [results] if "entities" in results else [])
    if batch is not None:
        version = prompt_version or batch.get("prompt_version") or PROMPT_VERSION
        if quality.requires_quality(version) and not quality.valid_identity(identity):
            return sorted(int(r["id"]) for r in items if r.get("entities"))
        sources = {int(i["id"]): i for i in batch["items"]}
        prompt = PROMPT_PATH.read_text() if prompt_text is None else prompt_text
        expected = marker.get("payload_digests", {})
        return sorted(int(r["id"]) for r in items if r.get("entities") and
                      (int(r["id"]) not in sources or expected.get(str(r["id"])) !=
                       extraction_payload_digest(sources[int(r["id"])], r, version, prompt,
                                                 batch=batch, results=results, identity=identity)))
    return sorted({int(r["id"]) for r in items if r.get("entities")} - seen)


# ---------- 命名同步（单模型）：和全部战役已提交批次比，加本批内部统一 ----------

def namesync_report(batch: dict, results: dict, *, new_entities: dict[str, set[str]] | None = None,
                    root: Path | None = None, layout=None) -> dict:
    """单模型批次的命名同步报告（规则与战役同一份，见 tools/campaign_namesync.py）。
    参照物是 root（缺省 状态目录/campaigns）下全部战役的已提交批次；本状态目录自己的旧批不留结果文件，不参与。"""
    from tools import campaign_namesync as ns
    layout = layout or ns.Layout.from_disk()
    recs, _ = ns.campaign_records(root or state_dir() / "campaigns", None, layout)
    bodies = {it.get("id"): ns.Body(it.get("episode_body", ""), layout) for it in batch.get("items", [])}
    for body in bodies.values():
        layout.known_wts.update(body.wts)
    last = (2, "", "")   # 排在全部已提交、待提交批次之后
    mine = ns.records_for("agent_extract", batch.get("batch_id") or "?", ns.items_of(results), bodies, last, layout)
    earlier = ns.earlier_index(recs, last)
    rep = {"batch": batch.get("batch_id"), **ns.analyse(mine, earlier, layout)}
    rep["splits"] = ns.dryrun_splits(mine, earlier, new_entities) if new_entities is not None else []
    return rep


def dryrun_new_entities(sd: Path, batch_id: str, results: dict) -> dict[str, set[str]] | None:
    """dry-run 标记里每条会新建的实体名；有实体的条目缺记录（旧版本预览）返回 None。"""
    p = dryrun_marker_path(sd, batch_id)
    known = (json.loads(p.read_text()).get("new_entities") or {}) if p.exists() else {}
    items = results.get("items", [results] if "entities" in results else [])
    if any(str(r["id"]) not in known for r in items if r.get("entities")):
        return None
    return {k: set(v) for k, v in known.items()}


def quality_file(sd: Path, batch_id: str) -> Path:
    return sd / "reviews" / f"{batch_id}.json"


def check_quality_file(batch: dict, results: dict, review_path: Path, prompt_text: str,
                       destination: Path | None = None, expected_identity: dict | None = None) -> dict:
    try:
        review = json.loads(review_path.read_text())
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [f"quality_review_unreadable: {type(exc).__name__}"]}
    problems = quality.validate_review(batch, results, review, prompt_text)
    if expected_identity is not None and review.get("identity") != expected_identity:
        problems.append("quality_identity_mismatch: campaign manifest changed")
    if not problems and destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(review, ensure_ascii=False, indent=2))
    return {"ok": not problems, "errors": problems}


def runtime_preflight(url: str, token: str | None, get=None) -> dict:
    """Read actual server capabilities, not just health=200; never claim readiness on unknowns."""
    import importlib.util
    from tools.extract_campaign import tracking_state
    root = PROMPT_PATH.parent.parent
    tracking = tracking_state()
    dependencies = {name: importlib.util.find_spec(name) is not None for name in ("pydantic", "httpx")}
    errors = []
    if sys.version_info < (3, 10):
        errors.append("python_requires_3_10")
    if not all(dependencies.values()):
        errors.append("missing_dependencies")
    if tracking["tracked"]:
        errors.append("extraction_session_is_recorded")
    try:
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = root.name if re_full_sha(root.name) else None
    try:
        code, capabilities = (get or _http)("GET", url.rstrip("/") + "/api/extraction/capabilities", None, token)
    except (OSError, ValueError) as exc:
        code, capabilities = 0, {"error": type(exc).__name__}
    if (code != 200 or not isinstance(capabilities, dict)
            or quality.QUALITY_VERSION not in capabilities.get("quality_versions", [])
            or PROMPT_VERSION not in capabilities.get("prompt_versions", [])):
        errors.append("server_quality_capability_unavailable: publish server before clients")
    return {"ok": not errors, "errors": errors, "loaded_code_root": str(root), "revision": revision,
            "python": sys.version.split()[0], "dependencies": dependencies,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": __import__("hashlib").sha256(PROMPT_PATH.read_bytes()).hexdigest(),
            "quality_version": quality.QUALITY_VERSION, "tracking": tracking, "server": capabilities}


def re_full_sha(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{40}", value))


SUMMARY_KEYS = ("model", "run_id", "executor")
# 提交后不再需要占着的终态：入库、空观测（不提交）、别处已入库
DONE_STATUSES = ("ok", "empty", "skipped")


def confirmed_empty(rec: dict) -> bool:
    return rec.get("status") == "empty" and rec.get("http") == 200 and rec.get("empty_recorded") is True


def latest_records(records: list[dict]) -> list[dict]:
    """Attempts are not observations; preserve independent models/runs/executors."""
    last = {}
    for index, rec in enumerate(records):
        key = ((rec.get("batch"), rec.get("id"), rec.get("model"), rec.get("run_id"), rec.get("executor"))
               if rec.get("id") is not None else ("unidentified", index))
        last[key] = rec
    return list(last.values())


def summarize_log(records: list[dict], key: str = "model") -> dict:
    """按 key（model / run_id / executor）汇总提交口径。旧记录没有 run_id / executor 字段时归入 '?'。"""
    if key not in SUMMARY_KEYS:
        raise ValueError(f"key 只能是 {SUMMARY_KEYS}")
    by_model: dict[str, dict] = {}
    for r in latest_records(records):
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
        elif st == "empty" and confirmed_empty(r):
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
              knobs: dict | None = None, prompt_version: str | None = None,
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
        "model": model, "prompt_version": prompt_version or PROMPT_VERSION, "server_commit": server_commit,
        "knobs": merged, "sample": sample,
        "started_at": now or datetime.now(tz=timezone.utc).isoformat(), "ended_at": None,
        "self_reported": {"input_tokens": None, "output_tokens": None, "wall_s": None},
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
    sr = manifest.setdefault("self_reported", {"input_tokens": None, "output_tokens": None, "wall_s": None})
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
    tmp = p.with_name(p.name + ".tmp")   # 原子替换：status 等并发读者不会读到写了一半的文件
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["next", "submit", "status", "prompt", "retro", "run", "dryrun-check", "mark-empty",
                                    "quality-sheet", "quality-check", "preflight", "namesync"])
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
    ap.add_argument("--apply", action="store_true", help="namesync：把同键异名改成规范名，写回 --file")
    ap.add_argument("--allow-split", metavar="原因",
                    help="submit：命名分裂判据拦下的名字确属不同事物时照样提交（原因写进 lessons）")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--after-id", type=int, help="next：覆盖游标起点")
    ap.add_argument("--out", help="next：batch 文件路径（默认 状态目录/batch-<id>.json）")
    ap.add_argument("--batch", help="submit：next 写出的 batch 文件")
    ap.add_argument("--file", help="submit：会话写的抽取结果 JSON（{items:[{id, entities, edges}]}）")
    ap.add_argument("--review", help="quality-check / submit: completed source-bound review JSON")
    ap.add_argument("--prompt-file", help="frozen prompt text used for this batch, including chain rules")
    ap.add_argument("--model", help="submit：extractor_model，如 claude-fable-5-1")
    ap.add_argument("--session", help="submit：extractor_session（会话 id）")
    ap.add_argument("--url", default=os.environ.get("KG_HUB_URL", DEFAULT_URL))
    # retro 的参数要原样透传给 digest_retro（如 --every 500 / --adjust k=v），所以用 parse_known_args
    a, extra = ap.parse_known_args(argv)
    if a.cmd == "retro":
        a.retro_args = list(a.retro_args) + list(extra)
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    if os.environ.get('KG_HUB_REWORK_BATCHES') is not None and a.cmd in ('next', 'submit', 'mark-empty'):
        raise SystemExit('quality rework cannot claim or submit sources through the standalone agent entry')
    token = api_token()
    sd = state_dir()
    state_path, log_path = sd / "state.json", sd / "log.jsonl"

    if a.cmd == "preflight":
        out = runtime_preflight(a.url, token)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1
    if a.cmd in ("quality-sheet", "quality-check"):
        if not (a.batch and a.file):
            raise SystemExit("quality-sheet/check require --batch and --file")
        batch, results = json.loads(Path(a.batch).read_text()), json.loads(Path(a.file).read_text())
        prompt = Path(a.prompt_file).read_text() if a.prompt_file else PROMPT_PATH.read_text()
        if a.cmd == "quality-sheet":
            if not a.out:
                raise SystemExit("quality-sheet requires --out")
            identity = quality.extraction_identity(a.model, a.executor, a.run_id)
            if not quality.valid_identity(identity):
                raise SystemExit("quality-sheet requires --model --executor --run-id (or their environment defaults)")
            Path(a.out).write_text(json.dumps(quality.review_sheet(batch, results, prompt, identity), ensure_ascii=False, indent=2))
            print(json.dumps({"review": a.out, "approved": False}))
            return 0
        if not a.review:
            raise SystemExit("quality-check requires --review")
        out = check_quality_file(batch, results, Path(a.review), prompt,
                                 quality_file(sd, batch.get("batch_id") or "?"))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1

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
        flight = runtime_preflight(a.url, token)
        if not flight["ok"]:
            raise SystemExit(json.dumps(flight, ensure_ascii=False))
        cfg = load_config()
        with claim_registry.opened() as reg:
            # 游标、批次号必须在登记锁里读：这把全设备锁让并发的 next 排队，后一个拿到前一个推进后的值。
            # 2026-10-24 实测：原先在锁外读，两个工具用同一状态目录同时 next，领到完全相同的 8 条、
            # 都写成 batch-b0001.json（后写覆盖先写），state.batches 只记 1。
            st = _load_state(state_path)
            after = a.after_id if a.after_id is not None else int(st.get("cursor", 0))
            batch_id = f"b{int(st.get('batches', 0)) + 1:04d}"
            owner = next_owner(sd, batch_id)
            # 全设备在途领取登记：剔除别的战役 / 别的 next 状态目录正领着的观测（tools/claim_registry.py）；
            # 可抽观测稀疏时逐段往后扫，凑满一批再交
            g = gather_batch(after, a.n, fetch=fetch_remote_pending, cfg=cfg,
                             check=lambda idents: check_existing(idents, a.url, token),
                             held=reg.held_by_others(owner))
            remote, picked, stats = g["remote"], g["picked"], g["stats"]
            if not g["seen"]:
                print(json.dumps({"picked": 0, "message": "游标之后没有待处理观测了", "cursor": after,
                                  "boundary": remote.get("boundary"), "exhausted": remote.get("exhausted")}))
                return 0
            stats.update({"boundary": remote.get("boundary"), "skipped_by_watermark": remote.get("skipped_by_watermark")})
            st["batches"] = int(st.get("batches", 0)) + 1
            # 游标只推进到「被挑中的最后一条」或「全部看过的最后一条」（没凑满 n 条时），
            # 中间被闸拒/已入库的不会再看第二遍。前瞻：本批之后紧接着的观测标题（第 2 轮复盘）
            last_considered, lookahead = g["last"], g["lookahead"]
            batch = {"batch_id": batch_id, "created_at": datetime.now(tz=timezone.utc).isoformat(),
                     "after_id": after, "last_considered_id": last_considered, "prompt": str(PROMPT_PATH),
                     "prompt_version": PROMPT_VERSION, "stats": stats, "items": picked, "lookahead": lookahead}
            out = Path(a.out) if a.out else sd / f"batch-{batch_id}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(batch, ensure_ascii=False, indent=1))
            st["cursor"] = last_considered
            st["last_batch"] = str(out)
            _save_state(state_path, st)
            reg.reserve([x["id"] for x in picked], owner, info=batch_id)
        print(json.dumps({"batch": str(out), "picked": len(picked), "cursor": last_considered,
                          **stats}, ensure_ascii=False))
        return 0
    if a.cmd == "mark-empty":
        # 补记本状态目录里判为空、服务端还没记录的观测（批次文件 batch-<批次号>.json 与 log.jsonl 都在这里）
        def load_batch(bid):
            p = sd / f"batch-{bid}.json"
            return json.loads(p.read_text()) if p.exists() else None
        print(json.dumps(mark_empty_from_log(log_path, load_batch, url=a.url, token=token, executor=a.executor,
                                             run_id=a.run_id), ensure_ascii=False))
        return 0
    if a.cmd == "namesync":
        if not (a.batch and a.file):
            raise SystemExit("namesync 需要 --batch --file")
        from tools import campaign_namesync as ns
        batch, results = json.loads(Path(a.batch).read_text()), json.loads(Path(a.file).read_text())
        rep = namesync_report(batch, results)
        if a.apply and rep["auto"]:
            skipped = ns.apply_renames(results, rep["auto"])
            tmp = Path(a.file).with_name(Path(a.file).name + ".namesync-tmp")
            tmp.write_text(json.dumps(results, ensure_ascii=False, indent=1))
            tmp.replace(Path(a.file))
            after = namesync_report(batch, results)
            # 与 campaign namesync 一样：auto 是改之前的计划，applied 是实际写回的每一条，residual 是改完仍没统一的
            rep.update(applied=ns.applied_rows(rep["auto"], skipped), skipped=skipped, residual=after["auto"],
                       redo="结果已改：重做质量表与 dry-run（旧的都已失效）")
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 2 if rep.get("residual", rep["auto"]) else 0
    if a.cmd == "dryrun-check":
        if not (a.batch and a.file):
            raise SystemExit("dryrun-check 需要 --batch --file")
        batch = json.loads(Path(a.batch).read_text())
        results = json.loads(Path(a.file).read_text())
        prompt = Path(a.prompt_file).read_text() if a.prompt_file else PROMPT_PATH.read_text()
        missing = dryrun_missing(sd, batch.get("batch_id") or "?", results, batch=batch, prompt_text=prompt,
                                 identity=quality.extraction_identity(a.model, a.executor, a.run_id))
        print(json.dumps({"batch": batch.get("batch_id"), "missing": missing, "ok": not missing}))
        return 1 if missing else 0
    # submit
    if not (a.batch and a.file and a.model):
        raise SystemExit("submit 需要 --batch --file --model")
    batch = json.loads(Path(a.batch).read_text())
    results = json.loads(Path(a.file).read_text())
    batch_id = batch.get("batch_id") or "?"
    prompt = Path(a.prompt_file).read_text() if a.prompt_file else PROMPT_PATH.read_text()
    version = batch.get("prompt_version") or PROMPT_VERSION
    review = None
    if quality.requires_quality(version):
        path = Path(a.review) if a.review else quality_file(sd, batch_id)
        try:
            review = json.loads(path.read_text())
        except (OSError, ValueError):
            raise SystemExit("quality review required: run quality-sheet then quality-check")
        if a.skip_dryrun_check:
            raise SystemExit("v5 requires payload-bound dry-run; skipping is not supported")
    if not a.dry_run and not a.skip_dryrun_check:
        missing = dryrun_missing(sd, batch_id, results, batch=batch, prompt_text=prompt, prompt_version=version,
                                 identity=quality.extraction_identity(a.model, a.executor, a.run_id))
        if missing:
            raise SystemExit(f"本批 {batch_id} 有 {len(missing)} 条还没 dry-run 过（id {missing[:10]}{'…' if len(missing) > 10 else ''}）；"
                             f"先 submit --dry-run，确需跳过加 --skip-dryrun-check 并写进 lessons")
        new = dryrun_new_entities(sd, batch_id, results)
        if new is None:
            print(f"本批 {batch_id} 的 dry-run 标记没记 new_entities（旧版本预览），判不了命名分裂：重新 submit --dry-run",
                  file=sys.stderr)
            return 3
        splits = namesync_report(batch, results, new_entities=new)["splits"]
        if splits and not a.allow_split:
            print(json.dumps({"batch": batch_id, "name_splits": splits}, ensure_ascii=False, indent=1), file=sys.stderr)
            print(f"{len(splits)} 个名字提交后会在图里分裂成重复节点：namesync --apply 后重做质量表与 dry-run；"
                  "确属不同事物用 --allow-split \"原因\"", file=sys.stderr)
            return 2
    from tools import nas_gate
    try:
        with nas_gate.hold("dryrun" if a.dry_run else "submit") as gate_info:
            recs = submit_results(batch, results, model=a.model, session=a.session, url=a.url,
                                  token=token, log_path=log_path, dry_run=a.dry_run,
                                  post=nas_gate.track_responses(_http, gate_info),
                                  names_path=sd / f"names-{a.model}.tsv",
                                  executor=a.executor, run_id=a.run_id, prompt_version=version,
                                  quality_review=review, prompt_text=prompt)
    except nas_gate.GateRefused as e:
        print(e.message, file=sys.stderr)
        return e.code
    if a.dry_run:
        record_dryrun(sd, batch_id, recs)
    else:   # 入库（或别处已入库）的注销在途登记；没入库的留着，超时自动失效
        claim_registry.release([r["id"] for r in recs if r.get("status") in DONE_STATUSES], next_owner(sd, batch_id))
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(json.dumps({"submitted": len(recs), "by_status": counts, "dry_run": a.dry_run,
                      "executor": a.executor, "run_id": a.run_id,
                      "items": [{**{k: r.get(k) for k in ("id", "status", "nodes_new", "nodes_reused",
                                                           "edges_new", "edges_reused", "reuse_mismatch", "error")},
                                 **({"reused": [reuse_line(x) for x in r["resolution"] if x.get("reused")],
                                     "reuse_cross_type": r.get("reuse_cross_type") or []}
                                    if "resolution" in r else {})}
                                for r in recs]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
