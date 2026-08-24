#!/bin/sh
# 把 Mac 的 live claude-mem.db 同步到 NAS，供 kg-hub-refinery 消费。
#
#   Mac  ~/.claude-mem/claude-mem.db
#     └─(本脚本)→ NAS /volume2/4T/kg-hub-data/claude-mem/claude-mem.db
#                   └─(:ro 挂载)→ kg-hub-refinery ──POST /api/ingest──→ FalkorDB
#
# ── 2026-08-24 改为增量推送(T-0033) ─────────────────────────────────────
# 原来每轮传整库快照:79MB → gzip 26.9MB。而 Mac↔NAS 的实测吞吐只有
# ~25 KB/s(直连 67ms、小包正常，低吞吐疑似丢包/MTU/UDP 限速)，
#
#     整库 26.9MB / 25 KB/s ≈ 18 分钟  >  同步周期 15 分钟
#
# **单次传输比周期还长**,所以并发锁必然常态命中、落差必然持续扩大 ——
# 那不是偶发故障，是设计与链路能力不匹配的必然结果。
#
# 现在只传新增行(典型 3-19KB),合并在 NAS 本地做(磁盘 70.7 MB/s)。
# 载荷从 O(库大小) 变成 O(新增行数)，且不再随库增长。
#
# 端到端墙上时间 **1-15 秒**(实测区间,不是稳定的 1 秒)。构成:
#     ssh 握手      ~3 秒 × 2 次(探 NAS 状态 + 推增量)  ← 现在的大头
#     NAS 本地 cp   40MB ≈ 0.5 秒
#     真正的数据    4.5KB @25KB/s ≈ 0.2 秒             ← 已经微不足道
# 也就是说瓶颈已经从"传数据"变成"建连接"。若哪天嫌慢,下一步是 ssh
# ControlMaster 连接复用把两次握手并成一次,而不是再压数据。
# 15 秒占 900 秒周期的 1.7%,目前没必要。
#
# watermark 取**NAS 侧的 MAX(id)**而不是本地 stamp:没有本地状态可漂移，
# NAS 被重置/回滚也能自动补齐,天然自愈。
#
# 整库全量保留为兜底通道(见 full_rebuild):首次接入、NAS 库缺失或损坏时自动回退。
SRC="/Users/mac/.claude-mem/claude-mem.db"
NAS="commiao@100.123.208.32"
NAS_DIR="/volume2/4T/kg-hub-data/claude-mem"
DST="$NAS_DIR/claude-mem.db"
APPLIER_LOCAL="$(cd "$(dirname "$0")" && pwd)/nas_apply_claude_mem_delta.sh"
APPLIER_REMOTE="/volume1/docker/kg-hub-src/tools/nas_apply_claude_mem_delta.sh"
STATE="/Users/mac/.kg-hub/state"
STAMP="$STATE/claude-mem-synced.obsid"
mkdir -p "$STATE"
ts() { date '+%F %T'; }
# ConnectTimeout 只管建连;ServerAliveInterval/CountMax 才管**传输中途**僵住。
# 没有 keepalive 的 ssh 会无限期挂着(2026-08-21 实测挂了 23 分钟)。
SSHOPT="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

[ -f "$SRC" ] || { echo "$(ts) no source db"; exit 0; }

# ── 并发锁 ─────────────────────────────────────────────────────────────
# launchd 的 StartInterval **不会**在上一轮还活着时另起一轮,所以一次卡死
# 会让同步无限期冻结。这里自己判定并接管,不把活性交给 launchd。
LOCK="$STATE/sync.lock"
if [ -f "$LOCK" ]; then
  lpid=$(cat "$LOCK" 2>/dev/null)
  if [ -n "$lpid" ] && kill -0 "$lpid" 2>/dev/null; then
    # 跑了多久 = 现在 - 锁文件 mtime(锁是开工那刻写的)。不用 `ps -o etimes=`
    # —— 那是 GNU/procps 关键字,macOS 的 BSD ps 不认,会把关键字清单当成
    # $age,于是比较直接报错、永远落到 else,**接管逻辑等于从没生效过**。
    lmt=$(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null)
    now=$(date +%s)
    if [ -n "$lmt" ]; then age=$((now - lmt)); else age=0; fi
    if [ "$age" -gt 600 ]; then
      echo "$(ts) 上一轮 pid=$lpid 已卡 ${age}s,杀掉重来"
      pkill -9 -P "$lpid" 2>/dev/null
      kill -9 "$lpid" 2>/dev/null
    else
      echo "$(ts) 上一轮 pid=$lpid 仍在跑 (${age}s),本轮跳过"
      exit 0
    fi
  fi
