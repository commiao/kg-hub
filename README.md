# kg-hub — 个人 AI 中央知识图谱

> **维护者**：jingmiao@liblib.ai（Lovart）
> **起始**：2026-05-14
> **状态**：📝 规划阶段（未开工）
> **父项目**：claude-mem（`/Users/mac/workspace_claudeCode/claude-mem`）

## 一句话

把分散在多设备 / 多 IDE / 多 project 上的 claude-mem 本地 observations，**汇聚成一个中央知识图谱**，让所有 AI 工具通过 MCP 做跨设备跨项目的 RAG。

## 为什么独立成项目

claude-mem 已经把单机/单工具的"工具调用 → 结构化观察"做好了，但它**本质是单机方案**：每台设备一份 SQLite，互不相通。要解决"我的工程记忆跨设备聚合 + 实体关系导航"这个问题，需要在 claude-mem **之上加一层**，且这一层**完全可以独立演进**——它跟 claude-mem 的耦合点只有两个：
- **input**：读 claude-mem 的 SQLite `observations` 表
- **output**：暴露 MCP 接口给所有 IDE（含本机和远程）

所以解耦合做成独立项目。

## 与 claude-mem 的关系

```
┌──────────────────────────────────────────────────────────┐
│ claude-mem (各设备本地)                                   │
│   每台 Mac/NAS 各自一份                                   │
│   写：hook 自动捕获工具调用 → qwen 生成 obs → SQLite       │
│   读：MCP 工具集（search / timeline / get_observations）  │
└──────────────────────┬───────────────────────────────────┘
                       │ (kg-hub 读)
                       ▼
┌──────────────────────────────────────────────────────────┐
│ kg-hub (本项目)                                           │
│   中央 KG (Memgraph)，always-on                           │
│   接收各设备 push                                          │
│   暴露 MCP RAG 接口                                        │
└──────────────────────────────────────────────────────────┘
                       ▲
                       │ (任意 IDE 通过 MCP)
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
   Claude Code      Cursor          Codex 桌面版
```

## 目录结构

```
kg-hub/
├── README.md          ← 本文件（项目入口、当前状态）
├── DESIGN.md          ← 架构与已做决策（不要重新讨论已决定的事）
├── ROADMAP.md         ← 4 个 Phase 的具体任务清单
└── SESSION-PROMPT.md  ← 新会话开干时直接复制丢给 AI 的 prompt
```

## 当前阶段

✅ **Phase 2 完成（2026-05-17）**：自动化 launchd + 双数据源 + 跨进程 writer 锁。当前 KG 已含 **468 entities / 656 edges / 114 episodes**（仍在 backfill 中，全量完会到 ~3000 entities）。

最近里程碑（时间倒序）：
1. **2026-05-17** Phase 2 收口：
   - 两个 plist 上线（claude-mem 每 15 min + openclaw 每 30 min）
   - `writer_lock` 串行化所有本机 writer（防 entity-dedup race）
   - claude-mem 660 obs 历史 backfill 启动（~5h 后完成）
   - 全量数据 audit：UPPER/lower 边重复 12 对 / File 类型滥用 35.7% / 58.4% 边名 LLM 自创——留待 cleanup
2. **2026-05-17** Kuzu → FalkorDB 迁移：
   - 原因：Kuzu embedded 单写者锁与 Phase 2 自动化定时同步互斥
   - 路径：本机 Docker 开发 → 验证通过 → ACR → NAS（部署 pending）
   - graphiti_client/mcp_server/ingester 全部切换；237 → 235 数据保留率 99%（LLM 重抽差异）
3. **2026-05-15** Phase 1 收口：237/245/31 在 Kuzu，5 个 MCP 工具新会话验证通过
4. **2026-05-14** SPIKE 验证 Graphiti → 决策 9 / 10 / schema v0.2

下一步候选：
- **Phase 3 设计**：OpenClaw 主动 push (`/api/ingest` + idempotency key) / NAS 部署
- **Cleanup**：UPPER/lower 边合并 + File 类型重分类 + 25→56 unclassified 收口
- 详见 [ROADMAP.md](ROADMAP.md)

## 关键决策已锁定（详见 DESIGN.md，新会话不要重开讨论）

