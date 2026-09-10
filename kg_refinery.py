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
  backlog — id ≤ boundary_id 的历史积压:仅在夜间窗口(22:00-08:00 Asia/Shanghai)
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
import atexit
import json
import logging
import logging.handlers
import os
import queue
import signal
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import breakers  # noqa: E402
from utils.ingest_filter import (  # noqa: E402
    QuotaTracker, evaluate, load_config, log_decision,
)

# 这条线调模型用的 business key —— 拓扑图上「refinery」那个节点的开关就是它。
BREAKER_KEY = "kg_hub.entity_extract"


# A Docker log driver can still stall when its sink is unavailable.  The refinery
# heartbeat shares this process with batch work, so its application logging must
# never wait for stderr.  The listener may block on stderr; producers only make
# a bounded, non-blocking queue attempt and deliberately drop excess records.
REFINERY_LOG_QUEUE_SIZE = 1_024


class DroppingQueueHandler(logging.handlers.QueueHandler):
    """Never block or synchronously report errors from a bounded log queue."""

    def __init__(self, log_queue: queue.Queue) -> None:
        super().__init__(log_queue)
        self._lock = threading.Lock()
        self._dropped = 0
        self._last_drop_at: str | None = None
        # `write_status` can be called repeatedly while this process is alive.
        # Remember which process-local drops have reached disk, so a heartbeat
        # never re-adds the same events to the persistent cumulative counter.
        self._persisted_dropped = 0
        self._persisted_total = 0

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def last_drop_at(self) -> str | None:
        with self._lock:
            return self._last_drop_at

    def _record_drop(self) -> None:
        # This path deliberately contains no logging: it is used precisely
        # when a logging sink is unavailable or corrupt.
        try:
            timestamp = datetime.now(tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001 - keep the producer fail-open
            timestamp = None
        with self._lock:
            self._dropped += 1
            if timestamp is not None:
                self._last_drop_at = timestamp

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except Exception:  # noqa: BLE001 - queue errors must not reach stderr
            self._record_drop()

    def emit(self, record: logging.LogRecord) -> None:
        """Override QueueHandler.emit so prepare errors cannot call handleError.

        The stdlib implementation forwards preparation/enqueue exceptions to
        ``handleError``. That may synchronously write to stderr, reintroducing
        the exact Docker-log backpressure that this queue isolates.
        """
        try:
            self.enqueue(self.prepare(record))
        except Exception:  # noqa: BLE001 - including formatter failures
            self._record_drop()

    def status_fields(self, persisted_total: object,
                      persisted_last_drop_at: object) -> tuple[int, str | None, int]:
        """Return durable log-loss fields and the snapshot used to make them."""
        try:
            prior_total = max(0, int(persisted_total or 0))
        except (TypeError, ValueError):
            prior_total = 0
        prior_last = (persisted_last_drop_at
                      if isinstance(persisted_last_drop_at, str) else None)
        with self._lock:
            total = max(prior_total, self._persisted_total)
            total += max(0, self._dropped - self._persisted_dropped)
            return total, self._last_drop_at or prior_last, self._dropped

    def mark_status_persisted(self, total: int, dropped_snapshot: int) -> None:
        """Acknowledge only fields that made it through the atomic file replace."""
        with self._lock:
            self._persisted_total = max(self._persisted_total, total)
            self._persisted_dropped = max(self._persisted_dropped, dropped_snapshot)


class NonBlockingQueueListener(logging.handlers.QueueListener):
    """Drain on clean exit, but never make shutdown wait on a stuck stderr."""

    def stop(self, timeout: float = 1.0) -> None:
        thread = self._thread
        if thread is None:
            return
        try:
            self.enqueue_sentinel()
        except queue.Full:
            # There is no value in preserving queued output during shutdown if
            # the listener cannot accept its sentinel. Drop it and retry.
            try:
                while True:
                    self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.enqueue_sentinel()
            except Exception:  # noqa: BLE001 - shutdown must remain fail-open
                return
        except Exception:  # noqa: BLE001 - shutdown must remain fail-open
            return
        thread.join(timeout=timeout)
        if not thread.is_alive():
            self._thread = None


def configure_refinery_logging() -> tuple[
        logging.Logger, DroppingQueueHandler, logging.handlers.QueueListener]:
    """Send refinery logs through a bounded queue before writing to stderr."""
    # Production logging must not invoke the logging module's synchronous
    # stderr diagnostics if a handler fails. DroppingQueueHandler additionally
    # overrides emit, so this is a defence-in-depth policy for listener errors.
    logging.raiseExceptions = False
    log_queue: queue.Queue = queue.Queue(maxsize=REFINERY_LOG_QUEUE_SIZE)
    queue_handler = DroppingQueueHandler(log_queue)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(logging.Formatter("%(asctime)s [refinery] %(message)s"))
    listener = NonBlockingQueueListener(log_queue, stderr_handler)

    refinery_log = logging.getLogger("refinery")
    refinery_log.setLevel(logging.INFO)
    refinery_log.handlers.clear()
    refinery_log.addHandler(queue_handler)
    refinery_log.propagate = False
    listener.start()
    return refinery_log, queue_handler, listener


log, REFINERY_LOG_HANDLER, REFINERY_LOG_LISTENER = configure_refinery_logging()


def shutdown_refinery_logging() -> None:
    """Stop the daemon listener when possible without delaying process exit."""
    try:
        REFINERY_LOG_LISTENER.stop()
    except Exception:  # noqa: BLE001 - logging cleanup must never block exit
        pass


def _handle_sigterm(signum: int, _frame: object) -> None:
    shutdown_refinery_logging()
    raise SystemExit(128 + signum)


atexit.register(shutdown_refinery_logging)

KG_HUB_URL = os.environ.get("KG_HUB_URL", "http://kg_hub_server:8080").rstrip("/")
TOKEN = os.environ.get("KG_HUB_API_TOKEN", "")
DB_PATH = Path(os.environ.get("KG_HUB_REFINERY_DB", "/data/claude-mem/claude-mem.db"))
STATE_DIR = Path(os.environ.get("KG_HUB_REFINERY_STATE", "/state"))
INTERVAL = int(os.environ.get("KG_HUB_REFINERY_INTERVAL_SEC", "90"))
DEFAULT_HEARTBEAT_INTERVAL = 60
MIN_HEARTBEAT_INTERVAL = 15
MAX_HEARTBEAT_INTERVAL = 300


def bounded_heartbeat_interval(raw: str | None) -> int:
    """心跳不能过密写盘，也不能慢到躲过 15 分钟存活告警。"""
    try:
        value = int(raw or DEFAULT_HEARTBEAT_INTERVAL)
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_INTERVAL
    return min(MAX_HEARTBEAT_INTERVAL, max(MIN_HEARTBEAT_INTERVAL, value))


# 这是存活信号，不是批次进度：长批次逐条 poll 时也必须持续刷新。
HEARTBEAT_INTERVAL = bounded_heartbeat_interval(
    os.environ.get("KG_HUB_REFINERY_HEARTBEAT_SEC"))
BACKLOG_PER_CYCLE = int(os.environ.get("KG_HUB_REFINERY_BACKLOG_PER_CYCLE", "15"))
BACKLOG_ENABLED = os.environ.get("KG_HUB_REFINERY_BACKLOG", "1").lower() in ("1", "true", "yes")
# 夜间回填窗口(北京时间 / Asia/Shanghai, UTC+8;含头不含尾;跨午夜写成 start>end)。
# 默认 22:00-08:00，由环境变量显式覆盖时以覆盖值为准。
BACKLOG_START = int(os.environ.get("KG_HUB_REFINERY_WINDOW_START", "22"))
BACKLOG_END = int(os.environ.get("KG_HUB_REFINERY_WINDOW_END", "8"))
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
CST = timezone(timedelta(hours=8))  # 北京时间 / Asia/Shanghai (UTC+8),夜间窗口按此判
# 409 退避(2026-08-25 修活锁):服务端 error 键存在时 POST 必回 409。原实现把
# 409 当"下轮重试",于是 LLM 供应商失效期间每 90s 重试全部积压 —— 一夜刷了
# **6864 条 409 日志**、白烧 CPU/IO,而 backlog_remaining 一动不动、last_error
# 为 null、watchdog state=OK。**"持续失败但看起来在跑"正是本项目要消灭的失效
# 模式,我自己造了一个。** 现在同一 obs 连续 409 按指数退避,冷却期直接跳过不发请求。
# 退避序列(轮):1 → 2 → 4 → 8 → 16 → 32,上限 ~48min(服务端 error 键 24h 过期,
# 退避到上限后仍会周期性试探,不会永久放弃)。
BACKOFF_MAX_CYCLES = 32
# 单批内并发抽取上限。服务端 do_extract 用 async_writer_lock 串行化 add_episode,
# 所以这不会让抽取吞吐翻倍;它消除的是**批内队头阻塞**——此前一条慢抽取(p90 180s)
# 会把身后的快速失败条目全堵住,服务端的锁队列反而空转。2 与服务端 SEMAPHORE_LIMIT
# 对齐;锁竞争由服务端 180s 超时 ×5 次线性退避兜住,抽取 p90 远在容忍内。
INGEST_CONCURRENCY = max(1, int(os.environ.get("KG_HUB_REFINERY_INGEST_CONCURRENCY", "2")))
# 轮询节奏:先密后疏。实测决策间隔中位数 8.1s == 固定轮询周期,说明**过半条目在 1s 内
# 就有终态**(多为 409/error 快速失败),却要白等一个整周期(2026-09-07 实测)。
POLL_STEPS_S = (1, 1, 2, 4, 8)
# 网关回「配额耗尽」(日/分上限)时暂停的轮数;到期后再探一条,仍耗尽则再停。
QUOTA_PAUSE_CYCLES = int(os.environ.get("KG_HUB_REFINERY_QUOTA_PAUSE_CYCLES", "20"))
# 上游 SDK 的 RateLimitError 没有 status_code 时，服务端会把错误键按 1 小时
# 释放。多等一轮，避免刚好在阈值前探测又撞到 409 并进入指数退避。
RATE_LIMIT_PAUSE_CYCLES = int(os.environ.get(
    "KG_HUB_REFINERY_RATE_LIMIT_PAUSE_CYCLES",
    str((3600 + INTERVAL - 1) // INTERVAL + 1),
))

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
    step = 0
    while waited < max_wait:
        code, d = _http("GET", f"{KG_HUB_URL}/api/ingest/status?{q}")
        st = d.get("status", "")
        # 必须先看 HTTP code:服务端「键不存在」返回 **404 + {"status":"error"}**,
        # 旧实现只读正文的 status,于是把「键还没建出来 / 键被清理器删掉」当成
        # 这条观测抽取失败,日志里是一条假的 error(T-0033 记录的隐患)。
        # 2026-09-07 把首次轮询从 8s 缩到 1s 后,撞上该窗口的概率变大,所以一起修。
        if code == 404:
            return "gone"       # 键不见了:不是抽取失败,本轮 defer、下轮重试
        if code == 400:
            return "error"      # 参数问题:重试也不会变好
        if code == 200:
            if st == "error" and d.get("error_kind") == "quota_exhausted":
                return "quota"  # 网关配额拒绝:暂停后再探
            if st == "error" and d.get("error_kind") == "rate_limited":
                return "rate_limited"  # 上游限流:等服务端释放错误键后再探
            if st in ("ok", "skipped", "error"):
                return st
        # code == 0(网络层)或 5xx:瞬时故障,继续轮询直到 max_wait
        delay = POLL_STEPS_S[min(step, len(POLL_STEPS_S) - 1)]
        step += 1
        await asyncio.sleep(delay)
        waited += delay
    return "timeout"


async def ingest_via_api(obs: dict) -> str:
    """返回终态或无内容的 HTTP 类别，供状态页诊断 deferred。"""
    p = to_payload(obs)
    code, d = _http("POST", f"{KG_HUB_URL}/api/ingest", p, timeout=60)
    if code == 0:
        return "net"
    if code == 409:
        return "409"
    if code >= 400:
        # 不把响应 message 写进状态：它可能含上游细节。状态码已足够区分
        # 参数/认证/服务端失败，且与原有 generic error 一样会在下一轮重试。
        diagnostic = str(d.get("diagnostic") or "")
        if (0 < len(diagnostic) <= 80
                and all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in diagnostic)):
            return f"http_{code}_{diagnostic}"
        return f"http_{code}"
    st = d.get("status", "")
    if st in ("ok", "skipped"):
        return st
    if code == 202 or st in ("accepted", "in_progress"):
        return await poll_until_done(p["source_description"], p["source_obs_id"])
    return "error"


# ---------- 状态外露 ----------

def write_status(*, heartbeat_only: bool = False, **kw) -> None:
    """原子更新状态。heartbeat_at 是存活信号；ts 是最近一轮处理状态。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cur = json.loads(STATUS.read_text()) if STATUS.exists() else {}
        now = datetime.now(tz=timezone.utc).isoformat()
        if not heartbeat_only:
            cur.update(kw, ts=now)
        cur["heartbeat_at"] = now
        log_total, log_last_drop_at, dropped_snapshot = REFINERY_LOG_HANDLER.status_fields(
            cur.get("log_dropped_total"), cur.get("log_last_drop_at"))
        cur["log_dropped_total"] = log_total
        cur["log_last_drop_at"] = log_last_drop_at
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False))
        tmp.replace(STATUS)
        REFINERY_LOG_HANDLER.mark_status_persisted(log_total, dropped_snapshot)
    except Exception:  # noqa: BLE001
        log.exception("[status] write failed (non-fatal)")


def in_backlog_window() -> bool:
    """工作窗口:refinery 的全部摄入(新 obs + 积压回填)只在此窗口内跑,窗口外
    完全静默(白天不与真实使用抢 LLM/IO,也避开室温峰值)。默认北京时间 22:00-08:00,
    可经 env 调(跨午夜按 start>end 处理)。"""
    h = datetime.now(tz=CST).hour
    if BACKLOG_START == BACKLOG_END:
        return False
    if BACKLOG_START < BACKLOG_END:          # 同日窗口,如 1-5
        return BACKLOG_START <= h < BACKLOG_END
    return h >= BACKLOG_START or h < BACKLOG_END   # 跨午夜,如 22-5


async def heartbeat_loop() -> None:
    """独立于工作窗口和长批次的 refinery liveness 心跳。"""
    while True:
        write_status(heartbeat_only=True)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ---------- 主循环 ----------

async def process_batch(rows: list[dict], wm: dict, cfg: dict,
                        quotas: QuotaTracker, decided: dict,
                        backoff: dict[int, list[int]], cycle: int, kind: str,
                        quota_pause: dict | None = None,
                        on_progress=None) -> dict:
    """decided: 进程内决策缓存 {obs_id: accept}。deferred 条目下轮重评会重复
    quotas.consume(幻影消耗把日配额烧穿)——缓存决策,每条 obs 只评一次。

    三段式:①过滤必须串行(要动日配额且每条只评一次) ②抽取有界并发(消除批内队头
    阻塞) ③落账串行(水印/退避表的读改写不能交错)。

    on_progress(stats):每条落账后回调一次,用于刷新对外状态。stats 是就地更新的
    同一个字典,调用方拿到的永远是最新累计值。没有它的话,批内进度对外不可见——
    积压批 8 条要等最后一条(可能在 timeout 重试)才刷一次 status,于是
    backlog_remaining 明明已经降了却还显示旧值(2026-09-07 夜实测到这一幕)。
    """
    # 对外状态只能给出类别聚合，绝不携带 observation 文本、提示词或令牌。
    # 仅有 deferred 总数无法判断卡在本地过滤、幂等键、网络还是服务端，运维会
    # 被迫猜测；这两个小计让下一轮状态直接说明哪一层作出了决定。
    stats = {"ingested": 0, "rejected": 0, "deferred": 0, "backoff_skipped": 0,
             "filter_counts": {}, "result_counts": {}}

    def count(bucket: str, label: str) -> None:
        values = stats[bucket]
        values[label] = values.get(label, 0) + 1

    to_ingest: list[dict] = []
    for obs in rows:
        oid = obs["id"]
        if oid in wm["ingested"] or oid in wm["rejected"] or oid in wm["failed"]:
            continue
        # 409 退避:冷却期内直接跳过,连请求都不发(活锁的根治点)
        bo = backoff.get(oid)
        if bo and cycle < bo[1]:
            stats["backoff_skipped"] += 1
            count("result_counts", "backoff")
            continue
        if oid in decided:
            accept = decided[oid]
        else:
            d = evaluate(obs, cfg, quotas)
            count("filter_counts", d.layer or "unknown")
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
            if on_progress is not None:
                try:
                    on_progress(stats)
                except Exception:  # noqa: BLE001
                    log.exception("[%s] on_progress failed (non-fatal)", kind)
            continue
        to_ingest.append(obs)

    # ② 有界并发抽取,**每条完成即落账**。
    #
    # 第一版把落账放在 gather 之后,结果 200 条一批要全跑完(实测 ~100 分钟)才写一次
    # 水印:网关明明在被调用,而 backlog_remaining / watermark 半小时一动不动,既没有
    # 增量进度也没有崩溃后的durability。settle() 是纯同步函数,asyncio 单线程且它内部
    # 没有 await ⇒ 与其他任务不会交错,可以安全地在每个任务里就地落账。
    halt = {"stop": False}

    def settle(obs: dict, st: str) -> None:
        oid = obs["id"]
        count("result_counts", st or "unknown")
        if st in ("ok", "skipped"):
            wm["ingested"].add(oid)
            stats["ingested"] += 1
            decided.pop(oid, None)
            backoff.pop(oid, None)   # 成功即清退避,恢复后立刻全速
            log.info("[%s] obs-%d → %s", kind, oid, st)
        elif st == "409":
            # 指数退避:n 次连续 409 → 等 2^(n-1) 轮再试(上限 BACKOFF_MAX_CYCLES)。
            # 服务端 error 键 24h 会过期,故退避到上限后仍周期试探,不永久放弃。
            n = (backoff.get(oid, [0, 0])[0]) + 1
            wait = min(2 ** (n - 1), BACKOFF_MAX_CYCLES)
            backoff[oid] = [n, cycle + wait]
            stats["deferred"] += 1
            # 只在前 3 次打 warning,之后降 debug —— 日志量本身也是故障放大器
            (log.warning if n <= 3 else log.debug)(
                "[%s] obs-%d → 409(第 %d 次,退避 %d 轮≈%dmin)",
                kind, oid, n, wait, wait * INTERVAL // 60)
        elif st == "quota":
            # 网关日/分上限:请求根本没到供应商,失败与这条观测无关。继续逐条撞只会白烧
            # 每篇前面的调用并堆 error 键(2026-09-06 夜 218 篇败/127 篇成),暂停,
            # QUOTA_PAUSE_CYCLES 轮后再探一条。
            if quota_pause is not None:
                quota_pause["hits"] = quota_pause.get("hits", 0) + 1
                candidate_until = cycle + QUOTA_PAUSE_CYCLES
                # 一批最多两条已在飞。较短的配额暂停不能覆盖同批刚落下的
                # 更长上游限流暂停，否则会在错误键释放前又撞上 409。
                if candidate_until >= quota_pause.get("until_cycle", 0):
                    quota_pause["until_cycle"] = candidate_until
                    quota_pause["reason"] = "quota_exhausted"
            stats["quota_paused"] = 1
            stats["deferred"] += 1
            log.warning("[%s] obs-%d → 网关配额耗尽,停发 %d 轮(≈%dmin)后再探",
                        kind, oid, QUOTA_PAUSE_CYCLES, QUOTA_PAUSE_CYCLES * INTERVAL // 60)
        elif st == "rate_limited":
            # 服务端对这类键按 1 小时快清；暂停必须长于该阈值，否则下一次探测
            # 仍是同一把 error 键的 409，反而将无关的限流变成单条指数退避。
            if quota_pause is not None:
                quota_pause["hits"] = quota_pause.get("hits", 0) + 1
                candidate_until = cycle + RATE_LIMIT_PAUSE_CYCLES
                if candidate_until >= quota_pause.get("until_cycle", 0):
                    quota_pause["until_cycle"] = candidate_until
                    quota_pause["reason"] = "rate_limited"
            stats["rate_limited"] = 1
            stats["deferred"] += 1
            log.warning("[%s] obs-%d → 上游限流,停发 %d 轮(≈%dmin)后再探",
                        kind, oid, RATE_LIMIT_PAUSE_CYCLES,
                        RATE_LIMIT_PAUSE_CYCLES * INTERVAL // 60)
        else:  # error/timeout/net → 不记水印,下轮重试
            stats["deferred"] += 1
            log.warning("[%s] obs-%d → %s(下轮重试)", kind, oid, st)
        save_watermark(wm)
        if on_progress is not None:
            try:
                on_progress(stats)
            except Exception:  # noqa: BLE001 — 刷状态失败不该影响入图
                log.exception("[%s] on_progress failed (non-fatal)", kind)

    if to_ingest:
        gate = asyncio.Semaphore(INGEST_CONCURRENCY)

        async def run(obs: dict) -> None:
            if halt["stop"]:
                stats["deferred"] += 1      # 没发出去,下轮重试
                count("result_counts", "halted")
                return
            async with gate:
                if halt["stop"]:
                    stats["deferred"] += 1
                    count("result_counts", "halted")
                    return
                st = await ingest_via_api(obs)
            if st in ("quota", "rate_limited", "net"):
                halt["stop"] = True         # 尚未拿到令牌的条目不再发
            settle(obs, st)

        await asyncio.gather(*(run(o) for o in to_ingest))
    return stats


def select_backlog_batch(pending_ids: list[int], backoff: dict[int, list[int]],
                         cycle: int, limit: int = BACKLOG_PER_CYCLE) -> list[int]:
    """积压窗口取数:冷却中的 id 不占名额。

    2026-09-03→09-06 积压 7919 三天零进展:队头 8 条因网关 425 反复失败,
    `pending_ids[:8]` 每轮取到的都是它们——退避只是让它们"跳过",名额却仍被占着,
    身后 7900 条一条也轮不到。这里先排除冷却中的再截前 N 条:失败项退避期间把名额
    让给后面的,退避到期照常回来重试,不丢数据。"""
    picked: list[int] = []
    for oid in pending_ids:
        bo = backoff.get(oid)
        if bo and cycle < bo[1]:
            continue
        picked.append(oid)
        if len(picked) >= limit:
            break
    return picked


async def main() -> int:
    log.info("kg-refinery Level-1 启动 url=%s db=%s interval=%ss backlog=%s",
             KG_HUB_URL, DB_PATH, INTERVAL, BACKLOG_ENABLED)
    # 主循环的 status 只在一轮处理结束后才落盘；另起任务避免长 poll 批次把它
    # 误当成进程死亡。heartbeat_at 与处理进度 ts 是两个不同信号。
    _heartbeat_task = asyncio.create_task(heartbeat_loop())
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
    backoff: dict[int, list[int]] = {}   # obs_id → [连续409次数, 下次可试的 cycle]
    quota_pause: dict = {}               # 网关配额耗尽 → {"until_cycle", "hits"}
    breaker_held = False                 # 只在状态翻转时打日志,不每轮刷屏
    cycle = 0

    while True:
        cycle += 1
        try:
            # —— 人工断路器:排在所有门控最前面 ——
            # 这是**停流**,不是拒绝。开关一关本轮一条都不选、一条都不提交,所以
            # 既不产生请求也不产生错误——不会出现"关了开关却一直撞墙报错、把
            # backoff 和 24h 错误键刷满"的局面。观测原样留在积压里,开关一开
            # 下一轮自动接着跑。
            # (model_gateway_client 里还有一层硬挡,那层是兜底,正常永不触发。)
            tripped, reason = breakers.is_tripped(BREAKER_KEY)
            if tripped:
                if not breaker_held:
                    log.warning("[breaker] %s 已人工断开,停止提交:%s",
                                BREAKER_KEY, reason or "(未填原因)")
                    breaker_held = True
                write_status(breaker_open=True, breaker_reason=reason,
                             backlog_window_open=in_backlog_window(),
                             last_error=None)
                await asyncio.sleep(INTERVAL)
                continue
            if breaker_held:
                log.info("[breaker] %s 已恢复,继续提交", BREAKER_KEY)
                breaker_held = False
            if datetime.now(tz=CST).date() != quota_day:  # 日配额按天重置
                quotas = QuotaTracker()
                quota_day = datetime.now(tz=CST).date()
                decided.clear()
            # —— 温度门控:盘温超阈值本轮完全歇工(只写状态心跳),保硬件 ——
            dtemp = max_disk_temp()
            if dtemp is not None and dtemp >= MAX_DISK_TEMP:
                log.warning("[thermal] 盘温 %d°C ≥ 阈值 %d°C,本轮歇工", dtemp, MAX_DISK_TEMP)
                write_status(disk_temp=dtemp, thermal_hold=True,
                             backlog_window_open=in_backlog_window(), last_error=None)
                await asyncio.sleep(INTERVAL)
                continue
            # —— 工作窗口门控(默认北京时间 22:00-08:00):窗口外新 obs 与
            # 积压一律不动,只留心跳。数据在 db/水印里等着,窗口一开自动追平。
            if not in_backlog_window():
                write_status(disk_temp=dtemp, thermal_hold=False,
                             backlog_window_open=False, idle_outside_window=True,
                             last_error=None)
                await asyncio.sleep(INTERVAL)
                continue
            cfg = load_config()  # 每轮重读(容器内烤的文件;换 bind-mount 后即热改)
            if cycle < quota_pause.get("until_cycle", 0):
                pause_reason = quota_pause.get("reason", "quota_exhausted")
                write_status(quota_paused=pause_reason == "quota_exhausted",
                             rate_limited=pause_reason == "rate_limited",
                             rate_limit_paused_until_cycle=quota_pause["until_cycle"],
                             rate_limit_hits=quota_pause.get("hits", 0),
                             quota_paused_until_cycle=quota_pause["until_cycle"],
                             quota_hits=quota_pause.get("hits", 0), last_error=None)
                await asyncio.sleep(INTERVAL)
                continue
            boundary = wm["boundary_id"]

            def snapshot(**extra):
                """每完成一段就落一次状态。

                此前只在**整轮结束**时写一次。而 live 线一轮取 200 条、每条真抽取
                约 3 分钟 ⇒ 一轮可达 10 小时,期间 backlog_remaining /
                backlog_window_open 全是几小时前的过期值(2026-09-07 我自己被它误导
                过一次:以为 refinery 没在干活,实际正在跑)。存活有独立 heartbeat,
                但**进度**必须增量可见 —— 与今天修的"200 条一批才落一次账"同一类。
                """
                write_status(
                    disk_temp=dtemp, thermal_hold=False, idle_outside_window=False,
                    boundary_id=boundary, live_cursor=wm.get("live_cursor"),
                    backlog_window_open=in_backlog_window(),
                    per_cycle=BACKLOG_PER_CYCLE,
                    backoff_pending=len(backoff),
                    quota_paused=False,
                    rate_limited=False,
                    rate_limit_paused_until_cycle=quota_pause.get("until_cycle"),
                    rate_limit_hits=quota_pause.get("hits", 0),
                    quota_paused_until_cycle=quota_pause.get("until_cycle"),
                    quota_hits=quota_pause.get("hits", 0),
                    breaker_open=False, breaker_reason="",
                    watermark={"ingested": len(wm["ingested"]),
                               "rejected": len(wm["rejected"]),
                               "failed": len(wm["failed"])},
                    last_error=None, **extra)

            # —— backlog 先跑 ——
            # 顺序在 2026-09-07 夜间实测后调换:live 线自己积压 1006 条、每轮取 200 条,
            # 一轮就吃掉整个 12 小时窗口,排在它后面的积压那 8 个名额**整夜拿不到**
            # (backlog_remaining 连续 4 天恒为 7786)。积压先跑保证每轮必得名额;
            # 代价只是新观测入图晚一轮(90 秒),而积压已经等了几个月。
            s_back = {"ingested": 0, "rejected": 0, "deferred": 0}
            backlog_remaining = 0
            if BACKLOG_ENABLED:
                seen = wm["ingested"] | wm["rejected"] | wm["failed"]
                pending_ids = [i for i in fetch_ids(max_id_inclusive=boundary)
                               if i not in seen]
                backlog_remaining = len(pending_ids)
                if in_backlog_window() and pending_ids:
                    # 每条落账即刷:积压余量随之递减,不必等整批 8 条跑完
                    s_back = await process_batch(
                        fetch_rows_by_ids(select_backlog_batch(pending_ids, backoff, cycle)),
                        wm, cfg, quotas, decided, backoff, cycle, "backlog",
                        quota_pause=quota_pause,
                        on_progress=lambda st: snapshot(
                            backlog_processed=dict(st),
                            backlog_remaining=backlog_remaining
                            - st["ingested"] - st["rejected"]))
                    backlog_remaining -= s_back["ingested"] + s_back["rejected"]
            snapshot(backlog_processed=s_back, backlog_remaining=backlog_remaining,
                     live_processed={"ingested": 0, "rejected": 0, "deferred": 0,
                                     "pending_this_cycle": True})

            # —— live:游标推进(审查 R1:固定下界+LIMIT 会在积累>200条后永久卡死)
            terminal = wm["ingested"] | wm["rejected"] | wm["failed"]
            cursor = wm.get("live_cursor") or boundary
            live_ids = [i for i in fetch_ids(min_id_exclusive=cursor)
                        if i not in terminal][:200]
            s_live = await process_batch(
                fetch_rows_by_ids(live_ids), wm, cfg, quotas, decided, backoff, cycle, "live",
                quota_pause=quota_pause,
                on_progress=lambda st: snapshot(
                    live_processed=dict(st), backlog_processed=s_back,
                    backlog_remaining=backlog_remaining))
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

            snapshot(live_processed=s_live, backlog_processed=s_back,
                     backlog_remaining=backlog_remaining)
        except Exception as exc:  # noqa: BLE001 — 单轮失败不倒进程
            log.exception("[cycle] failed")
            write_status(last_error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    previous_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        sys.exit(asyncio.run(main()))
    finally:
        shutdown_refinery_logging()
        signal.signal(signal.SIGTERM, previous_sigterm)