fi
echo $$ > "$LOCK"

TMP=""
cleanup() { rm -f "$LOCK" "$TMP" "$TMP-wal" "$TMP-shm"; }
trap cleanup EXIT INT TERM

# 被 SIGKILL 打断时 trap 不会跑,临时库会留在 /tmp。开工先扫。
find /tmp -maxdepth 1 \( -name 'cm-snap.*' -o -name 'cm-delta.*' \) -type f -mmin +30 -delete 2>/dev/null

# ── 本地 watermark ─────────────────────────────────────────────────────
# 用 sqlite3 查而不是 shasum 文件:claude-mem 是 journal_mode=wal,新写入全在
# -wal 里,主库哈希长时间不变 → 判据永远命中 unchanged(T-0028 的病根)。
local_max=$(sqlite3 -readonly "$SRC" 'SELECT MAX(id) FROM observations;' 2>/dev/null)
case "$local_max" in
  ''|*[!0-9]*)
    # 读不到 watermark 是**真故障**(库被锁/损坏/schema 变了),绝不能当成
    # "没变化"静静跳过 —— 那就是又造一个静默失效。
    echo "$(ts) ERROR 读不到本地 watermark,本轮不同步"; exit 1 ;;
esac

# ── 构造「剥离版副本」──────────────────────────────────────────────────
# NAS 那份只需要 refinery 真正读的两张表:observations + sdk_sessions。
# 剥掉其余的有三个好处:
#   1. **绕开 fts5**。NAS 的 /usr/bin/sqlite3 没编 FTS5 模块,而 claude-mem 库里
#      有 observations_fts 等 FTS5 虚表 —— 只读查询能过,但一旦以**读写**方式
#      打开就报 `no such module: fts5`,增量合并根本做不了。
#   2. 兜底全量从 gzip 26.9MB 降到 11.0MB(FTS 索引 + session_summaries +
#      sync_outbox + user_prompts 等约 47MB 是纯本地产物,NAS 一行都用不到)。
#   3. 副本内容 = 消费契约,不多不少。
#
# 必须照搬原始 DDL 而不是 `CREATE TABLE AS SELECT` —— 后者产出的表**没有主键**,
# 远端的 `INSERT OR IGNORE` 就失去去重依据,会插进重复行。
subset_ddl() {
  sqlite3 -readonly "$SRC" "
    SELECT sql||';' FROM sqlite_master
    WHERE type IN ('table','index') AND tbl_name IN ('observations','sdk_sessions')
      AND sql IS NOT NULL
    ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,
             CASE tbl_name WHEN 'sdk_sessions' THEN 0 ELSE 1 END;"
}

