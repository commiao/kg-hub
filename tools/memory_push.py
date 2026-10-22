"""tools/memory_push.py — 把人写的 agent 记忆确定性抽取后经 /api/ingest_extracted 推进 kg-hub。

背景与方案：docs/MEMORY-INGEST-PLAN.md（T-0332）。Phase 1 只做 Claude Code 的记忆
（~/.claude/projects/<桶>/memory/<slug>.md，frontmatter name/description/metadata.type + Why/How to apply）。

原则：
- 真相源留在文件（~/.local/share/agent-memory/claude-code.git 跟踪的工作树），kg-hub 只做索引与召回。
- 不调模型：实体/边由本文件的纯函数机械生成；/api/search 只返回边的 fact，所以 fact 就是
  记忆的 description 原文（人写的一句话结论）、How to apply 首句，以及正文前几段的段首句。
- 幂等键沿用服务端的 (source_description, source_obs_id)，source_obs_id = 文件内容 sha256。
  文件改了 → 推新版（name 带新 sha8）并归档旧版；文件删了 → 归档。不反向回写文件。
- 分档：A 推（type=feedback/reference）、B 搬（type=project，走 T-0302 流程搬到所属文档）、
  C 不推（跳过清单、机密扫描命中、无 type）。

子命令：
  list      列出记忆与分档及原因（不联网）
  dry-run   对 A 档逐条 POST dry_run=true：看实体消解到哪、纪律指标；不写图
  push      串行推送 A 档：跳过已入库、归档被改/被删的旧版；状态写 ~/.kg-hub/agent-memory/
  status    看水位线与日志汇总

环境：KG_HUB_URL（默认 NAS）、KG_HUB_API_TOKEN（同 tools/agent_extract：环境变量 → ~/.claude-mem/.env，
只用于 Authorization 头，不打印、不落日志）、KG_HUB_MEMORY_PUSH_DIR（状态目录）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.agent_extract import _http, api_token, check_existing  # noqa: E402

TOOL = "claude-code"
TOOL_ENTITY = "Claude Code"            # 顶层桶（跨项目）的记忆挂到工具节点上
EXTRACTOR_MODEL = "agent-memory-deterministic"   # 来源标记，不是模型；见 EXTRACTOR_MODEL_PATTERN
# 抽取规则版本。进幂等键：规则改了（fact 写法变）同一文件也要重推并归档旧版。
# v1 → v2（2026-10-19）：主 fact 去掉 "slug：" 前缀。实测英文 slug 在 bge-small-en 下压过中文语义，
# 纯中文 description 当查询时连自己都召不回（alert-thresholds / hard-logic），去前缀后命中。
# v2 → v3（2026-10-22，T-0351）：每条记忆再补正文段首句（最多 MAX_LEADS 条）。离线实测（MiniLM 语义腿、
# 全图 18 万条边）：记忆集中文问法 nl 3/11 → 6/11，原黄金集、中文问英文、英文对照 top-10 零新丢；
# Why 首句、只补正文首句、补前 2–5 条段首句都到不了 6/11。
PROMPT_VERSION = "memory-v3"
DEFAULT_URL = "http://100.123.208.32:17171"
DEFAULT_ROOT = "~/.claude/projects"
DEFAULT_GIT_DIR = "~/.local/share/agent-memory/claude-code.git"
MAX_FACT = 1000
MAX_SUMMARY = 2000
MAX_LEADS = 6

KIND_BY_TYPE = {"feedback": "方法论", "reference": "手册", "project": "项目事实"}
TIER_BY_TYPE = {"feedback": "A", "reference": "A", "project": "B"}
BUCKET_PREFIXES = ("-Users-mac-work-ai-", "-Users-mac-workspace-")
GENERAL_BUCKETS = {"-Users-mac-work-ai", "-Users-mac-workspace"}
# C 档固定清单：已是 canonical 文档影子 / 纯工作日志，推了只会重复。
DEFAULT_SKIP = {"kg-hub-northstar", "kg-hub-cognitive-asset-plan", "kg-hub-capsule-ranking"}

# 机密扫描：只认「键 + 值」形态；光提到 password/token 这些词不算（那是在讲概念）。
SECRET_PATTERNS = (
    # 不加前导 \b：KG_HUB_API_TOKEN= 这种键名里 TOKEN 前面是下划线，词边界不成立
    re.compile(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{8,}"),
    re.compile(r"\b(sk|xox[abp]|ghp|gho|AKIA)[-_][A-Za-z0-9]{10,}"),
    re.compile(r"https?://\S*(hook|webhook)\S*/[A-Za-z0-9_\-]{20,}"),
)
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
_HOWTO_RE = re.compile(r"^\s*(?:\*\*|##\s*)?How to apply\s*[:：]?\s*(?:\*\*)?\s*[:：]?\s*(.*)$", re.I)
_LIST_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+")
# 段首句不取 Why / How to apply 段：How 首句已单独成边；Why 首句离线实测对召回无增益。
_WHY_HOW_RE = re.compile(r"^(?:Why\s*[:：]|How to apply)", re.I)


# ---------- 纯函数：解析 ----------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (frontmatter dict, 正文)。没有 frontmatter → ({}, 全文)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        return {}, text
    head = parts[0][3:]
    body = parts[1] if len(parts) == 2 else parts[1] + ("\n---" + parts[2] if parts[2] else "")
    # 正文从第二个 --- 之后开始（上面 split 的第二段起始是 --- 后的换行）
    body = text[len(parts[0]) + 4:]
    try:
        import yaml  # noqa: WPS433 — venv 里有；没有时退回极简解析
        fm = yaml.safe_load(head) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:  # noqa: BLE001
        fm = {}
        for line in head.splitlines():
            m = re.match(r"^\s*([A-Za-z_]+):\s*(.*)$", line)
            if m and not line.startswith(" "):
                fm[m.group(1)] = m.group(2).strip().strip('"')
            elif m and line.startswith(" "):
                fm.setdefault("metadata", {})
                if isinstance(fm["metadata"], dict):
                    fm["metadata"][m.group(1)] = m.group(2).strip()
    return fm, body.lstrip("\n")


def first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    m = re.search(r"[。；;]|\.\s", text)
    return text[:m.end()].rstrip() if m else text


def how_to_first_sentence(body: str) -> str:
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        m = _HOWTO_RE.match(line)
        if not m:
            continue
        rest = m.group(1).strip().strip("*").strip()
        if not rest:
            rest = next((ln.strip() for ln in lines[i + 1:] if ln.strip()), "")
        return first_sentence(rest.lstrip("-* ").strip())
    return ""


def plain(text: str) -> str:
    """去掉 markdown 标记（[[链接]]、[文字](url)、**、`），压成一行。"""
    text = re.sub(r"\[\[([^\]|#]+)[^\]]*\]\]", r"\1", text or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return " ".join(text.replace("**", "").replace("`", "").split())


