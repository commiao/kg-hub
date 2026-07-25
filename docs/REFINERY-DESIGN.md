# kg-refinery 统一知识中间件 — 设计方案(已评审,未实施)

## Context(为什么做)

kg-hub 中央图已有完整的**服务/治理端**(hybrid 检索、kind/provenance 元数据链、退休/隔离/反馈回路),但**摄入+提炼侧是三条不等价的线**,且最重要的一条已断:

| 现状断层(侦察实证) | 后果 |
|---|---|
| claude-mem 线**休眠**:db 每 15min 同步到 NAS(`/volume1/docker/kg-hub-data/claude-mem/claude-mem.db`)但**无消费者**;旧摄入器 `ingesters/claude_mem_obs.py` 直连 FalkorDB 绕过 `/api/ingest`(无幂等键/格式门/元数据打标/备份) | **4555 条 obs 积压**(2026-06-09 起),Mac 侧全部编码知识 6 周未进图 |
| OpenClaw **无会话级实时提炼**:仅每日 04:00 `kb-001` agentTurn 全天一锅烩;`session-knowledge-manager` hook 未注册、`extractSummary()` 是桩 | 会话知识延迟一天、粒度粗、prompt 约束靠自觉 |
| 不支持 claude-mem 的新工具**无接入口** | 每接一个工具重新发明一条线 |
| 治理原语齐全但**无调度**:kind 校准/backfill/胶囊合成全是手动触发 | 归纳提炼总结不成体系,low-value 39.5%、单连接 42.9% 无人系统性收敛 |

中间件(命名 **kg-refinery**,精炼层)= 补上"统一摄入 + 会话级提炼 + 定时治理"这一层。**核心原则:不另开一套**——它吸收/复活现有线,所有图写入走唯一治理通道 `/api/ingest`,治理任务编排复用 server 已有原语。

## 用户已拍板的五个决策

1. **双层接入**:claude-mem 系工具送 observation 成品;其他工具送原始会话,由 refinery 提炼
2. **实时精度 = 会话结束 + 分钟级微批**(不做逐消息流式)
3. **部署 = NAS 新容器**(同 compose,数据旁)
4. **4555 条积压全量回填**(经质量闸,约 18% 入图,夜间低峰串行烧)
5. **收窄 kb-001**:会话提炼归 refinery;kb-001 只保留文档/任务产出提炼

## 架构总览

```
Mac 工具(Claude Code/Cursor/Codex/Qoder)          OpenClaw(oc-vps)              未来新工具
  └ claude-mem hook→worker(:37701)逐动作LLM提炼    └ 会话 jsonl(append-only)      └ 任意
      └ observations 表(结构化,content_hash幂等)     └ [新]session-tailer:会话结束    │
          └ [现有]launchd 15min 同步 db 到 NAS          推送原始transcript ─────┐    │
                              │                                                │    │
════════════════ NAS(同 compose 新容器 kg-refinery)═══════════════════════════╪════╪═══
  ┌─ Level-1 成品摄入:分钟级微批读 NAS 侧 db 副本 ◀─────┘                      ▼    ▼
  │    └ ingest_filter 质量闸(复用) ─┐            ┌─ Level-0 原始会话摄入:POST /refinery/sessions
  │                                  │            │    └ 会话缓冲 → 会话结束触发 LLM 提炼
  │                                  ▼            │       (observation 形状对齐 claude-mem 六字段)
  │                        统一规范化(name/sd/source_obs_id/reference_time/origin_*)
  │                                  │
  │                                  ▼
  │              POST 127.0.0.1:8080 /api/ingest(唯一图写入通道,串行 poll-drain)
  │                    └ 现有:幂等键/格式门→隔离/备份jsonl/graphiti抽取/provenance/kind链
  │
  └─ 治理调度器(asyncio 定时,不新造原语):
       · 每晚:胶囊合成 pass(聚类近7天相关episode→CASEPACK_PROMPT 风格固定结构→入图)
       · 每晚:kind 补分类 sweep(复用 tools/backfill_schema)+ 去重/合并候选→待办⑦
       · 每晚:409 error 键巡检自动清理(补现有死锁缺口)
       · 每日 09:35 汇入现有 feedback_digest 飞书日报(精炼层段落)
       · 全部动作走 veto-after:可逆自动执行+待办可撤销;不可逆/合并类进待办等拍板
```

## 组件设计

