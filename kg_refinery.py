"""kg-refinery — 统一知识摄入/提炼层,Phase A:Level-1 claude-mem 消费者。

REFINERY-DESIGN Phase A:复活休眠的 claude-mem 线。db 副本早已每 15min 同步到
NAS(sync_claude_mem_to_nas.sh),此前无消费者(4555 条积压)。本进程:

    NAS db 副本(ro) ──90s 微批──▶ ingest_filter 质量闸(复用)
        ──▶ POST /api/ingest(唯一治理写入通道:幂等键/备份/kind链/预拆分流)
             逐条 poll-drain 串行(尊重单写者,模式同 vps_push_capsules)

与退役的 ingesters/claude_mem_obs.py 的关系:查询/正文拼装/过滤调用逐字段镜像
(语义等价),唯一区别是写入从「直连 FalkorDB add_episode(无治理)」改为
「HTTP /api/ingest(全治理)」。旧水印(526 ingested + 2471 rejected)首轮自动迁移。

新旧数据分流:
  live    — id > boundary_id(首轮启动时的 db 最大 id):每轮全量处理,分钟级
  backlog — id ≤ boundary_id 的历史积压:仅在夜间窗口(23:00-07:00 Asia/Shanghai)
            每轮限量烧,不与白天真实使用抢 LLM 串行额度

状态外露:/state/status.json(server 挂同卷 ro,门户「精炼层」卡读它)。

Env(compose):
  KG_HUB_URL(默认 http://kg_hub_server:8080)  KG_HUB_API_TOKEN
  KG_HUB_REFINERY_DB(默认 /data/claude-mem/claude-mem.db)
  KG_HUB_REFINERY_STATE(默认 /state)
  KG_HUB_REFINERY_INTERVAL_SEC(默认 90) KG_HUB_REFINERY_BACKLOG_PER_CYCLE(默认 15)
  KG_HUB_REFINERY_BACKLOG(默认 1;0=只处理 live,不烧积压)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.ingest_filter import (  # noqa: E402
    QuotaTracker, evaluate, load_config, log_decision,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refinery] %(message)s")
log = logging.getLogger("refinery")

KG_HUB_URL = os.environ.get("KG_HUB_URL", "http://kg_hub_server:8080").rstrip("/")
TOKEN = os.environ.get("KG_HUB_API_TOKEN", "")
DB_PATH = Path(os.environ.get("KG_HUB_REFINERY_DB", "/data/claude-mem/claude-mem.db"))
STATE_DIR = Path(os.environ.get("KG_HUB_REFINERY_STATE", "/state"))
INTERVAL = int(os.environ.get("KG_HUB_REFINERY_INTERVAL_SEC", "90"))
BACKLOG_PER_CYCLE = int(os.environ.get("KG_HUB_REFINERY_BACKLOG_PER_CYCLE", "15"))
BACKLOG_ENABLED = os.environ.get("KG_HUB_REFINERY_BACKLOG", "1").lower() in ("1", "true", "yes")
# 旧直连线水印(526 ingested + 2471 rejected)。repo 的 data/ 被 .dockerignore 排除,
# 容器里拿不到 → 部署时必须把该 json 预置到 refinery-state 卷(见 REFINERY-DESIGN
# 部署步骤);这里两个位置都找:先 STATE 卷(生产),再 repo(Mac 本地调试)。
_LEGACY_CANDIDATES = (
    STATE_DIR / "legacy.ingested.claude_mem.json",
    Path(__file__).resolve().parent / "data" / ".ingested.claude_mem.json",
)

WATERMARK = STATE_DIR / "watermark.json"
STATUS = STATE_DIR / "status.json"
DECISIONS_LOG = STATE_DIR / "ingest_decisions.jsonl"
CST = timezone(timedelta(hours=8))  # Asia/Shanghai,夜间窗口按此判
# 温度门控(2026-08 过热事件:空闲盘温 58/59°C,DSM 强制关机线 ~61°C,余量仅
# 2-3°C——持续写盘曾连续两周把 NAS 压关机)。群晖盘温免 sudo 直读
# /run/synostorage/disks/sata*/temperature,compose 把该目录挂到 /disktemp(ro)。
DISKTEMP_DIR = Path(os.environ.get("KG_HUB_DISKTEMP_DIR", "/disktemp"))
MAX_DISK_TEMP = int(os.environ.get("KG_HUB_REFINERY_MAX_DISK_TEMP", "52"))


def max_disk_temp() -> int | None:
    """全部盘温取最大。读不到(非群晖/未挂载)返回 None = 不拦,但会记进 status。"""
    temps = []
    try:
        for f in DISKTEMP_DIR.glob("*/temperature"):
            try:
                temps.append(int(f.read_text().strip()))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return max(temps) if temps else None


# ---------- 水印 ----------

def load_watermark() -> dict:
    if WATERMARK.exists():
        wm = json.loads(WATERMARK.read_text())
        wm["ingested"] = set(wm.get("ingested", []))
        wm["rejected"] = set(wm.get("rejected", []))
        wm["failed"] = set(wm.get("failed", []))
        wm.setdefault("live_cursor", None)
        return wm
    # 首轮:迁移旧直连线的水印(防止 526 条已入图的重复入图——旧线直连 add_episode
    # **没有** IngestedKey,服务端幂等键兜不住这批,水印是唯一防线!)
    wm = {"ingested": set(), "rejected": set(), "failed": set(),
          "boundary_id": None, "live_cursor": None}
    for cand in _LEGACY_CANDIDATES:
        if cand.exists():
            try:
                legacy = json.loads(cand.read_text())
                wm["ingested"] = set(legacy.get("ingested_obs_ids", []))
                wm["rejected"] = set(legacy.get("rejected_obs_ids", []))
                log.info("[watermark] 迁移旧水印(%s): ingested=%d rejected=%d",
                         cand, len(wm["ingested"]), len(wm["rejected"]))
                return wm
            except Exception:  # noqa: BLE001
                log.exception("[watermark] 旧水印 %s 解析失败,试下一候选", cand)
    log.error("[watermark] ⚠⚠ 未找到旧水印(%s)——旧线已入图的 ~526 条将被重复入图!"
              "部署时须把 data/.ingested.claude_mem.json 预置为 STATE 卷的 "
              "legacy.ingested.claude_mem.json", _LEGACY_CANDIDATES[0])
    return wm


def save_watermark(wm: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = {**wm, "ingested": sorted(wm["ingested"]), "rejected": sorted(wm["rejected"]),
           "failed": sorted(wm["failed"])}
    tmp = WATERMARK.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(WATERMARK)


# ---------- 读 db(镜像 ingesters/claude_mem_obs.fetch_observations) ----------

def fetch_rows(min_id_exclusive: int | None = None, max_id_inclusive: int | None = None,
               limit: int = 500) -> list[dict]:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"claude-mem db not found at {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    where, params = [], []
    if min_id_exclusive is not None:
        where.append("o.id > ?"); params.append(min_id_exclusive)
    if max_id_inclusive is not None:
        where.append("o.id <= ?"); params.append(max_id_inclusive)
    sql = (
        "SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, "
        "o.concepts, o.files_read, o.files_modified, o.created_at, o.content_hash, "
        "o.generated_by_model, o.relevance_count, s.platform_source "
        "FROM observations o "
        "LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
        + ("WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY o.id ASC LIMIT {int(limit)}"
    )
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def max_db_id() -> int:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    v = conn.execute("SELECT coalesce(max(id), 0) FROM observations").fetchone()[0]
    conn.close()
    return int(v)


def fetch_ids(min_id_exclusive: int | None = None,
              max_id_inclusive: int | None = None) -> list[int]:
    """只取 id 列(积压/游标计算用,避免每 90s 全列拉 1.3 万行大字段)。"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    where, params = [], []
    if min_id_exclusive is not None:
        where.append("id > ?"); params.append(min_id_exclusive)
    if max_id_inclusive is not None:
        where.append("id <= ?"); params.append(max_id_inclusive)
    sql = ("SELECT id FROM observations "
           + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY id ASC")
    ids = [int(r[0]) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return ids


def fetch_rows_by_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    ph = ",".join("?" * len(ids))
    sql = (
        "SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, "
        "o.concepts, o.files_read, o.files_modified, o.created_at, o.content_hash, "
        "o.generated_by_model, o.relevance_count, s.platform_source "
        "FROM observations o "
        "LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
        f"WHERE o.id IN ({ph}) ORDER BY o.id ASC"
    )
    rows = [dict(r) for r in conn.execute(sql, ids).fetchall()]
    conn.close()
    return rows


# ---------- 正文/payload(镜像 claude_mem_obs.build_episode_body / ingest_one) ----------

def _jlist(field) -> list[str]:
    if not field:
        return []
    try:
        v = json.loads(field)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def build_episode_body(obs: dict) -> str:
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


def to_payload(obs: dict) -> dict:
    ref = obs.get("created_at") or datetime.now(tz=timezone.utc).isoformat()
    return {
        "name": f"claude-mem-obs-{obs['id']}",
        "episode_body": build_episode_body(obs),
        # sd 与旧线逐字段一致(type=/project= 供 origin 正则),尾加 platform=
        "source_description": (
            f"claude-mem obs id={obs['id']} project={obs.get('project', '?')} "
            f"type={obs.get('type', '?')} platform={obs.get('platform_source') or '_default'}"
        ),
        "source_obs_id": obs.get("content_hash") or f"claude-mem-id-{obs['id']}",
        "reference_time": ref,
        "sync": False,
    }


# ---------- HTTP(镜像 vps_push_capsules 的 post + poll-drain 纪律) ----------

def _http(method: str, url: str, body: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001 — 网络层异常:哨兵,retry-next-cycle
        return 0, {"error": f"{type(e).__name__}: {e}"}


async def poll_until_done(sd: str, sid: str, max_wait: int = 600) -> str:
    import urllib.parse
    q = urllib.parse.urlencode({"source_description": sd, "source_obs_id": sid})
    waited = 0
    while waited < max_wait:
        code, d = _http("GET", f"{KG_HUB_URL}/api/ingest/status?{q}")
        st = d.get("status", "")
        if st in ("ok", "skipped", "error"):
            return st
        await asyncio.sleep(8)
        waited += 8
    return "timeout"


async def ingest_via_api(obs: dict) -> str:
    """返回终态: ok|skipped|error|timeout|net|409"""
    p = to_payload(obs)
    code, d = _http("POST", f"{KG_HUB_URL}/api/ingest", p, timeout=60)
    if code == 0:
        return "net"
    if code == 409:
        return "409"
    st = d.get("status", "")
    if st in ("ok", "skipped"):
        return st
    if code == 202 or st in ("accepted", "in_progress"):
        return await poll_until_done(p["source_description"], p["source_obs_id"])
    return "error"


# ---------- 状态外露 ----------

def write_status(**kw) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cur = json.loads(STATUS.read_text()) if STATUS.exists() else {}
        cur.update(kw, ts=datetime.now(tz=timezone.utc).isoformat())
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False))
        tmp.replace(STATUS)
    except Exception:  # noqa: BLE001
        log.exception("[status] write failed (non-fatal)")


def in_backlog_window() -> bool:
    h = datetime.now(tz=CST).hour
    return h >= 23 or h < 7


# ---------- 主循环 ----------

async def process_batch(rows: list[dict], wm: dict, cfg: dict,
                        quotas: QuotaTracker, decided: dict, kind: str) -> dict:
    """decided: 进程内决策缓存 {obs_id: accept}。deferred 条目下轮重评会重复
    quotas.consume(幻影消耗把日配额烧穿)——缓存决策,每条 obs 只评一次。"""
    stats = {"ingested": 0, "rejected": 0, "deferred": 0}
    for obs in rows:
        oid = obs["id"]
        if oid in wm["ingested"] or oid in wm["rejected"] or oid in wm["failed"]:
            continue
        if oid in decided:
            accept = decided[oid]
        else:
            d = evaluate(obs, cfg, quotas)
            try:
                log_decision(d, log_path=DECISIONS_LOG)
            except Exception:  # noqa: BLE001
                pass
            if d.layer == "quota" and not d.accept:
                # 配额拒绝是"今天满了"不是"永不要"——不落水印不缓存,改天再评
                stats["deferred"] += 1
                continue
            accept = d.accept
            decided[oid] = accept
        if not accept:
            wm["rejected"].add(oid)
            stats["rejected"] += 1
            save_watermark(wm)
            continue
        st = await ingest_via_api(obs)
        if st in ("ok", "skipped"):
            wm["ingested"].add(oid)
            stats["ingested"] += 1
            decided.pop(oid, None)
            log.info("[%s] obs-%d → %s", kind, oid, st)
        elif st == "409":
            # error 键服务端 24h 自动清理(cleanup_stuck_jobs)——409 只是暂时挡板,
            # 不永久放弃;deferred 留待键过期后重试。
            stats["deferred"] += 1
            log.warning("[%s] obs-%d → 409(上次抽取失败;键 24h 过期后自动重试)", kind, oid)
        else:  # error/timeout/net → 不记水印,下轮重试
            stats["deferred"] += 1
            log.warning("[%s] obs-%d → %s(下轮重试)", kind, oid, st)
            if st == "net":
                break  # server 不可达,本轮剩余直接留到下轮
        save_watermark(wm)
    return stats


async def main() -> int:
    log.info("kg-refinery Level-1 启动 url=%s db=%s interval=%ss backlog=%s",
             KG_HUB_URL, DB_PATH, INTERVAL, BACKLOG_ENABLED)
    # 启动等待 db(首次部署卷可能还空着,15min 后 launchd 同步才到位;别崩溃循环)
    while not DB_PATH.exists():
        log.warning("[startup] %s 不存在(等 Mac 侧同步),60s 后重查", DB_PATH)
        write_status(last_error=f"waiting for db: {DB_PATH}")
        await asyncio.sleep(60)
    wm = load_watermark()
    if wm.get("boundary_id") is None:
        wm["boundary_id"] = max_db_id()
        save_watermark(wm)
        log.info("[boundary] 首轮启动,boundary_id=%d(≤此为积压,夜间窗口烧)", wm["boundary_id"])

    quotas = QuotaTracker()
    quota_day = datetime.now(tz=CST).date()
    decided: dict[int, bool] = {}  # 进程内决策缓存(防 deferred 重评的配额幻影消耗)

    while True:
        try:
            if datetime.now(tz=CST).date() != quota_day:  # 日配额按天重置
                quotas = QuotaTracker()
                quota_day = datetime.now(tz=CST).date()
                decided.clear()
            # —— 温度门控:盘温超阈值本轮完全歇工(只写状态心跳),保硬件 ——
            dtemp = max_disk_temp()
            if dtemp is not None and dtemp >= MAX_DISK_TEMP:
                log.warning("[thermal] 盘温 %d°C ≥ 阈值 %d°C,本轮歇工", dtemp, MAX_DISK_TEMP)
                write_status(disk_temp=dtemp, thermal_hold=True, last_error=None)
                await asyncio.sleep(INTERVAL)
                continue
            cfg = load_config()  # 每轮重读(容器内烤的文件;换 bind-mount 后即热改)
            boundary = wm["boundary_id"]

            # —— live:游标推进(审查 R1:固定下界+LIMIT 会在积累>200条后永久卡死)
            terminal = wm["ingested"] | wm["rejected"] | wm["failed"]
            cursor = wm.get("live_cursor") or boundary
            live_ids = [i for i in fetch_ids(min_id_exclusive=cursor)
                        if i not in terminal][:200]
            s_live = await process_batch(
                fetch_rows_by_ids(live_ids), wm, cfg, quotas, decided, "live")
            # 游标只推进到"连续终态"的最高 id:deferred 挡住游标,下轮重取重试
            terminal = wm["ingested"] | wm["rejected"] | wm["failed"]
            new_cursor = cursor
            for i in fetch_ids(min_id_exclusive=cursor):
                if i in terminal:
                    new_cursor = i
                else:
                    break
            if new_cursor != cursor:
                wm["live_cursor"] = new_cursor
                save_watermark(wm)

            # —— backlog:只拉 id 列算余量;窗口开才按需取正文(审查 R6)
            s_back = {"ingested": 0, "rejected": 0, "deferred": 0}
            backlog_remaining = 0
            if BACKLOG_ENABLED:
                seen = wm["ingested"] | wm["rejected"] | wm["failed"]
                pending_ids = [i for i in fetch_ids(max_id_inclusive=boundary)
                               if i not in seen]
                backlog_remaining = len(pending_ids)
                if in_backlog_window() and pending_ids:
                    s_back = await process_batch(
                        fetch_rows_by_ids(pending_ids[:BACKLOG_PER_CYCLE]),
                        wm, cfg, quotas, decided, "backlog")
                    backlog_remaining -= s_back["ingested"] + s_back["rejected"]

            write_status(
                disk_temp=dtemp, thermal_hold=False,
                boundary_id=boundary, live_cursor=wm.get("live_cursor"),
                live_processed=s_live, backlog_processed=s_back,
                backlog_remaining=backlog_remaining,
                backlog_window_open=in_backlog_window(),
                per_cycle=BACKLOG_PER_CYCLE,
                watermark={"ingested": len(wm["ingested"]), "rejected": len(wm["rejected"]),
                           "failed": len(wm["failed"])},
                last_error=None,
            )
        except Exception as exc:  # noqa: BLE001 — 单轮失败不倒进程
            log.exception("[cycle] failed")
            write_status(last_error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