def paragraphs(body: str) -> list[str]:
    """正文段落：按空行切，列表项各算一段；跳过代码块、表格、标题、分隔线。"""
    out: list[str] = []
    cur: list[str] = []
    fence = False

    def flush():
        if cur:
            out.append(plain(" ".join(cur)))
            cur.clear()

    for line in (body or "").splitlines():
        t = line.strip()
        if t.startswith("```"):
            fence = not fence
            flush()
            continue
        if fence or not t or t.startswith(("|", "#")) or re.fullmatch(r"-{3,}", t):
            flush()
            continue
        if _LIST_RE.match(t):
            flush()
            t = _LIST_RE.sub("", t)
        cur.append(t)
    flush()
    return [p for p in out if p]


def paragraph_leads(body: str, limit: int = MAX_LEADS) -> list[str]:
    """前 limit 段（不含 Why / How to apply 段）各自的首句。去重留给 build_extraction。"""
    paras = [p for p in paragraphs(body) if not _WHY_HOW_RE.match(p)]
    return [plain(first_sentence(p)) for p in paras[:limit]]


def extract_links(body: str) -> list[str]:
    seen: list[str] = []
    for l in _LINK_RE.findall(body or ""):
        l = l.strip()
        if l and l not in seen:
            seen.append(l)
    return seen


def scan_secrets(text: str) -> list[str]:
    hits = []
    for pat in SECRET_PATTERNS:
        for m in pat.finditer(text or ""):
            hits.append(m.group(0)[:24] + "…")   # 只记前缀，足够定位，不把值带出去
    return hits


