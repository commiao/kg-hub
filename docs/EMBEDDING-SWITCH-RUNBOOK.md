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
- **全量写回向量会让 `RELATES_TO.fact_embedding` 的 HNSW 索引积累墓碑并漏召回**（`maxDocId` 远大于
  `numDocuments`），所以切换和回退写完向量后都必须重建该索引并验收，见下方第 5 步。
  依据与实测在 `docs/falkor-vector-index-rollout.md`「Semantic search」与「Rebuild runbook」。

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

`backup` 把线上现有向量原样导出（约 350 MB）；`compute` 本机约 50 分钟。
`backup` 只覆盖导出那一刻已有的条目：它之后入图的条目不在里面，回退时 `restore` 管不到它们（见「回退」）。

### 2. 把新模型放进 NAS 的离线模型目录

容器强制离线（`HF_HUB_OFFLINE=1`），新模型必须预先放进 `${KG_HUB_DATA_ROOT}/models`：
把本机 fastembed 缓存里的 `models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q` 整个目录拷过去。
拷完用现有镜像起一个断网临时容器，`local_files_only=True` 加载一次确认能读到。

### 3. 夜间写回（23:00–01:45）

```bash
python -m tools.reembed backup
python -m tools.reembed write --yes
python -m tools.reembed verify
```

`write` 前紧贴着再 `backup` 一次（只读，几分钟）：备份与写回之间入图的条目越少，将来 `restore` 越有可能可用；
有入图也不要紧，`restore` 会拒绝，回退改走 `compute` + `write`。

写回约 23 万条，按 500 条一批、批间 0.2 秒，预计 10–20 分钟。期间线上处于排空态，入图写入会被拒收并由调用方自动重试。
`verify` 抽样 1000 条读回，与本机检查点余弦须 ≥ 0.999。

### 4. 立刻切配置并重启

在 NAS 的 `.env` 写入 `KG_HUB_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，
按部署准则重新发布当前标签（让 `kg_hub_server`、`ingester`、`weekly_report` 带新环境变量重建）。
**第 3 步结束到这一步之间**，正在跑的旧进程仍用旧模型算查询向量，语义检索会暂时变差，所以两步要连着做。

确认：`/health` 的 `embed_model` 是新模型；服务正常起来（不一致会拒绝启动，日志里有 `EmbeddingProfileError`）。

### 5. 重建 RELATES_TO 向量索引并验收索引

第 3 步把全部 `fact_embedding` 原地重写了一遍，HNSW 索引里留下大量墓碑；不重建，语义检索和入图的边相似度查询
都会漏掉本该靠前的边，且加大候选数也找不回。第 4 步之后紧接着做，同一夜内完成；在重建完成前，语义检索的质量是降级的。

按 `docs/falkor-vector-index-rollout.md` 的「Rebuild runbook」逐步做（选窗口、查 NAS 温度、`DROP`/`CREATE`、等 `OPERATIONAL`、验收），
这里只补换模型场景下要注意的三点：

- 重建期间索引只建了一部分、查询却照常作答，所以要让语义检索和入图的边相似度查询都别走索引：
  `KG_HUB_SEARCH_VECTOR_INDEX` 为 0（或没人在搜），入图停着（断路器开着，或 `KG_HUB_EDGE_VECTOR_INDEX_PERCENT=0`），
  且没有 `/api/ingest_extracted` 的轮次在飞。`DROP` 会让整个 `RELATES_TO` 索引（含 `fact` 全文索引）一起重建，
  期间 `/api/search` 的标识符腿、字面腿可能只返回部分结果。
- `CREATE` 的 `dimension` 要等于目标模型的维度（`utils/embedding_profile` 里登记的 `dim`）；本次两个模型同为 384，选项不变。
- `python -m tools.vector_index_check` 用**本机环境变量 `KG_HUB_EMBED_MODEL`** 决定查询向量的模型，没设就是缺省模型。
  切换后验收要先在运行它的 shell 里 `export KG_HUB_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，
  否则用旧模型的查询向量去比新模型的库向量，会报一片「不符」；回退后验收则必须不设（缺省就是回退目标）。

验收标准：

```bash
python -m tools.vector_index_check --stats    # 等到退出码 0（OPERATIONAL）；ratio 回到约 1.0
python -m tools.vector_index_check            # NAS 降到 85 °C 以下再跑，必须打印 8/8 条一致
```