### 1. kg-refinery 容器(docker-compose.yml 新增)
- 同镜像同网络,`depends_on: falkordb healthy + kg_hub_server`;挂载:`/volume1/docker/kg-hub-data/claude-mem`(ro)、`refinery-state`(rw,水印/配额/离线队列)、**config 用 bind-mount**(吸取 ingest_filter 烤镜像的教训,阈值/开关免 rebuild)
- 进程 = 单 asyncio 应用:HTTP 服务(Level-0 接收) + 微批消费循环(Level-1) + 治理调度器
- 对 tailnet 暴露一个新端口(如 `127.0.0.1:17173`,同 tailscale userspace 转发),Bearer 同 KG_HUB_API_TOKEN
- **单写者不变**:refinery 只 POST `/api/ingest`(loopback),复用 vps_push 的 `poll_until_done` 串行化模式;绝不直连 FalkorDB 写

### 2. Level-1 成品摄入(claude-mem 消费者 = 复活休眠线)
- 数据源:NAS 侧 db 副本(已在同步,零新客户端);分钟级微批(60-120s)按 `created_at_epoch` 增量读 `observations LEFT JOIN sdk_sessions`(照抄 `ingesters/claude_mem_obs.py:87-110` 的查询)
- 质量闸:**原样复用 `utils/ingest_filter.py`**(Layer1 硬门/Layer2 平台阈值/Layer3 配额);修补两个已知缺口:QuotaTracker 状态落 refinery-state 文件(日配额变真的)、决策日志继续写 `.ingest_decisions.jsonl`
- 规范化后 POST `/api/ingest`:`name=claude-mem-obs-<id>`(沿用现有命名)、`source_obs_id=content_hash`(表内天然幂等锚)、`sd` 携带 `type=/project=/platform=`(现有 origin 派生正则直接可用)
- **水印迁移**:导入旧 `data/.ingested.claude_mem.json`(526 ingested + 2471 rejected)为初始状态,防止重复入图
- **积压回填**(决策④):同一消费者、加 `--backlog` 节流模式(仅 23:00-07:00 跑,LLM 串行限速已有 SEMAPHORE=1+4s 间隔),预计 ~800 条入图,烧数个夜间;进度进日报

### 3. Level-0 原始会话摄入(新工具 + OpenClaw)
- **契约**(刻意最小,两个端点):
  - `POST /refinery/sessions/events`:`{session_id, tool, project?, seq, events:[{role, content, ts, tool_name?}]}` — 增量追加,幂等键 `(session_id, seq)`
  - `POST /refinery/sessions/close`:`{session_id}` — 显式收口;另有 30min 无事件自动视为结束(对齐现有"中断>30min"惯例)
  - 缓冲落盘(refinery-state jsonl),重启不丢