def parse_memory(text: str, mtime_iso: str) -> dict:
    fm, body = parse_frontmatter(text)
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    typ = str(meta.get("type") or fm.get("type") or "").strip()
    modified = meta.get("modified") or fm.get("modified")
    ref = _iso(modified) or mtime_iso
    return {
        "name": str(fm.get("name") or "").strip(),
        "description": " ".join(str(fm.get("description") or "").split()),
        "type": typ,
        "durability": meta.get("durability") or fm.get("durability"),
        "reference_time": ref,
        "body": body,
        "raw": text,
        "links": extract_links(body),
        "how_to": how_to_first_sentence(body),
        "leads": paragraph_leads(body),
        "secret_hits": scan_secrets(text),
    }


def _iso(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s + "T00:00:00+00:00"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.isoformat()
    except ValueError:
        return None


# ---------- 纯函数：分档与身份 ----------

def project_from_bucket(bucket: str) -> str | None:
    """记忆桶目录名 → 项目名；顶层桶（跨项目）→ None。"""
    if bucket in GENERAL_BUCKETS:
        return None
    for p in BUCKET_PREFIXES:
        if bucket.startswith(p):
            return bucket[len(p):] or None
    return None


def classify_tier(mem: dict, skip: set[str] | None = None) -> tuple[str, str]:
    skip = DEFAULT_SKIP if skip is None else skip
    if not mem.get("name") or not mem.get("description"):
        return "C", "no-frontmatter"
    if mem["name"] in skip:
        return "C", "skip-list"
    if mem.get("secret_hits"):
        return "C", "secret-scan"
    tier = TIER_BY_TYPE.get(mem.get("type") or "")
    if tier is None:
        return "C", f"type={mem.get('type') or '-'}"
    return tier, f"type={mem['type']}"


def content_key(sha: str) -> str:
    """幂等键的 source_obs_id：文件 sha256 + 规则版本。两者任一变 → 新 episode。"""
    return f"{sha}:{PROMPT_VERSION}"


def build_identity(mem: dict, bucket: str, sha: str) -> tuple[str, str, str]:
    """返回 (episode name, source_description, source_obs_id)。name 的 @后缀取自键的 sha8，
    所以文件改了或规则版本变了，name 都不同——归档旧版按 name 才不会误伤新版。"""
    project = project_from_bucket(bucket) or "general"
    key = content_key(sha)
    key8 = hashlib.sha256(key.encode()).hexdigest()[:8]
    name = f"agent-memory-{TOOL}-{mem['name']}@{key8}"
    sd = f"agent-memory/{TOOL} project={project} type={mem['type']} sha={sha[:8]} v={PROMPT_VERSION}"
    return name, sd, key


def _fact(text: str) -> str:
    return " ".join(text.split())[:MAX_FACT]


def build_extraction(mem: dict, project: str | None, index: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """实体与边。index: 所有记忆 slug → description，用来解析 [[链接]]（不在索引里的链接丢弃）。"""
    slug = mem["name"]
    desc = mem["description"]
    ents = [{"name": slug, "type": "Lesson", "summary": desc[:MAX_SUMMARY]}]
    if project:
        target, primary = {"name": project, "type": "Project", "summary": ""}, "belongs_to"
    else:
        target, primary = {"name": TOOL_ENTITY, "type": "Tool", "summary": ""}, "derived_from"
    ents.append(target)
    # 主 fact 不带 slug 前缀：description / How-to 本身就是独立成立的一句话；
    # 英文 slug 开头会让中文查询的向量对不上（见 PROMPT_VERSION 注释）。
    edges = [{"source": slug, "target": target["name"], "name": primary, "fact": _fact(desc)}]
    if mem.get("how_to"):
        edges.append({"source": slug, "target": target["name"], "name": "relates_to",
                      "fact": _fact(mem["how_to"])})
    for l in mem.get("links") or []:
        if l == slug or l not in index:
            continue
        ents.append({"name": l, "type": "Lesson", "summary": index[l][:MAX_SUMMARY]})
        edges.append({"source": slug, "target": l, "name": "relates_to",
                      "fact": _fact(f"{slug} 关联 {l}：{index[l]}")})
    # 段首句：中文问法常问到正文里的现象/机制，而不是 description 那句结论（memory-v3）
    seen = {_fact(desc), _fact(mem.get("how_to") or "")}
    for lead in mem.get("leads") or []:
        f = _fact(lead)
        if f and f not in seen:
            seen.add(f)
            edges.append({"source": slug, "target": target["name"], "name": "relates_to", "fact": f})
    return ents, edges


def build_payload(mem: dict, bucket: str, sha: str, index: dict[str, str], dry_run: bool = False) -> dict:
    name, sd, sid = build_identity(mem, bucket, sha)
    ents, edges = build_extraction(mem, project_from_bucket(bucket), index)
    p = {
        "name": name, "episode_body": mem["raw"], "source_description": sd,
        "reference_time": mem["reference_time"], "source_obs_id": sid,
        "extractor_model": EXTRACTOR_MODEL, "prompt_version": PROMPT_VERSION,
        "origin_tool": TOOL, "durability": mem.get("durability") or "evergreen",
        "entities": ents, "edges": edges, "dry_run": dry_run,
    }
    kind = KIND_BY_TYPE.get(mem.get("type") or "")
    if kind:
        p["kind"] = kind
    return p


def _wm_key(rec: dict) -> str | None:
    # v1 水位线只记了 sha（规则版本 memory-v1）；补成 content_key 口径后与 v2 不等 → 判为 changed
    return rec.get("key") or (f"{rec['sha']}:memory-v1" if rec.get("sha") else None)


def plan_changes(watermark: dict, current: dict[str, str]) -> dict[str, list[str]]:
    """watermark: slug → {key|sha, episode_name, ...}；current: slug → content_key。"""
    new = sorted(s for s in current if s not in watermark)
    changed = sorted(s for s in current if s in watermark and _wm_key(watermark[s]) != current[s])
    unchanged = sorted(s for s in current if s in watermark and _wm_key(watermark[s]) == current[s])
    deleted = sorted(s for s in watermark if s not in current)
    return {"new": new, "changed": changed, "unchanged": unchanged, "deleted": deleted}


# ---------- IO ----------

def state_dir() -> Path:
    return Path(os.environ.get("KG_HUB_MEMORY_PUSH_DIR", "~/.kg-hub/agent-memory")).expanduser()


def enumerate_files(root: Path, git_dir: Path | None) -> list[Path]:
    """优先按 git 跟踪列表（只 ls-files，不 checkout）；没有仓库时退回 glob。"""
    rels: list[str] = []
    if git_dir and git_dir.exists():
        try:
            out = subprocess.run(["git", f"--git-dir={git_dir}", "ls-files", "-z"],
                                 capture_output=True, check=True).stdout.decode("utf-8", "replace")
            rels = [r for r in out.split("\0") if r]
        except (subprocess.CalledProcessError, OSError):
            rels = []
    if not rels:
        rels = [str(p.relative_to(root)) for p in root.glob("*/memory/*.md")]
    files = []
    for r in rels:
        p = root / r
        if p.suffix == ".md" and p.parent.name == "memory" and p.name != "MEMORY.md" and p.exists():
            files.append(p)
    return sorted(files)


def load_all(root: Path, git_dir: Path | None) -> list[dict]:
    items = []
    for p in enumerate_files(root, git_dir):
        raw = p.read_bytes()
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
        mem = parse_memory(raw.decode("utf-8", "replace"), mtime)
        if not mem["name"]:
            mem["name"] = p.stem
        items.append({"path": p, "bucket": p.parent.parent.name, "sha": hashlib.sha256(raw).hexdigest(),
                      "bytes": len(raw), "mem": mem})
    return items


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(p)


def _log(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(tz=timezone.utc).isoformat(), **rec}, ensure_ascii=False) + "\n")


