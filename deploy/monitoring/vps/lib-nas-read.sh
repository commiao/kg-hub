#!/bin/sh
# kg-hub 监控脚本共用的「读 NAS 状态」。被 daily-summary.sh / progress.sh source。
#
# 为什么要抽出来：2026-09-17 发现 daily-summary.sh、progress.sh、status.sh 各自
# 抄了一份取数代码,三份都指着同一个错的地方 ——
#   * 数的是 ingesters/claude_mem_obs.py 的水位线,那个 ingester 早就退役了;
#   * 路径写死 /volume1/docker/kg-hub-data,真实数据根是 /volume2/4T/kg-hub-data。
# 于是日报天天说"正常",progress 安静了 96 天,而管线其实积压 7489、当轮入图 0。
# 抄三份就会漂三次。这里是唯一真源。
#
# 输出(制表符分隔,单行):
#   OK \t 累计入图 \t 累计拒 \t 游标落后 \t 积压 \t 图节点 \t 心跳秒龄 \t 本轮halted \t 暂停标志 \t 网关配额
#   ERR \t 人话原因
# 读不到一律走 ERR,绝不静默返回 0 —— "文件没了"和"今天没新增"必须长得不一样。

NAS="${KG_HUB_NAS:-commiao@100.123.208.32}"

KG_NAS_READ='
ENVF=/volume1/docker/kg-hub-src/.env
ROOT=$(sed -n "s/^KG_HUB_DATA_ROOT=//p" "$ENVF" 2>/dev/null)
if [ -z "$ROOT" ]; then printf "ERR\t读不到 KG_HUB_DATA_ROOT（%s）\n" "$ENVF"; exit 0; fi
ST="$ROOT/refinery-state/status.json"
if [ ! -f "$ST" ]; then printf "ERR\trefinery 状态文件不存在：%s\n" "$ST"; exit 0; fi
DBMAX=$(sqlite3 -readonly "$ROOT/claude-mem/claude-mem.db" "SELECT MAX(id) FROM observations;" 2>/dev/null)
NODES=$(redis-cli -h 127.0.0.1 -a "$(cat "$ROOT/dbpass.conf" 2>/dev/null)" --no-auth-warning GRAPH.QUERY kg_hub "MATCH (n) RETURN count(n)" 2>/dev/null | sed -n 2p)
python3 - "$ST" "$DBMAX" "$NODES" <<PY
import json, sys, os, datetime
st, dbmax, nodes = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    d = json.load(open(st))
except Exception as e:
    print("ERR\trefinery 状态读不动：%s" % type(e).__name__); raise SystemExit
wm = d.get("watermark") or {}
hb = d.get("heartbeat_at") or d.get("ts")
age = ""
if hb:
    try:
        t = datetime.datetime.fromisoformat(hb.replace("Z", "+00:00"))
        age = int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds())
    except Exception:
        age = ""
lag = ""
try:
    lag = int(dbmax) - int(d.get("live_cursor") or 0)
except Exception:
    pass
halted = (d.get("live_processed") or {}).get("result_counts", {}).get("halted", 0)

# ---- 网关配额 ----
# 读 kg-hub 自己导出的快照(watchdog 容器每 90s 写一次),不去翻网关的私有
# 状态目录 —— 那是另一个项目的内部结构,伸手进去就是下一次漂移。
#
# 分母只认 effective_limits。快照里另有一个 ceilings,那是策略天花板不是当前
# 生效值:实测 ceilings 给 kg_hub 写 120000,而网关路由表是 5000,拿它当分母
# 会算出"用了 0.4%"并且永远不报警。取不到就如实说取不到,不编一个分母 ——
# 这正是日报冻三个月的同一个教训。
quota = "-"
try:
    u = json.load(open(os.path.join(os.path.dirname(os.path.dirname(st)),
                                    "gateway-usage", "usage.json")))
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    used = {r["business_key"]: r["count"]
            for r in (u.get("daily") or []) if r.get("day") == today}
    lim = u.get("effective_limits") or {}
    parts = []
    for key, label in (("claude_mem.observation", "claude-mem"),
                       ("kg_hub.entity_extract", "kg-hub")):
        n = used.get(key, 0)
        cap = (lim.get(key) or {}).get("daily_requests")
        parts.append("%s=%d/%s" % (label, n, cap if cap else "?"))
    if not lim:
        why = (u.get("effective_limits_meta") or {}).get("reason") or "unknown"
        parts.append("配额上限取不到(%s)" % why)
    quota = " ".join(parts)
except Exception as e:
    quota = "用量快照读不到:%s" % type(e).__name__
flags = [n for n, v in (("断路器开", d.get("breaker_open")),
                        ("温控暂停", d.get("thermal_hold")),
                        ("配额暂停", d.get("quota_paused")),
                        ("被限流", d.get("rate_limited"))) if v]
print("\t".join(str(x) for x in (
    "OK", wm.get("ingested", ""), wm.get("rejected", ""), lag,
    d.get("backlog_remaining", ""), nodes, age, halted,
    ",".join(flags) or "-", quota)))
PY
'

# nas_read [重试次数] —— 成功打印上面那一行;连不上则什么都不打印(调用方自己决定
# 要不要报警：日报要报,20 分钟一次的 progress 不报,宕机归 check.sh 管)。
nas_read() {
  _tries="${1:-3}"; _i=0; _out=""
  while [ "$_i" -lt "$_tries" ]; do
    # -n 是必须的：不带它,这个 ssh 会继承调用脚本的 stdin。cron 从文件启动时
    # 无所谓,但只要有人用管道喂脚本(调试时很自然),它就会把剩下的脚本正文当
    # 输入吃掉,表现为"整个脚本一声不吭什么都没干"。实测踩过。
    _out=$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$NAS" "$KG_NAS_READ" 2>/dev/null)
    [ -n "$_out" ] && break
    _i=$((_i + 1)); [ "$_i" -lt "$_tries" ] && sleep 8
  done
  [ -n "$_out" ] && printf '%s\n' "$_out"
}
