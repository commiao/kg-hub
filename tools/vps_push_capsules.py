#!/usr/bin/env python3
"""kg-push-capsules — OpenClaw VPS 直推知识胶囊到 NAS kg-hub。

取代旧链路「Mac 每 30 分钟 ssh+tar 拉全量快照再本地 ingest」:
数据在哪就从哪推(VPS),抽取在图所在地跑(NAS /api/ingest 异步端点)。
Mac 退出 OpenClaw 数据面,只剩它自己的 claude-mem 摄入。

部署位置: oc-vps ~/clawd/scripts/kg-push-capsules.py (源: kg-hub/tools/vps_push_capsules.py)
调度:     admin 的系统 crontab, 每 30 分钟(确定性脚本,不走 OpenClaw LLM cron)
状态:     ~/.kg-hub-push/state.json  (relpath → sha/status;跨 md→gz 改名按 sha 去重)
日志:     ~/.kg-hub-push/push.log    (>5MB 轮转一次)
配置:     KG_HUB_URL / KG_HUB_TOKEN, 取自环境或 ~/.openclaw/env.sh

服务器响应处理:
  ok/skipped/in_progress → 记水印(幂等,下轮不再推)
  quarantined            → 记水印(格式门拦截,去「反馈待办」人工处理)
  409 previous_attempt_failed → 记 failed(不重试轰炸;删 NAS IngestedKey 后手工重推)
  网络/5xx               → 不记水印,下轮自动重试

仅依赖 python3 标准库。
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLAWD = Path(os.environ.get("CLAWD_DIR", str(Path.home() / "clawd")))
SEARCH_ROOTS = ["notes", "memory", "plans", "reports", "capsules"]
# 知识层目录(相对 clawd):这些目录下的所有 .md/.md.gz 都是可复用知识(标准/方法论/
# 手册/方案),收进 NAS 标 openclaw-kb-*。2026-07-24 收拢:OpenClaw 不再自养知识库,
# 知识统一进 NAS;运营/原料层(social-search/reports/investment/archive…)不在此列。
KNOWLEDGE_DIRS = ["notes/standards", "notes/solutions", "notes/sop", "notes/knowledge-base",
                  "notes/technical", "notes/strategy", "notes/areas", "notes/resources",
                  "notes/analysis"]
# 顶层散落的知识文档按文件名关键词识别(guide/rule/standard/advice/sop/规范/指南/标准)
_KB_TOPLEVEL = re.compile(r"guide|rule|standard|advice|sop|规范|指南|标准|原则|manual",
                          re.IGNORECASE)
MIN_SIZE = 1500          # 解压后字节数;更小的是空壳存根
MAX_PER_RUN = 50         # 单轮推送上限(端点异步,纯保险)
STATE_DIR = Path.home() / ".kg-hub-push"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "push.log"
LOCK_FILE = STATE_DIR / ".lock"


def log(msg: str) -> None:
    line = f"{datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            LOG_FILE.rename(LOG_FILE.with_suffix(".log.1"))  # 覆盖旧 .1
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_state(state: dict) -> None:
    """原子落盘(tmp+rename):中途被杀不会留下半截 JSON。"""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    os.replace(tmp, STATE_FILE)


def load_env_config() -> tuple[str, str]:
    url = os.environ.get("KG_HUB_URL", "")
    tok = os.environ.get("KG_HUB_TOKEN", "")
    env_sh = Path.home() / ".openclaw" / "env.sh"
    if (not url or not tok) and env_sh.exists():
        for raw in env_sh.read_text().splitlines():
            raw = raw.strip()
            if raw.startswith("KG_HUB_URL=") and not url:
                url = raw.split("=", 1)[1].strip().strip("'\"")
            elif raw.startswith("KG_HUB_TOKEN=") and not tok:
                tok = raw.split("=", 1)[1].strip().strip("'\"")
    return url.rstrip("/"), tok


def capsule_bytes(path: Path) -> bytes:
    """按 gzip 魔数决定解压(与 kg-hub ingester 同规则):
    OpenClaw 会产出假 .md.gz(纯文本套 gz 名),按原文处理别丢。"""
    raw = path.read_bytes()
    if path.name.endswith(".gz"):
        if raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        log(f"[warn] {path.name}: .gz 命名但非 gzip 载荷,按纯文本处理")
    return raw


def capsule_stem(name: str) -> str:
    for suf in (".md.gz", ".md"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def discover() -> list[Path]:
    """返回 [(path, category)],category ∈ {'capsule','kb'}。
    capsule:SEARCH_ROOTS 下 CAPSULE-/capsule- 前缀(每日提炼胶囊,同 ingester 规则)。
    kb:KNOWLEDGE_DIRS 下全部 .md/.md.gz + 顶层知识文档(标准/方法论/手册,收拢进 NAS)。"""
    cat: dict[Path, str] = {}
    # capsules
    for root in SEARCH_ROOTS:
        base = CLAWD / root
        if not base.is_dir():
            continue
        for pattern in ("*.md", "*.md.gz"):
            for p in base.rglob(pattern):
                if p.name.startswith(("CAPSULE-", "capsule-")):
                    cat.setdefault(p, "capsule")
    # knowledge dirs(整目录收)
    for kd in KNOWLEDGE_DIRS:
        base = CLAWD / kd
        if not base.is_dir():
            continue
        for pattern in ("*.md", "*.md.gz"):
            for p in base.rglob(pattern):
                cat.setdefault(p, "kb")
    # 顶层散落知识文档(按文件名关键词)
    notes = CLAWD / "notes"
    if notes.is_dir():
        for pattern in ("*.md", "*.md.gz"):
            for p in notes.glob(pattern):   # 只顶层,不递归
                if _KB_TOPLEVEL.search(p.name):
                    cat.setdefault(p, "kb")

    seen: dict[str, Path] = {}   # 同内容多路径去重(首 200 字节)
    for p in sorted(cat.keys()):
        try:
            key = capsule_bytes(p)[:200].decode("utf-8", "ignore")
        except Exception as exc:
            log(f"[skip] {p.name}: unreadable ({type(exc).__name__}: {exc})")
            continue
        seen.setdefault(key, p)
    out = []
    for p in seen.values():
        try:
            if len(capsule_bytes(p)) >= MIN_SIZE:
                out.append((p, cat[p]))
        except Exception as exc:
            log(f"[skip] {p.name}: unreadable ({type(exc).__name__}: {exc})")
    return sorted(out, key=lambda t: str(t[0]))


def post_ingest(url: str, tok: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{url}/api/ingest",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {tok}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # URLError/TimeoutError/ConnectionReset 等网络层异常:
        # 返回哨兵值走 retry-next-run,绝不让单次网络抖动崩掉整轮、丢掉水印
        return 0, {"error": f"{type(e).__name__}: {e}"}


def poll_until_done(url: str, tok: str, sd: str, sid: str,
                    max_wait: int = 300, interval: int = 8) -> str:
    """异步 ingest 后轮询该条状态直到 ok/error,或超时。返回终态字符串。
    这样一次只有一篇在抽取(发下一篇前等这篇完成),既串行避免 writer.lock 争用,
    又不长挂 HTTP 连接(区别于 sync=true 的 client 超时)。"""
    import time as _t
    q = urllib.parse.urlencode({"source_description": sd, "source_obs_id": sid})
    waited = 0
    while waited < max_wait:
        _t.sleep(interval)
        waited += interval
        req = urllib.request.Request(
            f"{url}/api/ingest/status?{q}",
            headers={"Authorization": f"Bearer {tok}"} if tok else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                st = json.loads(r.read() or b"{}").get("status", "")
        except Exception:
            continue   # 状态查询抖动,下轮再看
        if st in ("ok", "skipped", "error", "not_found"):
            return st
    return "timeout"


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("[lock] another run in progress; exiting")
        return 0

    url, tok = load_env_config()
    if not url or not tok:
        log("[fatal] KG_HUB_URL/KG_HUB_TOKEN not configured (env or ~/.openclaw/env.sh)")
        return 1

    state: dict[str, dict] = {}
    if not STATE_FILE.exists():
        # 割接保险:历史胶囊只有水印这一道防线(旧链路直写图无 IngestedKey)。
        # 必须先用 Mac 的 data/.ingested.json 接种(sha256→sha 转换),
        # 或确认全新环境后手工 echo '{}' > state.json。
        log("[fatal] state.json 不存在 — 先接种水印再跑,防止历史胶囊全量重复入图")
        return 1
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            # 注意:服务端幂等只保护"经本脚本推过"的条目(有 IngestedKey);
            # 旧链路直写图的历史胶囊全靠本水印挡重——state 损坏时别硬跑。
            log("[fatal] state.json unreadable — 修复或从备份恢复后再跑"
                "(历史胶囊仅靠水印防重,清空重跑会重复入图)")
            return 1
    known_shas = {v.get("sha") for v in state.values()}

    files = discover()
    todo = []
    for p, category in files:
        rel = str(p.relative_to(CLAWD))
        sha = hashlib.sha256(capsule_bytes(p)).hexdigest()
        entry = state.get(rel)
        if entry and entry.get("sha") == sha:
            continue
        if sha in known_shas:  # md→gz 原地改名:内容没变,记别名即可
            state[rel] = {"sha": sha, "status": "alias", "at": datetime.now(tz=timezone.utc).isoformat()}
            continue
        todo.append((p, rel, sha, category))
    ncap = sum(1 for _, c in files if c == "capsule")
    nkb = sum(1 for _, c in files if c == "kb")
    log(f"[plan] {len(files)} discovered (capsule={ncap} kb={nkb}), {len(todo)} new/changed to push")

    pushed = 0
    for p, rel, sha, category in todo[:MAX_PER_RUN]:
        body = capsule_bytes(p).decode("utf-8", "ignore")
        ref = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
        if category == "kb":   # 知识文档:openclaw-kb-<relpath 去扩展名, /→->,便于唯一 & 溯源
            nm = "openclaw-kb-" + re.sub(r"\.(md|md\.gz)$", "", rel).replace("/", "-")
            sd = f"openclaw-vps-kb: {rel}"
        else:
            nm = f"openclaw-capsule-{capsule_stem(p.name)}"
            sd = f"openclaw-vps: {rel}"
        code, resp = post_ingest(url, tok, {
            "name": nm,
            "episode_body": body,
            "source_description": sd,
            "reference_time": ref,
            "source_obs_id": sha,
            "sync": False,   # async 发出 → 下面 poll_until_done 等它抽完再发下一篇
        })
        status = resp.get("status", "")
        # 立即终态(skipped/quarantined):直接记账,不用 poll
        if status in ("skipped", "quarantined"):
            state[rel] = {"sha": sha, "status": status,
                          "at": datetime.now(tz=timezone.utc).isoformat()}
            known_shas.add(sha); pushed += 1; save_state(state)
            log(f"[push] {rel} → {status}")
            if status == "quarantined":
                log(f"[gate] {rel} 被格式门拦截 → 反馈待办处理")
        # 已受理(异步抽取中):轮询到终态,一次只跑一篇,避免 writer.lock 争用
        elif code in (200, 202) and (status in ("accepted", "in_progress", "ok") or not status):
            final = poll_until_done(url, tok, sd, sha)
            if final in ("ok", "skipped"):
                state[rel] = {"sha": sha, "status": final,
                              "at": datetime.now(tz=timezone.utc).isoformat()}
                known_shas.add(sha); pushed += 1; save_state(state)
                log(f"[push] {rel} → {final}")
            else:  # error/timeout:不记账,下轮重试(error 需先清 NAS 键)
                log(f"[retry-next-run] {rel} → 终态={final}")
        elif code == 409:
            state[rel] = {"sha": sha, "status": "failed-409",
                          "at": datetime.now(tz=timezone.utc).isoformat()}
            log(f"[fail] {rel} → 409 previous_attempt_failed(不再重试;"
                f"删 NAS IngestedKey 后把本条从 state.json 移除可重推)")
        else:
            log(f"[retry-next-run] {rel} → HTTP {code} {json.dumps(resp, ensure_ascii=False)[:150]}")

    save_state(state)
    log(f"[done] pushed={pushed} state_entries={len(state)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