def post_with_retry(url: str, body: dict, token: str | None, post=_http, tries: int = 5, sleep=time.sleep):
    """排空期（503 draining）按 retry_after_seconds 等；其他码直接返回。"""
    for i in range(tries):
        code, d = post("POST", url, body, token)
        if code == 503 and isinstance(d, dict) and d.get("code") == "draining" and i < tries - 1:
            sleep(int(d.get("retry_after_seconds") or 5))
            continue
        return code, d
    return code, d


def archive_episode(base: str, name: str, token: str | None, post=_http) -> bool:
    code, d = post("POST", f"{base}/dashboard/archive_episode", {"name": name}, token)
    return code == 200 and bool(isinstance(d, dict) and d.get("ok"))


# ---------- 子命令 ----------

def cmd_list(items: list[dict], skip: set[str]) -> int:
    counts: dict[str, int] = {}
    print(f"{'档':2} {'原因':14} {'type':9} {'project':14} {'bytes':>6}  slug")
    for it in items:
        tier, why = classify_tier(it["mem"], skip)
        counts[tier] = counts.get(tier, 0) + 1
        proj = project_from_bucket(it["bucket"]) or "general"
        print(f"{tier:2} {why:14} {it['mem']['type'] or '-':9} {proj:14} {it['bytes']:6}  {it['mem']['name']}")
    print("\n分档：" + " · ".join(f"{k}={v}" for k, v in sorted(counts.items())) + f" · 共 {len(items)}")
    return 0