`--stats` 里的 `ratio`（= `maxDocId / numDocuments`）也是 `/api/queue_stats.edge_vector_index.ratio`，
重建完应约 1.0（上一次重建后实测 1.000，之后随入图缓慢上升）。写回后、重建前它在 2 以上（上次换模型后实测 2.26）；
超过 1.3 时 NAS 巡检会报 `vector_index_dirty`，重建后巡检自动恢复。比例没回到约 1.0 或验收不是 8/8，
都视为切换没做完：不要宣布完成，也不要放开入图和索引检索，按 Rebuild runbook 的第 4 步排查（温度、重建是否真的完成）后重做。

验收通过、比例回落后，再把入图和 `KG_HUB_SEARCH_VECTOR_INDEX` 恢复到原状（恢复断路器是操作者的决定）。

### 6. 验收检索效果

- `python -m tools.eval_recall --grep s1-mem --by-name`：记忆集不退步，重点看带英文标识符的 literal 问法（离线 4/5 → 2/5，看子串腿能否补回）。
- 跨语言 100 条线上复测：中文提问 top-10 应接近离线的 31（现状 8）。
- 入图冒烟：`/api/ingest_extracted` 的 `dry_run` 跑 50 条，看实体复用率与误并（对照 `extracted_ingest` 注释里的历史误并对）。

## 回退

### 先判断走哪条路

切换之后图是会继续长的：入图在跑，新增的实体和边**只有切换后模型的向量**。`reembed restore` 只把备份里的旧向量写回、
再把标记改回旧模型，覆盖不到备份之后新增的条目——它们会留着新模型的向量，却被标记成旧模型，
查询向量与库向量静默错位，没有任何报错。所以：

- **默认走下面的「回退 A：compute + write」**。2026-10-22 核实，切换后图已从 57,073 实体 / 172,188 边长到 61,578 / 181,194，
  多出的约 4,500 实体、9,000 边备份里都没有，`restore` 不够用。
- `restore` 只在「备份之后图里没有任何新增的带向量条目」时可用（例如切换当夜刚写完、入图一直停着就要回退）。
  这个前提由 `restore` 自己检查：线上每条带向量的实体和边，备份里都必须有；有一条没有就**拒绝执行**并列出数量与样例
  uuid，不提供越过开关，在排空线上之前就会拦下，排空等静后还会再查一遍。

### 回退 A：compute + write（通用）

与切换同一套流程，目标模型换成备份时的模型：

```bash
python -m tools.reembed compute --model BAAI/bge-small-en-v1.5 --cache-dir <本机 fastembed 缓存目录>
python -m tools.reembed write --model BAAI/bge-small-en-v1.5 --expect-from sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 --yes
python -m tools.reembed verify --model BAAI/bge-small-en-v1.5
```

- `compute`（只读，白天做，约 50 分钟）：用旧模型在本机重算全部实体名与 fact，存成检查点；本机缓存里要有旧模型。
- `write`（23:00–01:45）：先读线上文本，与检查点比哈希，**检查点之后新增或改动的条目会现场补算**，
  所以备份后新增的条目也会得到旧模型的向量。写完才把标记改成旧模型，最后解除排空。
- `verify` 要带同一个 `--model`，否则它拿默认的新模型的检查点去比，必然全不符。

然后按顺序做完（连着做，标记与配置任一没跟上，服务都会拒绝启动——这是有意的）：

1. 把 NAS `.env` 里的 `KG_HUB_EMBED_MODEL` 删掉或改回旧模型，按部署准则重新发布。`/health` 的 `embed_model` 应回到
   `BAAI/bge-small-en-v1.5`，实体消解门槛随档案回到 0.95。
2. **重建 RELATES_TO 向量索引并验收**，做法同切换的第 5 步；回退后跑 `vector_index_check` 时本机**不要**设
   `KG_HUB_EMBED_MODEL`。`edge_vector_index.ratio` 应回到约 1.0，8 条验收 8/8 一致。没做完这一步，回退就没做完。

### 回退 B：restore（仅当备份后没有新增入图）

```bash
python -m tools.reembed restore --expect-from sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 --yes
```

写回备份向量并把标记改回备份时的模型，再把 `.env` 的 `KG_HUB_EMBED_MODEL` 删掉或改回、重新发布，
然后同样要**重建索引并验收**（它也是全量重写向量）。被拒绝时按提示改走回退 A，不要想办法绕过。
`restore` 只认备份时的文本：备份之后文本被改过的条目（实体改名等）它同样覆盖不到，所以备份越新越好。
