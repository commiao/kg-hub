# KG 评测适配器

业务包 `kg-hub-eval==0.1.1` 兼容核心 `llm-eval>=0.1.0,<0.2`；正式发布组合仍锁定
精确版本及 wheel 哈希，不自动升级当前环境。此次补丁仅调整依赖兼容范围，图谱抽取规则不变。
先在已核实发布源码中运行
`python tools/build_eval_adapter.py`，再从此目录用 setuptools 构建 wheel；安装本地 wheel，
不向公共索引发布。检索评分另外需要原有 numpy/fastembed 和已冻结的离线向量资源。

原 `tools/extract bench/runtime/regression/eval` 只兼容转发到已安装包。
业务提示、schema/归一化、图谱指标、检索、原准入阈值和历史证据格式由适配器维护；
原生CLI、代理、公开探针、持久尝试、调度、恢复、引文及独立性校验由公共核心实现。
打包时从真实生产 schema/归一化/标题函数生成 namespaced 依赖闭包，运行时不找源码仓库。

统一入口：

```text
llm-eval business --scenario kg_hub.entity_extract -- bench --help
llm-eval business --scenario kg_hub.entity_extract -- regression --help
llm-eval business --scenario kg_hub.entity_extract -- eval --help
llm-eval campaign --scenario kg_hub.entity_extract -- --help
```

冒烟/准入/盲测延续原 bench 命令和私有记录；正式准入必须走原业务 baseline/retrieval/
blind/semantic admission-v2 全链，阈值不放宽。通用 SDK 的 KG 场景分数是诊断，检索未算显示未知，
不能替代原正式准入。旧历史按原版本校验，不因为探针过期要求重新付费；历史字节和路径不改。

战役继续用 KG 的领取、命名同步、质量检查、整批 dry-run、正式 submit、读回、费用/抽查和复盘。
`LLM_EVAL_KG_BACKEND_MANIFEST` 指向已发布后端的锁定清单，字段为 release_root（具体发布目录）、
commit、files（完整发布目录清单）。使用适配包 pin_campaign_backend(release_root,out) 生成封存清单。
不是运行时跟随 current。后端 RELEASE.json、commit 和文件哈希均核对后才转发原 argv；
环境变量与清单由发布组合管理，缺失时拒绝启动战役。现有旧 campaign 命令继续工作。
评测产物存储与战役图谱入库是两件事；本次离线验收没有执行生产写图。

验收运行原有评测/准入/战役专项测试。历史验证入口 `tools/audit_eval_migration.py` 对照已发布版，
比较原回执判定/拒绝、结果解析、纪律指标、费用、版本及原文件哈希；不重算历史检索向量。