def _select(items: list[dict], skip: set[str], only: list[str] | None, limit: int | None) -> list[dict]:
    sel = [it for it in items if classify_tier(it["mem"], skip)[0] == "A"]
    if only:
        sel = [it for it in sel if it["mem"]["name"] in set(only)]
    if limit:
        sel = sel[:limit]
    return sel


def cmd_dry_run(items: list[dict], sel: list[dict], base: str, token: str | None, sleep=time.sleep) -> int:
    index = {it["mem"]["name"]: it["mem"]["description"] for it in items}
    bad = 0
    for it in sel:
        p = build_payload(it["mem"], it["bucket"], it["sha"], index, dry_run=True)
        code, d = post_with_retry(f"{base}/api/ingest_extracted", p, token, sleep=sleep)
        if code != 200:
            bad += 1
            print(f"  ✗ {it['mem']['name']}: http {code} {d.get('code') if isinstance(d, dict) else d}")
            continue
        st = d.get("stats", {})
        reused = [f"{k}→{v['matched_name']}" for k, v in (d.get("resolved") or {}).items() if v.get("reused")]
        flags = {k: v for k, v in st.items() if k in ("unknown_type", "offschema_edge", "dangling_edge",
                                                       "dup_entity", "dup_edge", "dirty_name", "self_edge") and v}
        print(f"  · {it['mem']['name']}: 实体 {st.get('entities_out')} 边 {st.get('edges_out')} "
              f"新 {d.get('nodes_new')} 复用 {d.get('nodes_reused')}"
              + (f" 复用到 {reused}" if reused else "") + (f" ⚠ {flags}" if flags else ""))
        sleep(0.3)
    print(f"\ndry-run {len(sel)} 条，失败 {bad}")
    return 1 if bad else 0


