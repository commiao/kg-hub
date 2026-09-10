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
INBOX_LOCAL="/Users/mac/public-sync/kg-hub-inbox"   # Synology Drive 同步盘,主传输路径
STATE="/Users/mac/.kg-hub/state"
STAMP="$STATE/claude-mem-synced.obsid"
mkdir -p "$STATE"
ts() { date '+%F %T'; }
# ConnectTimeout 只管建连;ServerAliveInterval/CountMax 才管**传输中途**僵住。
# 没有 keepalive 的 ssh 会无限期挂着(2026-08-21 实测挂了 23 分钟)。
SSHOPT="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

[ -f "$SRC" ] || { echo "$(ts) no source db"; exit 0; }

# --rebuild:强制走兜底重建。兜底本来只在副本损坏/格式不符时触发,属于罕用路径
# —— 而罕用路径的通病是"用到时才发现早就坏了"。给它一个能随时手动走一遍的入口,
# 既方便给新 NAS 播种,也让这条路可以被定期演练。
# Kernel lock + independent supervisor: launchd need not start a second run.
if [ "${1:-}" != "--guarded" ]; then
  exec python3 "$(dirname "$0")/sync_guard.py" "$STATE/sync.flock" 600 /bin/sh "$0" --guarded "$@"
fi
shift
FORCE_REBUILD=""
[ "${1:-}" = "--rebuild" ] && FORCE_REBUILD=1
TMP=""
cleanup() { [ -z "$TMP" ] || rm -f "$TMP" "$TMP-wal" "$TMP-shm"; }
trap cleanup EXIT
trap 'exit 143' TERM HUP
trap 'exit 130' INT

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

# ── 推送 payload 并触发远端合并 ────────────────────────────────────────
# $1 = 本地增量库路径   $2 = 合并后应有的 MAX(id)
#
# 主路径走 **Synology Drive 同步盘**,不走 ssh 管道。理由是可用性,不是速度。
# 2026-08-24 在公司 Wi-Fi 上实测,tailscale 退化到 50% 丢包时:
#     ssh 小包             3/3 通
#     ssh 大块 133KB 起    全部 Timeout(连 6KB 稳态增量都失败过)
#     同步盘               4MB/15s、8KB/7s 正常
# 也就是说这条链路只保得住控制面。于是:控制面(读 watermark、触发合并、
# 校验结果)继续走 ssh 小包,真正的字节走同步盘。
#
# 同步盘因此**每 15 分钟被走一遍**,不会变成"用到时才发现早就坏了"的罕用
# 路径 —— 这正是先前否掉「稳态 ssh + 兜底同步盘」两条链路方案的那个理由。
#
# ssh 管道保留为兜底:Drive 客户端掉线等导致同步盘迟迟不到时,当轮改走它。
# $3 = merge(默认,稳态增量) | replace(兜底重建,整份换掉)
# 走了哪条路要落到文件而不是变量:调用方写的是 `out=$(push_payload ...)`,
# 命令替换开子 shell,函数里对变量的赋值传不回父进程(2026-08-24 实测打出空的 [])。
VIA_FILE="$STATE/.push_via"
push_via() { printf '%s' "$1" > "$VIA_FILE"; }
push_payload() {
  _src="$1"; _expect="$2"; _mode="${3:-merge}"
  push_via "?"
  _gz=$(mktemp /tmp/cm-push.XXXXXX) && gzip -c "$_src" > "$_gz"
  _bytes=$(wc -c < "$_gz" | tr -d ' ')
  _sha=$(shasum -a 256 "$_gz" | cut -d' ' -f1)
  _fname="cm-$_expect-$$.db.gz"
  _out=""

  if mkdir -p "$INBOX_LOCAL" 2>/dev/null; then
    # 先写 .part 再改名:Drive 一见文件就开始同步,而 applier 只按传入的确切
    # 文件名找 —— 半截文件带 .part 前缀,不可能被误认领。
    cp -f "$_gz" "$INBOX_LOCAL/.$_fname.part" \
      && mv -f "$INBOX_LOCAL/.$_fname.part" "$INBOX_LOCAL/$_fname"
    _out=$(ssh $SSHOPT "$NAS" \
      "MODE='$_mode' sh '$APPLIER_REMOTE' '$_expect' '$_fname' '$_bytes' '$_sha'" 2>&1)
    rm -f "$INBOX_LOCAL/$_fname" "$INBOX_LOCAL/.$_fname.part"
    case "$_out" in
      *"OK merged="*) rm -f "$_gz"; push_via 同步盘; echo "$_out"; return 0 ;;
      *未送达*)       echo "$(ts) 同步盘未送达,本轮改走 ssh 管道" >&2 ;;
      *)              rm -f "$_gz"; push_via 同步盘; echo "$_out"; return 1 ;;  # 远端明确判失败,换路也没用
    esac
  fi

  _out=$(ssh $SSHOPT "$NAS" "MODE='$_mode' sh '$APPLIER_REMOTE' '$_expect'" < "$_gz" 2>&1)
  rm -f "$_gz"
  push_via ssh管道
  echo "$_out"
  case "$_out" in *"OK merged="*) return 0 ;; *) return 1 ;; esac
}