- **会话结束触发提炼**:LLM(同 `_llm_complete` 配置,qwen3.6-plus 百炼)按 claude-mem 的 observation 骨架(type/title/facts/narrative/concepts 六字段,prompt 形状对齐 `plugin src/sdk/prompts.ts`,产出走同一质量闸+同一 `/api/ingest` 规范化。**一个会话产出 1-N 条 observation + 1 条 summary**,不逐消息
- **OpenClaw 适配器**(VPS 侧一个小 tailer,标准库,形状照抄 `vps_push_capsules.py` 的水印/重试纪律):tail `~/.openclaw/agents/main/sessions/*.jsonl`(处理 `.reset.` 改名+行 offset 续读),会话结束(读到新 session 首行/文件轮转/30min 静默)即把该会话 transcript 推给 `/refinery/sessions/*`;`user/assistant/toolResult` 三种 role 直接映射契约
- **kb-001 收窄**(决策⑤):prompt 改为只从"新增文档和任务产出"提炼,删去"对话记录"源;会话知识由 refinery 会话级提炼接管。过渡期靠 `/api/ingest` 幂等 + 图侧去重兜底

### 4. 治理引擎(归纳/提炼/总结/胶囊——只做调度和一个新 pass,原语全复用)
| 能力 | 实现 | 复用 |
|---|---|---|
| 归纳(分类) | 摄入时 kind/provenance/origin 链(已有);每晚 sweep 补 unclassified | `_tag_schema_fields`、`tools/backfill_schema.py`、`utils/origin.py` |
| 提炼 | Level-0 会话→observation(新);Mac 侧保持 worker 逐动作提炼不动 | prompt 形状对齐 claude-mem `src/sdk/prompts.ts` |
| 总结/胶囊 | **每晚胶囊合成 pass(唯一新治理逻辑)**:近 7 天未入包 episode 按实体重叠+语义聚类,簇≥3 条即走固定结构合成"知识胶囊",`name=refined-capsule-*`,`sources` 溯源,provenance 继承最保守值;每晚上限 N 簇(LLM 预算) | `CASEPACK_PROMPT`/`dashboard_casepack` 流程、`/api/search` 语义腿 |
| 去重/退休 | 每晚列高相似候选对→**待办⑦「合并候选」**(合并不可逆,留人);409 error 键>24h 自动清理重试一次 | 退休回路、隔离回路、veto-after 规则 |
| 反馈闭环 | 不动,kg-use→knowledge_feedback 已运转 | 现有 |
- 全部动作按既有 veto-after 红线:conflict/verified/canonical/合并类留人,其余可逆自动+日报+撤销

### 5. 可观测性
- 门户新卡「精炼层」:两级摄入吞吐/积压烧进度/隔离与 409 计数/每晚治理产出;`PORTAL_REPORTS` 加一条 + `/dashboard/refinery`(照抄现有看板模式)
- watchdog 增加 refinery 健康探针(90s 轮已有);日报并入 `tools/feedback_digest.py`

## 复用清单(不重写)
- `kg_hub_server.py`:`/api/ingest` 全套、`_llm_complete`、`CASEPACK_PROMPT`、待办面板模式
- `utils/`:`ingest_filter.py`、`origin.py`、`provenance.py`、`writer_lock.py`
- `tools/`:`backfill_schema.py`、`feedback_digest.py`、`vps_push_capsules.py`(tailer 的纪律模板)、`sync_claude_mem_to_nas.sh`(保持不动)
- claude-mem 侧:六动词 hook 体系、worker 提炼、`content_hash` 幂等——**全部不动**
- 退役清单(实施时):`ingesters/claude_mem_obs.py`(被 Level-1 取代,归档不删)、我遗留的死代码 `sync_openclaw.py`/`tools/tag_provenance.py`

## 分阶段落地(每阶段独立可回滚)
- **Phase A — 复活 claude-mem 线**(最大即时价值,零新客户端):refinery 容器 + Level-1 消费者 + 水印迁移 + 积压夜间回填 + 门户卡。回滚 = 停容器,线退回休眠现状
- **Phase B — Level-0 + OpenClaw 会话适配**:契约端点 + VPS tailer + kb-001 收窄。回滚 = 停 tailer、kb-001 prompt 还原
- **Phase C — 夜间治理**:胶囊合成 pass + kind sweep + 合并候选待办⑦ + 409 巡检。回滚 = 关调度器开关(bind-mount config,免 rebuild)
- **Phase D(可选)**:db 同步 15min→5min、Mac 侧 SSE 直推、新工具接入文档(ONBOARDING 加"路径 C: refinery")

## 明确不做
- 不做逐消息流式(决策②);不动 Mac 侧 claude-mem worker 提炼;不直连 FalkorDB 写;不新建第二套质量闸/分类器/prompt;检索/注入/看板仍归 kg_hub_server;evergreen 自动退休仍不做(弱信号)

## 风险与协调
1. **另一 kg-hub 会话地盘重叠最高**(ingest/治理②/compose 都是他们刚收拢的):实施前必须 git log+漂移检查+读最新 northstar,理想是把本方案文档 commit 进 `docs/REFINERY-DESIGN.md` 让两会话共识后再动工
2. LLM 预算:回填+夜间治理都吃百炼额度,全部限速串行+夜间窗口+每日上限,进日报可见
3. 质量闸误杀(historic 18% 接纳率):决策日志留全量,shadow 指标进精炼层看板,阈值 bind-mount 可调
4. OpenClaw tailer 读 sessions.json/jsonl 含敏感内容:提炼后原文不出 VPS 之外只进 refinery 缓冲,缓冲文件 0600 + 提炼完成即清

## 验证方式(实施时)
- Phase A:积压烧完后 `quality_audit` 对比(episodes 应 +~800);`/api/queue_stats` 无 stuck;门户卡吞吐曲线;kg-use 搜一条 6 月的 muxcp 知识应命中
- Phase B:OpenClaw 里跑一个真实会话→结束后 ≤5min 图里可搜到该会话 observation;kb-001 次日胶囊不再含会话源
- Phase C:连续 3 晚日报有治理段落;胶囊合成产物在整理台可见且 sources 可溯;合并候选出现在待办⑦且可撤销