def cmd_push(items: list[dict], sel: list[dict], base: str, token: str | None, sleep=time.sleep,
             post=_http) -> int:
    sd_ = state_dir()
    wm_path, log_path = sd_ / "watermark.json", sd_ / "log.jsonl"
    wm: dict = _load_json(wm_path, {})
    index = {it["mem"]["name"]: it["mem"]["description"] for it in items}
    current = {it["mem"]["name"]: content_key(it["sha"]) for it in sel}
    plan = plan_changes(wm, current)
    print(f"计划：新 {len(plan['new'])} · 改 {len(plan['changed'])} · 不变 {len(plan['unchanged'])} · 删 {len(plan['deleted'])}")

    by_slug = {it["mem"]["name"]: it for it in sel}
    todo = [by_slug[s] for s in plan["new"] + plan["changed"]]
    if todo:
        existing = check_existing(
            [{"source_description": build_identity(it["mem"], it["bucket"], it["sha"])[1],
              "source_obs_id": content_key(it["sha"])} for it in todo], base, token, post=post)
    else:
        existing = {}
    ok = skipped = failed = archived = 0
    for it in todo:
        slug = it["mem"]["name"]
        p = build_payload(it["mem"], it["bucket"], it["sha"], index)
        prev = wm.get(slug)
        if existing.get(p["source_obs_id"]) == "ok":
            code, d = 200, {"status": "skipped", "reason": "already-ok"}
        else:
            code, d = post_with_retry(f"{base}/api/ingest_extracted", p, token, post=post, sleep=sleep)
        rec = {"slug": slug, "episode_name": p["name"], "key": p["source_obs_id"], "http": code,
               "status": d.get("status") if isinstance(d, dict) else None,
               "episode_uuid": d.get("episode_uuid") if isinstance(d, dict) else None,
               "stats": d.get("stats") if isinstance(d, dict) else None}
        _log(log_path, rec)
        if code == 200 and rec["status"] in ("ok", "skipped"):
            ok += rec["status"] == "ok"
            skipped += rec["status"] == "skipped"
            wm[slug] = {"key": p["source_obs_id"], "sha": it["sha"], "version": PROMPT_VERSION,
                        "episode_name": p["name"], "episode_uuid": rec["episode_uuid"],
                        "pushed_at": datetime.now(tz=timezone.utc).isoformat()}
            if prev and prev.get("episode_name") and prev["episode_name"] != p["name"]:
                if archive_episode(base, prev["episode_name"], token, post=post):
                    archived += 1
                    _log(log_path, {"slug": slug, "archived": prev["episode_name"]})
                else:
                    print(f"  ⚠ 归档旧版失败：{prev['episode_name']}")
            print(f"  ✓ {slug} {rec['status']}")
        else:
            failed += 1
            print(f"  ✗ {slug}: http {code} {d.get('code') if isinstance(d, dict) else d}")
        _save_json(wm_path, wm)
        sleep(0.5)
    for slug in plan["deleted"]:
        prev = wm.get(slug) or {}
        if prev.get("episode_name") and archive_episode(base, prev["episode_name"], token, post=post):
            archived += 1
            _log(log_path, {"slug": slug, "archived": prev["episode_name"], "reason": "file-deleted"})
            wm.pop(slug, None)
            print(f"  ⌫ {slug} 已删，归档 {prev['episode_name']}")
        else:
            print(f"  ⚠ {slug} 已删但归档失败")
    _save_json(wm_path, wm)
    print(f"\n推送 {ok} · 跳过 {skipped} · 失败 {failed} · 归档 {archived} · 水位线 {wm_path}")
    return 1 if failed else 0


def cmd_status() -> int:
    sd_ = state_dir()
    wm = _load_json(sd_ / "watermark.json", {})
    recs = []
    try:
        recs = [json.loads(l) for l in (sd_ / "log.jsonl").read_text().splitlines() if l.strip()]
    except OSError:
        pass
    print(f"水位线 {len(wm)} 条 · 日志 {len(recs)} 行 · 目录 {sd_}")
    by = {}
    for r in recs:
        k = r.get("status") or ("archived" if r.get("archived") else "?")
        by[k] = by.get(k, 0) + 1
    print("  " + " · ".join(f"{k}={v}" for k, v in sorted(by.items())))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "dry-run", "push", "status"])
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--git-dir", default=DEFAULT_GIT_DIR)
    ap.add_argument("--url", default=os.environ.get("KG_HUB_URL", DEFAULT_URL))
    ap.add_argument("--only", action="append", help="只处理这些 slug（可重复）")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--skip-file", help="每行一个 slug，追加到 C 档跳过清单")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        return cmd_status()
    skip = set(DEFAULT_SKIP)
    if args.skip_file:
        skip |= {l.strip() for l in Path(args.skip_file).read_text().splitlines() if l.strip() and not l.startswith("#")}
    root = Path(args.root).expanduser()
    git_dir = Path(args.git_dir).expanduser() if args.git_dir else None
    items = load_all(root, git_dir)
    if args.cmd == "list":
        return cmd_list(items, skip)
    sel = _select(items, skip, args.only, args.limit)
    token = api_token()
    if not token:
        print("缺 KG_HUB_API_TOKEN（环境变量或 ~/.claude-mem/.env）", file=sys.stderr)
        return 2
    base = args.url.rstrip("/")
    if args.cmd == "dry-run":
        return cmd_dry_run(items, sel, base, token)
    return cmd_push(items, sel, base, token)


if __name__ == "__main__":
    raise SystemExit(main())
