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
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLAWD = Path(os.environ.get("CLAWD_DIR", str(Path.home() / "clawd")))
SEARCH_ROOTS = ["notes", "memory", "plans", "reports", "capsules"]
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
    """与 kg-hub ingesters/openclaw_capsule.discover_capsules 同规则。"""
    candidates: list[Path] = []
    for root in SEARCH_ROOTS:
        base = CLAWD / root
        if not base.is_dir():
            continue
        for pattern in ("*.md", "*.md.gz"):
            for p in base.rglob(pattern):
                if p.name.startswith(("CAPSULE-", "capsule-")):
                    candidates.append(p)
    seen: dict[str, Path] = {}   # 同内容多路径去重(首 200 字节)
    for p in sorted(candidates):
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
                out.append(p)
        except Exception as exc:
            log(f"[skip] {p.name}: unreadable ({type(exc).__name__}: {exc})")
    return sorted(out)


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
    for p in files:
        rel = str(p.relative_to(CLAWD))
        sha = hashlib.sha256(capsule_bytes(p)).hexdigest()
        entry = state.get(rel)
        if entry and entry.get("sha") == sha:
            continue
        if sha in known_shas:  # md→gz 原地改名:内容没变,记别名即可
            state[rel] = {"sha": sha, "status": "alias", "at": datetime.now(tz=timezone.utc).isoformat()}
            continue
        todo.append((p, rel, sha))
    log(f"[plan] {len(files)} capsules discovered, {len(todo)} new/changed to push")

    pushed = 0
    for p, rel, sha in todo[:MAX_PER_RUN]:
        body = capsule_bytes(p).decode("utf-8", "ignore")
        ref = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
        code, resp = post_ingest(url, tok, {
            "name": f"openclaw-capsule-{capsule_stem(p.name)}",
            "episode_body": body,
            "source_description": f"openclaw-vps: {rel}",
            "reference_time": ref,
            "source_obs_id": sha,
            "sync": False,
        })
        status = resp.get("status", "")
        if code in (200, 202) and status in ("ok", "skipped", "in_progress", "accepted", "quarantined") or \
           (code == 202 and not status):
            state[rel] = {"sha": sha, "status": status or "accepted",
                          "at": datetime.now(tz=timezone.utc).isoformat()}
            known_shas.add(sha)
            pushed += 1
            save_state(state)  # 每推成一条落一次盘:中途崩溃不丢已推水印
            log(f"[push] {rel} → {code} {status}")
            if status == "quarantined":
                log(f"[gate] {rel} 被格式门拦截 → 反馈待办处理")
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