# ── 为什么增量载荷是「SQLite 文件」而不是 NDJSON 文本 ──────────────────
# 先说清楚:**文本更小**。实测 gzip 后 —— 20 行 16.4KB vs 20.5KB,
# 106 行 54.5KB vs 66.9KB(约小 20-25%);1 行时差 3 倍(0.8KB vs 2.5KB,
# SQLite 有空 schema + 19 个索引的页开销地板)。
# 而且当初想的两个理由都不成立:表里只有 integer/null/text **没有 BLOB**,
# JSON 能无损往返;NAS 也有 python3 / jq / sqlite3 3.40(自带 JSON 函数)。
#
# 留着文件方案只为一件事:**没有序列化层可以写错**。
# ATTACH + `INSERT ... SELECT *` 全程由 SQLite 自己搬,不存在列名映射、
# NULL 与空串、数字与数字串这类要人肉维护的对应关系。
#
# 具体收益是 schema 漂移能被挡住并自愈(2026-08-24 实测):claude-mem 加一列后
#   applier → "has 27 columns but 28 values were supplied" → FAIL
#   线上库原封不动(size/MAX(id)/integrity 三项前后一致)
#   sync 收到 FAIL → 回退 full_rebuild → 副本按新 DDL 重造 → 自愈
# 文本方案要自己维护列清单,漏改就可能**静默写错列**而不是干脆失败。
#
# 代价是每次多传 ~20%。按实测 25 KB/s 折合约 0.2 秒 —— 传输早已不是瓶颈,
# 这个价买"错不了"很划算。若哪天同步频率提到分钟级、地板开销开始显眼,
# 再换文本不迟。
#
# $1=输出文件  $2=observations 的 WHERE 条件
# 输出库当 main(可写),源库以 mode=ro ATTACH —— 全程不写 claude-mem 的文件。
# (反过来用 `sqlite3 -readonly 源库` 再 ATTACH 是不行的:-readonly 会把整个
#  连接连同 ATTACH 进来的库一起设成只读。)
make_subset_db() {
  sqlite3 "$1" "
ATTACH 'file:$SRC?mode=ro' AS s;
$(subset_ddl)
INSERT INTO sdk_sessions SELECT * FROM s.sdk_sessions
  WHERE memory_session_id IN (SELECT memory_session_id FROM s.observations WHERE $2);
INSERT INTO observations SELECT * FROM s.observations WHERE $2;
" 2>/dev/null
}

# ── 全量兜底通道 ───────────────────────────────────────────────────────
full_rebuild() {
  why="$1"
  echo "$(ts) 全量重建($why)"
  TMP=$(mktemp /tmp/cm-snap.XXXXXX) && rm -f "$TMP" && TMP="$TMP.db"
  make_subset_db "$TMP" "1=1" \
    || { echo "$(ts) ERROR 构造全量副本失败"; return 1; }
  SIZE=$(wc -c < "$TMP" | tr -d ' ')
  # 远端在 mv 之前必须自己验:`cat > tmp && mv` 挡不住短流 —— ssh 一断,远端
  # cat 收到的是干净的 EOF → exit 0 → mv 把半截库装上去(2026-08-21 实测,
  # NAS 库变成 17301504 字节且 malformed,日志和退出码都看不出异常)。
  # 原子 mv 防的是"读到写一半的文件",防不住"写完的是个短文件"。
  R="gunzip -c > '$DST.tmp' \
   && [ \"\$(wc -c < '$DST.tmp' | tr -d ' ')\" = '$SIZE' ] \
   && [ \"\$(sqlite3 -readonly '$DST.tmp' 'PRAGMA integrity_check;' 2>/dev/null | head -1)\" = 'ok' ] \
   && [ \"\$(sqlite3 -readonly '$DST.tmp' 'SELECT MAX(id) FROM observations;' 2>/dev/null)\" = '$local_max' ] \
   && mv -f '$DST.tmp' '$DST' && rm -f '$DST-wal' '$DST-shm' \
   || { rm -f '$DST.tmp'; exit 9; }"
  for i in 1 2 3; do
    if gzip -c "$TMP" | ssh $SSHOPT "$NAS" "$R" >/dev/null 2>&1; then
      echo "$local_max" > "$STAMP"
      echo "$(ts) 全量重建完成 (MAX id=$local_max)"; return 0
    fi
    sleep 10
  done
  echo "$(ts) 全量重建失败(NAS 不可达或远端校验未过)"; return 1
}

