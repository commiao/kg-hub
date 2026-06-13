---

# kg-hub 事件复盘 + 最终架构(归档)

> 起因:`kg-hub-falkordb` 容器 CPU 100%、Mac 能耗几乎全压在它上。
> 结果:根因根治 + 数据全恢复 + 迁到常开 NAS + 双路持续摄入 + 三层监控自治。

## 一、根因

**① CPU 跑满(性能根因)**
Graphiti 的**边去重**用 `EDGE_HYBRID_SEARCH_RRF`(bm25 全文 + 向量),每条抽取边触发**两次**混合搜索,其中"失效候选"那次**全表无过滤**。FalkorDB 的 **bm25 全文查询很慢**(2000+ 节点时单条 2–14s,随图增长恶化),且当时**无查询超时** → 查询堆积、CPU 跑满。慢查询日志实锤:慢的全是 `(term|term)` 全文查询,向量查询无一上榜。

**② 数据丢失(持久化根因)**
排查中发现 bind mount **挂错路径**:挂到容器 `/data`,而 FalkorDB 实际写 `/var/lib/falkordb/data`(镜像 `run.sh` 写死 `--dir`)。即数据**从未真正落到宿主机**,只活在容器可写层。停删容器 → 图丢失。
**关键缓解**:图是**派生数据**——真源 `~/.claude-mem/claude-mem.db`(4632 obs)完好,可重建。

## 二、关键修复

| 修复 | 内容 |
|---|---|
| 止血 | `GRAPH.CONFIG SET TIMEOUT_DEFAULT/MAX`,先掐掉跑飞查询 |
| 持久化加固 | 正确挂载 `→/var/lib/falkordb/data` + RDB + AOF(appendfsync everysec) |
| **性能根治** | `graphiti_client.py` monkeypatch:边去重改**纯向量(cosine-only)**,丢掉慢的 bm25 → **14s→43ms**,CPU 0.6% |
| 配额保护 | `SEMAPHORE_LIMIT=1`(串行)+ 4s 最小调用间隔,避免百炼并发 429 |
| 迁移期 2 个真 bug | ①健康检查 `$FALKORDB_PASSWORD` 未注入容器;②`wait_for_falkordb` 写死 `127.0.0.1`(容器里连不到服务名) |
| fastembed 离线 | NAS 容器连不上 HuggingFace(`EADDRNOTAVAIL`)→ 把 bge-small 模型拷到 NAS `/models` + `HF_HUB_OFFLINE=1` |
| canonical bug | `skip_extraction=True` 与 graphiti 0.29 不兼容 → 去掉,改完整抽取 |

## 三、重建结果
- claude-mem **2161** obs + openclaw **23/24** 胶囊 + canonical **5** 文档。
- 去重后 **2189 唯一 episode**,~7400 节点。
- 不可恢复的仅:`kg_add_episode` 直接添加的无源正文(已加 ingest 落盘备份防再丢)。

## 四、最终架构(轻重分离 + 自治)

```
真源                          常开 NAS(群晖 DS920+, always-on)
─────                         ──────────────────────────────────
claude-mem.db (Mac) ──15min同步──┐   ┌─ falkordb   (图; RDB+AOF; 查询超时; 仅绑127.0.0.1)
OpenClaw clawd (VPS) ─1h同步────┤──▶│  kg_hub_server(:8080 HTTP API; 仅Tailscale可达)
                                 │   │  ingester    (持续摄入; 向量-only; 模型本地离线)
                                 │   └─ watchdog    (内部哨兵, sidecar)
                                 │
客户端/监控                       │
─────────                        ▼
MCP (Mac) ──HTTP──────────────▶ NAS
VPS 探针(check.sh) ─/health─▶ NAS  ┐
VPS 进度(progress.sh) ───────▶     ├─▶ 飞书(webhook): 宕机/恢复告警 + 每20min进度
MCP 客户端预警(连不上时) ─────▶     ┘
```

**三层监控**:L1 NAS sidecar watchdog(内部细粒度)· L2 VPS 探针(异地常开,整体挂也能报)· L3 MCP 客户端预警(用时连不上)。配置热读(`notify.json`),改规则免重建。

