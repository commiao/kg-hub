# 换向量模型操作单（T-0339）

> 背景与评测：`docs/EMBEDDING-MODEL-SWITCH-PLAN.md`。本单只讲怎么做、怎么验、怎么退。
> 目标：把向量模型从 `BAAI/bge-small-en-v1.5` 原地换成 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（同为 384 维，HNSW 索引不动）。

## 机制（代码已就位，缺省行为不变）

- 运行时模型只由 `KG_HUB_EMBED_MODEL` 决定（`utils/embedding_profile.py`），缺省是现状模型；未登记的模型名直接报错。
- 图里向量出自哪个模型，记在唯一的 `(:EmbeddingProfile {key:'active'})` 节点上；没有该节点 = 现状模型。
- `build_graphiti()` 建客户端前比对两者，**不一致就拒绝启动**。服务、文档入图、周报都经过它。
- 实体消解门槛 `NODE_MATCH_MIN` 随模型登记在档案里：bge-small-en 0.95，MiniLM **0.89**。
  校准口径：全图 5,651 对词面相同的实体对，按线上规则（分数过门槛且在前 5 候选内）统计复用成功率，现状 42.8%，MiniLM@0.89 为 42.9%；沿用 0.95 只有 16.7%。
  顺带发现现状 0.95 本身偏严（0.90 时 71.8%），属另一项优化，本次不动。
- `/health` 多了 `embed_model`，切换后据此确认生效。
- `tools/reembed.py`：本机算、分批写回；写图前进入排空态、等在飞归零，每批续期，写完才改标记，最后解除排空；
  默认只在 23:00–01:45（北京时间）允许写图，避开白天入图与 02:00 路由器重启。

## 步骤

### 0. 前置

1. 本 PR 合并并按部署准则发布。缺省模型不变，`/health` 应显示 `embed_model: BAAI/bge-small-en-v1.5`。
2. 断路器开着（入图为 0）或在 task-hub 确认这段时间没人入图、没人发布。

### 1. 白天只读准备（本机）

```bash
python -m tools.reembed plan
python -m tools.reembed backup
python -m tools.reembed compute --cache-dir <本机 fastembed 缓存目录>
```

`backup` 把线上现有向量原样导出（回退用，约 350 MB）；`compute` 本机约 50 分钟。

### 2. 把新模型放进 NAS 的离线模型目录

容器强制离线（`HF_HUB_OFFLINE=1`），新模型必须预先放进 `${KG_HUB_DATA_ROOT}/models`：
把本机 fastembed 缓存里的 `models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q` 整个目录拷过去。
拷完用现有镜像起一个断网临时容器，`local_files_only=True` 加载一次确认能读到。

### 3. 夜间写回（23:00–01:45）

```bash
python -m tools.reembed write --yes
python -m tools.reembed verify
```

写回约 23 万条，按 500 条一批、批间 0.2 秒，预计 10–20 分钟。期间线上处于排空态，入图写入会被拒收并由调用方自动重试。
`verify` 抽样 1000 条读回，与本机检查点余弦须 ≥ 0.999。

### 4. 立刻切配置并重启

在 NAS 的 `.env` 写入 `KG_HUB_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，
按部署准则重新发布当前标签（让 `kg_hub_server`、`ingester`、`weekly_report` 带新环境变量重建）。
**第 3 步结束到这一步之间**，正在跑的旧进程仍用旧模型算查询向量，语义检索会暂时变差，所以两步要连着做。

确认：`/health` 的 `embed_model` 是新模型；服务正常起来（不一致会拒绝启动，日志里有 `EmbeddingProfileError`）。

### 5. 验收

- `python -m tools.eval_recall --grep s1-mem --by-name`：记忆集不退步，重点看带英文标识符的 literal 问法（离线 4/5 → 2/5，看子串腿能否补回）。
- 跨语言 100 条线上复测：中文提问 top-10 应接近离线的 31（现状 8）。
- 入图冒烟：`/api/ingest_extracted` 的 `dry_run` 跑 50 条，看实体复用率与误并（对照 `extracted_ingest` 注释里的历史误并对）。

## 回退

```bash
python -m tools.reembed restore --expect-from sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 --yes
```

写回备份向量并把标记改回，再把 `.env` 的 `KG_HUB_EMBED_MODEL` 删掉或改回、重新发布。
顺序同样要连着做：标记与配置任一没跟上，服务都会拒绝启动——这是有意的。