| 决策 | 选择 |
|---|---|
| 架构模式 | Local-First + Central Sync（设备本地写 + 异步推中央） |
| 中央存储 | Memgraph（社区版 Docker） |
| 部署位置 | NAS 或 always-on 小机器，走 Tailscale 内网 |
| 实体抽取 LLM | qwen3.6-plus（复用 claude-mem 的百炼 coding plan） |
| 客户端接入 | MCP（与 claude-mem 同协议） |
| 设备端持久化 | 复用 claude-mem 的 SQLite，本项目只加读取 + push 逻辑 |
| 优先级 | 先做中央，本地 KG 副本（Phase 3）按需后做 |
| **Phase 0 数据源** 🆕 | **OpenClaw 胶囊 + 知识库导出**（不用 claude-mem obs） |

## 不在本项目范围内（明确划线）

- ❌ 不修改 claude-mem 本身（worker / hook / .env / plist 都不动）
- ❌ 不替代 claude-mem 的"自动工具调用捕获"（那是 claude-mem 的事）
- ❌ 不解决 Cursor / Codex 的写记忆缺失（那是上游问题）
- ❌ 不做对话内存（用户输入历史）

## 风险提示

- **数据敏感性**：观察里含路径 / 内部架构 / token 痕迹 → 中央必须严格内网或加密
- **冷启动**：父项目 claude-mem 现在只有 150 obs，建图谱 PoC 够，规模化要等数据涨
- **Schema 演进**：KG schema 设错半年要推倒重来，前期要慢
- **不要陷入完美主义**：实体消解、schema 设计都不可能一次性完美，Phase 路线就是"先跑起来再迭代"

## 故障排查 / 运维提示

按"症状 → 原因 → 处置"组织，半夜被叫醒时照着做。

### MCP 调用全部失败，错误含 `ONNXRuntimeError NO_SUCHFILE ... model_optimized.onnx`

- **原因**：fastembed 默认把嵌入模型缓存在 `/var/folders/sz/.../T/fastembed_cache/`，这是 macOS NSTemporaryDirectory，**系统会周期性清理**（典型 3 天）。模型一被清，kg_hub_server 的 Graphiti 单例就读不到 `.onnx`。
- **临时修复**：用项目 venv 重下模型
  ```bash
  /Users/mac/workspace_claudeCode/kg-hub/spike-graphiti/.venv/bin/python -c "
  from huggingface_hub import hf_hub_download
  hf_hub_download(repo_id='qdrant/bge-small-en-v1.5-onnx-q', filename='model_optimized.onnx')
  "
  ```
  ⚠️ **不要用 `curl` 直连**——HuggingFace 已迁到 Xet CAS (`cas-bridge.xethub.hf.co`)，macOS 系统 curl 用的 LibreSSL 跟它握不上手，必须走 Python `huggingface_hub` 库（原生支持 Xet）。
- **长期修复**：把 fastembed 缓存改到不会被清的目录
  ```bash
  # 在 LaunchAgent plist 的 EnvironmentVariables 里加：
  FASTEMBED_CACHE_PATH=/Users/mac/.cache/fastembed
  ```

### kg_hub_server 端口监听但所有请求卡死（含 `/health`）

- **症状**：`lsof -nP -iTCP:8080` 显示 `*:8080 LISTEN`，但 `curl /health` 30 秒无响应；`ps` 看 CPU 时间几乎不动。
- **原因**：服务在启动时如果 fastembed 模型缺失（见上一条），`get_graphiti()` 单例进入损坏状态——端口可监听但 Graphiti 用不了。SIGTERM 不响应。
- **处置**：
  ```bash
  kill -9 $(pgrep -f kg_hub_server.py)
  # com.kg-hub.server.plist 已设 KeepAlive，30s 内自动重启
  # 等到 curl http://127.0.0.1:8080/health 返回 200 即恢复
  ```

### `/api/ingest` 返回 `writer.lock timeout after 180s`

- **原因**：后台 ingester（`com.kg-hub.claude-mem-ingest.plist`，每 15 分钟跑一次）持锁时间长（10 episodes × ~200s = 33 分钟），server 自身的写入排队超过 180s 上限就失败。该状态会以 `errored` 记入 IngestedKey，**同一 `source_obs_id` 重投会被幂等键拒绝**。
- **处置**：
  - 临时绕过：换一个 `source_obs_id` 重投
  - 检查是不是有 ingester 卡得过久：`lsof ~/.kg-hub/locks/writer.lock`
  - 设计上的根因见 `DESIGN.md` §6 "已接受的限制"

### 查 KG 拿不到结果

- **优先用 `kg_search`（边事实语义搜索）**，不要用 `kg_episode_search`（原始 markdown 全文）。后者对中英混合关键词召回差。
- `kg_search` 走 embedding，能跨语言匹配；`kg_episode_search` 走全文倒排，对分词敏感。