## 五、经验教训
1. **监控者不能与被监控者同生共死** → 关键探针放 VPS(异地常开),不放被监控的项目里。
2. **持久化要验证落点**(`CONFIG GET dir`),别假设挂载生效——本次数据丢失正因挂错路径未察觉。
3. **派生数据 vs 真源**:保住真源(claude-mem.db / VPS clawd)+ 可复现管线 = 真正的数据安全网。
4. **轻重分离**:重的(图/摄入/LLM)放 NAS;轻的(健康探针)是 cron+curl,放 VPS,几乎零成本——监控≠部署服务。
5. **别混用 CLI `docker compose` 与 Container Manager 管理同一项目**(会让 CM UI 与实际容器脱同步,出现"幽灵容器")。
6. **Mac↔NAS tailscale 走 relay 会偶发抖动**:用粗粒度 HTTP + 重试 + 外部视角交叉验证来容忍,别让单点直连(如直连 falkordb)暴露在抖动下。
---

## 切流量后链路体检与修复(2026-06-12)

NAS 切换(约 06-09)后发现两条链路**静默中断了 5 天**——SSH/HTTP 都通,极具迷惑性。体检 + 根因 + 修复如下。

### 修复 1:claude-mem.db 同步中断(Mac→NAS)
- **症状**:NAS 上 `claude-mem.db` 冻结在 `06-09T20:35`;launchd 日志一直 `sync failed (NAS unreachable)`,但 `ssh` 到 NAS 明明是通的。
- **根因**:macOS 新版 `scp` 默认走 **SFTP 子系统**,而群晖 sshd 未启用该子系统 → `subsystem request failed on channel 0`。SSH(exec)能用、SFTP(scp)不能,造成"看起来网络没问题却传不过去"。
- **修复**:`tools/sync_claude_mem_to_nas.sh` 把 `scp` 换成 `cat | ssh "cat > tmp && mv -f tmp dst"` 管道(经临时文件原子 mv,避免 ingester 读到半截库),与 openclaw 同步同款思路。已追平。

### 修复 2:usage_count 知识使用量统计中断(HTTP 化彻底修)
- **症状**:`usage_count` 冻结在 `06-09`,2190 个 episode 仅 10 个有计数;SessionStart 检索成功(有 `OK`)却无 `bumped` 日志。
- **根因**:自增只由 Claude Code SessionStart 的 `kg_push_hook.py` 做,且是**直连 NAS FalkorDB 写**。切 NAS 后走 tailscale:检索读涨到 ~3.6s(逼近 5s 超时),而 bump 用的是 chip 输出后才跑的 **1s 快失败连接** → 被超时/进程回收**静默跳过**。
- **修复(HTTP 化,根治读+写两个问题)**:
  1. **server** 新增 `GET /api/canonical_context?kw=&top_n=&bump=1`:两遍检索(canonical CONTAINS + Episodic 全文)+ rank(canonical 优先)+ **服务端自增 usage_count/last_used_at**,全部在 NAS localhost FalkorDB 上完成(server↔db 本地直连,可靠)。
  2. **push hook** 改为**纯 HTTP 一次调用**(`urllib`,去掉直连 FalkorDB):读 + bump 一次往返。
- **效果**:hook 耗时 **3.6s → 0.07s**;bump 100% 可靠并验证落库(DESIGN usage_count 20→21,`last_used_at` 更新为当日);hook 不再依赖 `falkordb` 模块,跨网络只剩一个可容忍的 HTTP 往返。
- **部署**:NAS `kg-hub-server:latest` 镜像重建(`docker compose -p kg-hub build/up`,复用 CM 项目名,UI 未脱同步)。

### 一条经验
> **跨设备链路"看起来通"≠真的通**:SSH 通不代表 scp 通(SFTP 子系统)、HTTP 读通不代表写也跟得上(超时预算)。迁移后要对**每一条**读/写/同步链路做端到端验证,而不是只 ping 一下主机。原则上,跨网络的写操作应收敛到与数据同机的服务端(localhost),客户端只留一个可容忍的 HTTP 往返。
---

## 能耗复发 + 记忆断流排查(2026-06-13)

起因:发现 kg-hub 一直没有「新增」通知。顺藤摸瓜,确认 **kg-hub 本身健康**(库 4632 obs 全部已评估、无遗漏),问题在 **上游 claude-mem 断了两处**——且其中一处正是当初触发整个事件的同类能耗问题,复发了。