# ── 全量兜底通道 ───────────────────────────────────────────────────────
full_rebuild() {
  why="$1"
  echo "$(ts) 全量重建($why)"
  TMP=$(mktemp /tmp/cm-snap.XXXXXX) && rm -f "$TMP" && TMP="$TMP.db"
  # 上界同样是 $local_max,不是 1=1 —— 见下方增量处的说明。兜底这条更要命:
  # 它跑在增量失败之后,离取 watermark 又远了十几秒,窗口比稳态还宽。
  # 2026-09-11 实测两次(14:19 / 00:00),增量撞上竞态、回退重建**也必然**
  # 撞上同一个竞态,于是唯一的兜底在它唯一被触发的场景里 100% 失败。
  make_subset_db "$TMP" "id <= $local_max" \
    || { echo "$(ts) ERROR 构造全量副本失败"; return 1; }
  # 走与稳态同一条推送路径(同步盘为主)、同一个 applier、同一套校验 ——
  # 只是模式为 replace。以前这里内联了一份自己的远端命令,等于第二套实现,
  # 而它罕用、没人走,坏了也不会有人知道。
  for i in 1 2 3; do
    if out=$(push_payload "$TMP" "$local_max" replace); then
      echo "$local_max" > "$STAMP"
      echo "$(ts) 全量重建完成 (MAX id=$local_max) ${out#OK } [$(cat "$VIA_FILE" 2>/dev/null)]"; return 0
    fi
    case "$out" in
      *FAIL*) echo "$(ts) 全量重建被远端拒绝: $out"; return 1 ;;
    esac
    sleep 10
  done
  echo "$(ts) 全量重建失败(两条路都不通): $out"; return 1
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

# ── applier 保鲜:哈希不一致就重推(3KB,可忽略),杜绝版本漂移 ────────────
# **必须在下面任何 full_rebuild 之前** —— 兜底重建现在也经由 applier(replace
# 模式),NAS 上若还是不认识 replace 的旧版,重建会直接失败。
# The remote supervisor must be installed before the applier that calls it.
GUARD_LOCAL="$(dirname "$APPLIER_LOCAL")/sync_guard.py"
GUARD_REMOTE="$(dirname "$APPLIER_REMOTE")/sync_guard.py"
guard_sha=$(shasum -a 256 "$GUARD_LOCAL" | cut -d' ' -f1)
remote_guard=$(ssh $SSHOPT "$NAS" "sha256sum '$GUARD_REMOTE' 2>/dev/null" | cut -d' ' -f1)
if [ "$guard_sha" != "$remote_guard" ]; then
  ssh $SSHOPT "$NAS" "cat > '$GUARD_REMOTE.$$.tmp' && test \"\$(sha256sum '$GUARD_REMOTE.$$.tmp' | cut -d' ' -f1)\" = '$guard_sha' && mv -f '$GUARD_REMOTE.$$.tmp' '$GUARD_REMOTE'" < "$GUARD_LOCAL" || exit 1
fi
want=$(shasum -a 256 "$APPLIER_LOCAL" | cut -c1-16)
if [ "$nas_applier" != "$want" ]; then
  echo "$(ts) 推送 applier ($nas_applier → $want)"
  ssh $SSHOPT "$NAS" \
    "cat > '$APPLIER_REMOTE.$$.tmp' && test \"\$(sha256sum '$APPLIER_REMOTE.$$.tmp' | cut -c1-16)\" = '$want' && mv -f '$APPLIER_REMOTE.$$.tmp' '$APPLIER_REMOTE'" \
    < "$APPLIER_LOCAL" \
    || { echo "$(ts) applier 推送失败,本轮跳过"; exit 0; }
fi

# 需要全量重建的几种情况
[ -n "$FORCE_REBUILD" ] && { full_rebuild "手动 --rebuild"; exit $?; }
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

# ── 生成增量库 ─────────────────────────────────────────────────────────
# **必须有上界 $local_max**。applier 校验的是「合并后 MAX(id) 严格等于 expect」,
# 而 expect 是本轮开头那一刻读到的 $local_max。若这里只写 `id > $nas_max`,
# 取 watermark 之后新落库的行会被一并带走 → 合并后 MAX 比 expect 大 → FAIL。
# 这不是理论风险:2026-09-11 00:00 实测,读到 23163(00:00:03 写入),造增量前
# 23164 落库(00:00:21),远端判 `23164 != 23163` 直接拒收,一整轮同步作废。
# 缩短「读 watermark → 造增量」的窗口只能降低概率、压不到零;加上界才是让
# payload 的 MAX(id) **由构造恒等于** expect。超出的行留给下一轮,语义不变。
# 这样那条严格相等的校验就只在**真正的数据分叉**时报警 —— 那才是它该守的。
TMP=$(mktemp /tmp/cm-delta.XXXXXX) && rm -f "$TMP" && TMP="$TMP.db"
make_subset_db "$TMP" "id > $nas_max AND id <= $local_max" \
  || { echo "$(ts) ERROR 生成增量库失败"; exit 1; }

nrow=$(sqlite3 -readonly "$TMP" 'SELECT COUNT(*) FROM observations;' 2>/dev/null)
kb=$(gzip -c "$TMP" | wc -c | awk '{printf "%.1f", $1/1024}')

# ── 推送 + 远端合并 ────────────────────────────────────────────────────
for i in 1 2 3; do
  if out=$(push_payload "$TMP" "$local_max" merge); then
    echo "$local_max" > "$STAMP"
    echo "$(ts) synced +$nrow 条 (${kb}KB) $nas_max → $local_max"
    exit 0
  fi
  case "$out" in
    *FAIL*)
      # 远端明确判定失败(合并/校验不过) —— 重试同样的输入没意义,
      # 且线上库未被触碰,直接回退全量重建。
      echo "$(ts) 远端合并失败: $out"
      full_rebuild "增量合并失败"; exit $? ;;
  esac
  sleep 10
done
echo "$(ts) 增量推送失败(同步盘与 ssh 都不通),下个周期重试"
exit 0