# ── 探 NAS 侧状态:watermark + 完整性 + applier 是否就位 ────────────────
probe=$(ssh $SSHOPT "$NAS" "
  if [ -f '$DST' ]; then
    echo \"max=\$(sqlite3 -readonly '$DST' 'SELECT MAX(id) FROM observations;' 2>/dev/null)\"
    echo \"integ=\$(sqlite3 -readonly '$DST' 'PRAGMA integrity_check;' 2>/dev/null | head -1)\"
    echo \"fts=\$(sqlite3 -readonly '$DST' \"SELECT COUNT(*) FROM sqlite_master WHERE sql LIKE '%fts5%';\" 2>/dev/null)\"
  else
    echo 'max='; echo 'integ=missing'
  fi
  echo \"applier=\$(sha256sum '$APPLIER_REMOTE' 2>/dev/null | cut -c1-16)\"
" 2>/dev/null) || { echo "$(ts) NAS 不可达,本轮跳过"; exit 0; }

nas_max=$(echo "$probe"  | sed -n 's/^max=//p')
nas_integ=$(echo "$probe" | sed -n 's/^integ=//p')
nas_applier=$(echo "$probe" | sed -n 's/^applier=//p')
nas_fts=$(echo "$probe"     | sed -n 's/^fts=//p')

# 需要全量重建的三种情况
case "$nas_max" in ''|*[!0-9]*) full_rebuild "NAS 侧 watermark 读不到($nas_integ)"; exit $? ;; esac
[ "$nas_integ" = "ok" ] || { full_rebuild "NAS 侧库损坏($nas_integ)"; exit $?; }
[ "$nas_max" -le "$local_max" ] || { full_rebuild "NAS($nas_max) 领先本机($local_max),数据分叉"; exit $?; }
# 旧的「胖副本」(含 FTS5 虚表)以读写方式打不开(NAS sqlite3 没编 fts5),增量合并
# 做不了 → 自动重建成剥离版。这一条让格式迁移自愈,不需要人工跑一次。
case "$nas_fts" in ''|0) : ;; *) full_rebuild "NAS 副本是旧胖格式($nas_fts 个 fts5 对象),换剥离版"; exit $? ;; esac

if [ "$nas_max" -eq "$local_max" ]; then
  echo "$local_max" > "$STAMP"
  echo "$(ts) 无新 obs (MAX id=$local_max), skip"; exit 0
fi

# ── applier 保鲜:哈希不一致就重推(2KB,可忽略),杜绝版本漂移 ────────────
want=$(shasum -a 256 "$APPLIER_LOCAL" | cut -c1-16)
if [ "$nas_applier" != "$want" ]; then
  echo "$(ts) 推送 applier ($nas_applier → $want)"
  cat "$APPLIER_LOCAL" | ssh $SSHOPT "$NAS" \
    "cat > '$APPLIER_REMOTE.tmp' && mv -f '$APPLIER_REMOTE.tmp' '$APPLIER_REMOTE'" \
    || { echo "$(ts) applier 推送失败,本轮跳过"; exit 0; }
fi

# ── 生成增量库 ─────────────────────────────────────────────────────────
TMP=$(mktemp /tmp/cm-delta.XXXXXX) && rm -f "$TMP" && TMP="$TMP.db"
make_subset_db "$TMP" "id > $nas_max" \
  || { echo "$(ts) ERROR 生成增量库失败"; exit 1; }

nrow=$(sqlite3 -readonly "$TMP" 'SELECT COUNT(*) FROM observations;' 2>/dev/null)
kb=$(gzip -c "$TMP" | wc -c | awk '{printf "%.1f", $1/1024}')

# ── 推送 + 远端合并 ────────────────────────────────────────────────────
for i in 1 2 3; do
  out=$(gzip -c "$TMP" | ssh $SSHOPT "$NAS" "sh '$APPLIER_REMOTE' '$local_max'" 2>&1)
  case "$out" in
    *"OK merged="*)
      echo "$local_max" > "$STAMP"
      echo "$(ts) synced +$nrow 条 (${kb}KB) $nas_max → $local_max"
      exit 0 ;;
    *FAIL*)
      # 远端明确判定失败(合并/校验不过) —— 重试同样的输入没意义,
      # 且线上库未被触碰,直接回退全量重建。
      echo "$(ts) 远端合并失败: $out"
      full_rebuild "增量合并失败"; exit $? ;;
  esac
  sleep 10
done
echo "$(ts) 增量推送失败(NAS 不可达),下个周期重试"
exit 0