### 问题 1:Mac 能耗复发 —— claude-mem worker 空转(issue #2188)
- **现象**:4 个 `claude-mem/13.2.0 hook codex ...` 进程(PPID=1 孤儿),自 06-04 起各 ~90% CPU **空转 9 天**(累计 CPU 6400+ 分钟/个),是 Mac 当前发热耗电主因。
- **根因**:claude-mem 的 `bun-runner` 收到 **空 stdin 负载(0 字节,issue #2188)** 后进入 CPU 死循环;采样确认死钉在主线程紧密循环、无阻塞系统调用。`CAPTURE_BROKEN` 标记与 `runner-errors.log` 实锤。
- **处理**:清除全部 4 个孤儿(保留正常常驻 daemon)→ ~270%+ CPU 空转立即停止。

### 问题 2:06-01 起零新记忆 —— 生成器找不到 claude
- **现象**:`observations` 表停在 `2026-06-01T13:08`(id=4639),12 天无新 obs;库被各工具同步触碰(文件 SHA 变)但 obs 行数恒为 4632 → 自然没有新数据流入 kg-hub。
- **根因**:claude-mem 观察生成器 `provider=claude`,日志反复报 `Generator failed: Claude executable not found`。06-01 重启的 daemon 在最小 PATH 下找不到 `claude`(实际在 `~/.npm-global/bin/claude`)。
- **处理**:`~/.claude-mem/settings.json` 补 `CLAUDE_CODE_PATH=/Users/mac/.npm-global/bin/claude`(已备份)→ 杀旧 daemon、launchd 带新配置干净重生(新 PID,日志无报错)。

### 新增防线:claude-mem 空转守护
- `tools/claude_mem_guard.sh` + launchd `com.kg-hub.claude-mem-guard`(每 300s)。
- 判定:命令含 `claude-mem` 且含 `hook` 的进程,**累计 CPU 时间 > 120s** 即判定空转(正常 hook < 10s),清理 + 飞书提醒;平时静默。
- 用「累计 CPU 时间」而非瞬时 %CPU:躲开 macOS `%cpu` 是生命周期均值的坑,也不误杀正在跑 LLM 的合法 hook;**常驻 daemon(无 hook 字样)永不触碰**。
- 意义:把「悄悄烧 9 天才被发现」变成「5 分钟内自动清理 + 主动告警」。

### 因果链与经验
> Codex 插件 worker 06-04 卡死空转(#2188)+ 生成器 06-01 找不到 claude → claude-mem 停产新记忆 → kg-hub 无新数据可摄入 → 无「新增」通知。**kg-hub 这一环始终健康,是上游记忆生产断了。**
>
> **经验**:① 监控要覆盖「依赖链上游」——下游再健康,上游断了一样没产出,且"没通知"会被误读成"没问题"。② 后台 worker / hook 类进程要有**空转兜底守护**(按累计 CPU 时间判定),否则孤儿进程能无声烧上几天。③ 派生数据管线(claude-mem → kg-hub)任一环断了都会表现为"下游静默",排查要从"有没有新源数据"这个最上游问起。

---

## ingest 写锁竞争改进(2026-06-13)

- **现象**:一次性并发 POST 多条 `/api/ingest` 时,首篇大文档独占 `writer.lock` 做抽取(串行 LLM + 4s 限流,可达数分钟),其余在等锁 180s 后 `WriterLockBusy` → 被直接判 `error` **丢弃**(实测 5 条只成 1 条)。
- **根因**:`do_extract` 对锁超时是"一次性失败即丢",没有重试;而抽取吞吐受限流约束,锁持有时间天然可能超过等待方的超时窗口。
- **修复**(`kg_hub_server.py`):锁获取失败不再立即判错,改为**线性退避重试**(`KG_HUB_INGEST_LOCK_RETRIES` 默认 5 次、`_BACKOFF_SEC` 默认 5s、`_TIMEOUT_SEC` 默认 180s),多次仍拿不到才判错。并发批量写由此**自动串行化排队**,不再静默丢失。
- **经验**:有限流的串行写管线,锁争用要靠"重试/排队"消化,而不是"超时即丢"——否则高峰批量写会悄悄掉数据。
