# 落地方案：从囤积到经营 —— kg-hub 认知资产化改造

> 依据：《从囤积到经营：我用 Obsidian+AI 打造认知资产库》一文的处方，经「采纳/拒绝」决策表筛选后，落到 kg-hub 的可执行工程计划。
> 本文档目标：**任何人照着从上到下执行，都能落地并验收。** 每个工作流都给出 前置 / 步骤（命令级） / 验收标准 / 回滚。
>
> 作者：本次会话分析产出　最后更新：2026-07-09　状态：**待执行（未提交）**

---

## 0. 背景与判定原则

kg-hub 与文章里的个人 Obsidian **不是同一物种**：文章是「一个写作者人肉侍弄的小花园」，产出=验证=工作本身；kg-hub 是「一条自动记忆总线」，claude-mem 自动灌入、跨会话/工具注入 canonical 胶囊，**人基本不在环内，没有天然输出闭环**。

因此判定原则：

> **凡依赖「人肉讨论闭环 + 天然输出场景」的处方 → 拒绝或大改；凡「防退化 / 治理 / 别过早自动化」的处方 → 采纳。**

文章对 kg-hub 真正有用的部分，几乎全是「别做什么」和「扔什么」，而非「再建什么」。本方案据此裁剪。

### 0.1 现状快照（已核实，2026-07-09）

| 项 | 事实 | 出处 |
|---|---|---|
| 图谱规模 | 2,266 episodes / 5,831 实体 / 13,105 关系 | `kg_stats` |
| 注入覆盖率 | 全图 1.15% 的 episode `usage_count>0`（= 曾被 canonical_context 注入过；**非**「被使用过」，见 WS-4 插桩盲区）。胶囊层沉睡 **0%**（9 胶囊全在用）。⚠️ 原「98.9% 沉睡」说法被此盲区夸大，见 WS-4 实测基线 | `health_check`（2026-07-09） |
| **质量画像**（图内 2161 obs） | 形态质量近满分（当前 filter 拒 0%、薄内容 0%、median narrative 577 字）；type 构成 **操作型 55.4% / 知识型 12.8% / discovery 31.7%**；治理噪音 = ops_noise 26 + dup 21（~2%）。**详见 0.4 三层定义** | `quality_profile`（2026-07-09） |
| 摄入状态 | **claude-mem→kg 已休眠**（图内 obs max id 4639 vs 本地 db 9194，~4555 未进图；决策日志冻结 6/10；一次性 rebuild 后主动从 loop 删除）→ 图是 ~1 月前冻结快照，G4 需重启摄入 | `health_check` + NAS 实测 |
| 淬炼闸 | `utils/ingest_filter.py` 已上线，`shadow_mode=false`（实时拦截）；`ops_noise` 已接线未武装（`enabled=false`） | 本仓 |
| experimental | `tools/experimental/*`（贡献度/自进化）**已无任何 cron/server/ingester 引用**，冻结护栏 `tests/test_experimental_frozen.py` 在位 | 已核实 |

### 0.2 关键约束（不可违反）

1. **FalkorDB 仅绑 NAS `127.0.0.1:6379`**，Mac 连不上；图谱读写脚本**必须在 NAS 上跑**（`ssh commiao@100.123.208.32`，key-based tailscale）。
2. **server 无任意 Cypher 写端点**（安全姿态）；任何图谱写操作只能通过 NAS 上跑的受控管理脚本，**不新增公网写端点**。
3. **部署纪律**：改 `kg_hub_server.py` / 过滤器配置等运行时文件 → `git commit && push` → 同步到 NAS `/volume1/docker/kg-hub-src` → `sudo -n docker compose -p kg-hub up -d --no-deps <svc>` → **校验 NAS sha == git HEAD**（PORTAL-HANDOFF 坑 #8）。compose project 名是 `kg-hub`（非目录名）。
4. **docs 不随 redeploy 部署**（redeploy 只同步 server 文件）；纯文档改动无需 redeploy，也不会造成漂移。
5. **重新 ingest canonical 会重置 usage_count**（已知 bug，修复在分支未合，curate 时须避开重 ingest 路径）。
6. 三容器 `kg_hub_server / watchdog / ingester` 共用镜像 `kg-hub-server:latest`。本地 Python 环境：`spike-graphiti/.venv/bin/python`。

### 0.3 范围

**采纳（本方案覆盖）**：WS-0 决策存档、WS-1 冻结护栏(#9)、WS-2 一次性 curate(#11)、WS-3 收紧噪音闸(#1)、WS-4 可用性体检器(#10)、WS-5 摄入四分机制(#8)。

**明确拒绝（存档，禁止重启）**：

| 处方 | 拒绝理由 |
|---|---|
| #2 三层架构中的「讨论层」 | 按定义需人来回质疑碰撞，kg-hub 无人在环，硬加=空转 |
| #6 Discussion Insights | 依附 #2，无源 |
| #4 主题驱动选题 / #5 讨论角色 | 依赖人肉主题维护，落不了地，降级为可选，不进本方案 |

> ⚠️ **反复重申**：本方案的「淬炼」= **降噪 + 去重防膨胀**，**不是**「把信息变成我的判断」。后者需要人，属被拒的 #2。谁想在 kg-hub 里加「讨论层」，先读 0.3 拒绝理由。

### 0.4 三层质量定义（数据治理锚点 —— 由 `tools/quality_profile.py` 实测校准，2026-07-09）

> **停用「低质」这个统称**。全图画像证明它掩盖了三种完全不同的问题，混用会滑回「大扫除」叙事。往后一律按三层说话：

| 层 | 定义 | 实测（图内 2161 obs） | 处置 |
|---|---|---|---|
| **① 形态质量** | 残缺 / 灌水 / 太薄 | **≈0%**（intake filter 早已滤掉；median narrative 577 字、当前 filter 会拒 0%、薄内容 0%） | **无需再治理** |
| **② 治理噪音** | 规则可判的小集合：ops_noise(26) + 重复(21) + INCIDENT-RETRO | **~47 条 / ~2%** | **精确清理**（step 3；manifest+dry-run+archive-aware，规则可判、零误伤） |
| **③ 交付优先级** | 操作型记录（bugfix/change/feature/refactor）**非垃圾**，但默认不该与 decision/security 争注入位 | **操作型 55.4% / 知识型 12.8% / discovery 31.7%** | **软加权，不删除**（step 5；交付端 type-tiered 排序 + 探索地板） |

> **铁律**：`type` 只能当**粗代理**（一条记了坑的 bugfix 也可能很有用）→ **只适合软排序，不适合硬删**。语义级「知识 vs 日志」自动分拣＝ experimental/ 已判「不可靠」的老路，**不走**。
> **一句话定性**：kg-hub 的问题不是「多数是垃圾」，而是「**多数是低复用优先级的操作型记录**」——治理重心在**交付端降权**（让耐用知识浮上来），不在删。

---

## 执行顺序与里程碑（修正版，2026-07-09）

> **目标（定稿）**：kg-hub 持续把**最新 + 高信噪 + 相关**的知识送进会话并被真正用上。"高信噪"= 三层质量定义（0.4）——治理重心在**交付端降权**，不在删。
> **顺序原则**：质量治理（定义→交付分层→精确清理）是「进(重启摄入)」和「出(拓宽注入)」两端的前置闸；不先治理就拓宽=给污水库装泵。

**已完成（已提交 main）**：
- ✅ **G0 存档 + 冻结**（WS-0/WS-1）：方案存档、experimental 冻结护栏。
- ✅ **G1 定义+测量质量**（WS-4 + quality_profile）：体检器 + 基线 + 全图质量画像 → 得出 0.4 三层定义。
- ✅ **进闸备件**：`utils/ops_noise.py` 分类器（10/10）、WS-3 接线 + `filter_replay`（全量回放 30/30 全 ops_noise、零误杀）。**未武装、未 rebuild**（config `ops_noise.enabled=false`）。

**修正后主线（按此推进，勿滑回"大扫除"）**：

```
G2  ⏭️ 下一步 = 主杠杆：交付分层设计（step 5，仅设计 + 离线 replay，不改线上）
      拿真实查询，对比「当前 ranking」vs「type-weighted ranking」，
      验 decision/security 上浮、bugfix/change 降权但保留探索地板。
G3  精确清理（step 3，低风险随后做，别抢主线）：只清 ②治理噪音
      = ops_noise 26 + dup 21 + INCIDENT-RETRO（~47 条）；manifest+dry-run+archive-aware。
      ⚠️ 绝不碰那 55% 操作日志。
G4  进闸武装 + 重启摄入（⛔生产 gate）：flip ops_noise.enabled + rebuild/recreate；
      设计 claude-mem→kg 增量摄入通路，让图恢复最新（~4555 backlog + 持续）。
G5  交付分层上线（出）：G2 replay 达标后，把 type-weighted + 拓宽注入接入线上排序。
G6  度量使用闭环：给检索/注入加使用插桩，验「送出去的被用上」（补当前插桩盲区）。
```

**为什么 G2（交付分层）先于 G3（清理）**：① G3 只清 ~47 条=小收益；G2 决定整张图怎么被送出去=主杠杆。② G3 的 archive/filter 行为本身属**读路径/交付语义**，先把交付设计清楚再清更稳。③ 治理价值大头在"降权让耐用知识浮上来"，不在删。

**为什么先造尺子（G1 已完成）**：文章 #10——先量「用起来了吗」，否则动作无法验证。体检器 = 总验收工具。

---

## WS-0　决策存档（#采纳全表）

**目标**：把「采纳/拒绝」判定固化成文档，防止后续会话重新纠结、或心痒去建被拒的讨论层。

**步骤**：
1. 本文件（`docs/LANDING-PLAN-cognitive-asset.md`）即存档主体，已含 0.3 的拒绝清单。
2. 在根目录 `DESIGN.md`（注意：DESIGN.md 在仓库根，不在 `docs/`）抬头加一行指针：`认知资产化改造计划见 docs/LANDING-PLAN-cognitive-asset.md`。
3. 提交（仅 docs，无需 redeploy）：
   ```bash
   cd /Users/mac/workspace_claudeCode/kg-hub
   git add docs/LANDING-PLAN-cognitive-asset.md DESIGN.md
   git commit -m "docs: 认知资产化改造落地方案（采纳/拒绝存档）"
   git push
   ```

**验收标准**：
- [ ] `docs/LANDING-PLAN-cognitive-asset.md` 存在且含 0.3 拒绝清单。
- [ ] `git log -1` 显示已提交、`git push` 成功。
- [ ] `DESIGN.md`/`REPORTS.md` 有指向本文件的指针。

**回滚**：`git revert <sha>`（纯文档，无副作用）。

---

## WS-1　冻结 experimental 并加回归护栏（#9）

**目标**：#9 的冻结在实际部署层**已完成**（无引用、无 cron）；本工作流只做「上锁」——加一个自动化护栏，防止未来有人把它接回线上。

**前置**：确认现状（应为空输出）：
```bash
cd /Users/mac/workspace_claudeCode/kg-hub
grep -rn "experimental" . 2>/dev/null | grep -vE "tools/experimental/|__pycache__|\.git/|\.md" || echo "OK: no live reference"
```

**步骤**：
1. 新增回归测试 `tests/test_experimental_frozen.py`（已建）。判定口径：**只匹配真实 import/from 语句**（行首正则 `^\s*(?:from|import)\s+\S*experimental`，`re.MULTILINE`），**不误伤**注释或文档字符串里出现的 "experimental" 一词。含 `__main__` 入口，可无 pytest 直跑。扫描面 = `ingesters/`、`utils/`、`kg_hub_server.py`、`mcp_server.py`、`tools/*.py`（排除 `tools/experimental/`）。
2. 在 `tools/experimental/README.md` 顶部状态行补一句：`冻结护栏见 tests/test_experimental_frozen.py；接回线上前必须先删该测试并书面说明理由。`
3. 运行 + 提交（**本 venv 无 pytest**，用 `__main__` 直跑）：
   ```bash
   spike-graphiti/.venv/bin/python tests/test_experimental_frozen.py   # 应打印 PASS
   git add tests/test_experimental_frozen.py tools/experimental/README.md
   git commit -m "test: 冻结护栏——禁止线上代码 import tools/experimental" && git push
   ```

**验收标准**（均已实测通过 2026-07-09）：
- [x] 前置 grep 输出 `OK: no live reference`。
- [x] 直跑 `test_experimental_frozen.py` 打印 `PASS`（绿）。
- [x] **有效性验证**：临时植入真实 import 探针 `utils/_guard_probe_tmp.py`（内容 `from tools.experimental.capsule_score import *`）→ 测试**变红**并点名该文件；删除探针后恢复绿。（注意：必须是真实 import 而非注释——精确正则不匹配注释。）

**回滚**：删除测试文件即可，无运行时影响。

---

## WS-4　图谱可用性体检器 + 基线（#10）

**目标**：造 kg-hub 版的「用写文章检验知识库」——一个可重复运行的**可用性体检脚本**，产出核心健康指标，作为本方案总验收的尺子。

**设计**（新脚本 `tools/health_check.py`，**在 NAS 上跑**，直读 FalkorDB）：

输出以下指标（JSON + 人类可读），全部来自 `group_id="kg_hub"` 图：
| 指标 | 定义 | 目标方向 |
|---|---|---|
| `total_episodes` | Episode 节点总数 | —（观测） |
| `injected_ever_rate` | `usage_count>0` 的 episode 占比 = **曾被 `canonical_context` 注入过**的占比。⚠️**不是「被使用过」**（见下方插桩盲区），故不作沉睡真值，仅作趋势观测 | —（观测） |
| `capsule_dormant_rate` | **canonical 胶囊中** `usage_count=0` 的占比 —— 这个才是可信的沉睡信号（胶囊注入有插桩） | ↓ |
| `ops_noise_share` | 「运维自指 **bugfix**」episode 占比（见下方签名，只统计 type=bugfix） | ↓ |
| `orphan_rate` | 零出边或零入边的实体占比 | ↓ |
| `dup_clusters` | 高相似（同 project + 术语重合 ≥ 阈值）episode 簇数 | ↓ |
| `capsule_starvation` | canonical 胶囊中 usage=0 的个数 / 总数（= `capsule_dormant_rate` 的计数形式） | ↓ |

> **⚠️ 插桩盲区（读 `kg_hub_server.py:894` 后确认，必须写进指标语义）**：`usage_count` **只在 `canonical_context` 注入路径上 bump**，MCP `kg_search`、各 dashboard 读取**都不 bump**。因此那 ~2,229 条普通 obs 的 `usage_count=0` **不等于「没用过」，而是「这条读取路径没插桩」**。所谓「全图 1.1% 使用率」实为「曾被 canonical_context 注入过的占比」，会**系统性高估**沉睡。
> **推论**：① 非 canonical 的真实沉睡**当前不可测**，`injected_ever_rate` 只能作上界/趋势，**不得**当沉睡真值去驱动删除；② 真要测全图使用，得先给 `kg_search`/dashboard 读路径加使用插桩——**列为 WS-4 之外的独立前置**，本方案不假设它已存在；③ WS-2 的沉睡淡出（(c)）因此改为**保守策略**：只依据「年龄 + 非 decision/security + 非 canonical」，**不**依赖 `usage_count`。

**运维自指签名 `is_ops_noise`（本 WS 建立，WS-3/WS-2 复用 —— 单一真相源）**：`type == "bugfix"` **且** 文本（标题+叙事+facts）含**自我标记** `self_markers`（如 `kg-hub`/`kg_hub`）**且** 文本命中运维关键词集 `{docker, falkordb, keepalive, push hook, l2 fallback, daemon, compose, watchdog, redeploy, container, tailscale, dump.rdb}` 中 ≥2 个。
> **⚠️ 为何用文本标记而非 project（WS-4 实测校正 2026-07-09）**：初版用 `project 属 kg-hub` 判自指——**实测命中 0**。因为这些运维 obs 的 `project` 全是 `workspace_claudeCode`（与 libtv-m 等所有 Claude Code 工作同一个 project），project 区分不了「kg-hub 自身维护」。改用「正文/标题提到 kg-hub」后：实测**命中 26 条**真运维（含 1 条从 codex 修的），并**正确排除** 3 条含 infra 词但非 kg-hub 的 bugfix（Evaluation docker scan / Exit-node routing / Pillow 缺失）。
>
> **type gate 前置**（钉子①）：非 bugfix 一律 false，从源头保证 decision/security 零误杀。**纯分类器，不看 `enabled`**（钉子②）：`enabled` 只在过滤器消费端控制是否惩罚，故本 WS 落 `enabled:false` 时体检器仍能测 `ops_noise_share`。签名参数统一从 `config/ingest_filter.json` 的 `ops_noise` 块读，三处不得各写各的。

> **执行顺序注意**：WS-4 最先需要该签名，故由 WS-4 负责创建 `utils/ops_noise.py`（含纯分类器 `is_ops_noise(obs, cfg)`）+ 在 `config/ingest_filter.json` 落 `ops_noise` 配置块（**`enabled: false`，未武装**）。WS-3 的过滤器、WS-2 的 curate 直接 `from utils.ops_noise import is_ops_noise`，不重复定义。

**步骤**：
1. 建 `utils/ops_noise.py`（见 WS-3 步骤 1 的纯分类器函数体）+ 往 `config/ingest_filter.json` 加 `ops_noise` 配置块，**`enabled: false`**（见 WS-3 步骤 3）。**注意**：此时只是「定义签名 + 供体检器读」，**尚未接入过滤器 evaluate()、且 enabled=false**，所以本步对线上摄入**零影响**（双重保险：既没接线、开关也没开）。
2. 写 `tools/health_check.py`：接受 `--json`（机读）/ 默认人读 / `--baseline <path>`（把结果存为基线）/ `--compare <baseline>`（出前后 diff）。`ops_noise_share` 指标直接调 `is_ops_noise`。复用 `graphiti_client.py` 现有 FalkorDB 连接方式，**只读**，不写图。
3. 提交并同步到 NAS（`utils/ops_noise.py` + `tools/health_check.py` + config 属运行时文件，需随镜像上 NAS 才能连库；按部署纪律走）：
   ```bash
   git add utils/ops_noise.py tools/health_check.py config/ingest_filter.json
   git commit -m "feat(tools): 图谱可用性体检器 + ops_noise 共享签名（只读，未接过滤器）" && git push
   ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && git status'  # 若 NAS 走文件同步则按既有 redeploy 脚本同步
   # 按现有部署方式把改动同步进 NAS 源，再校验 NAS sha == git HEAD
   ```
4. 在 NAS 上跑并存基线：
   ```bash
   ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && sudo -n docker compose -p kg-hub exec ingester python -m tools.health_check --baseline data/health-baseline-2026-07-09.json'
   ```
   （若 ingester 容器无交互，退化为 `docker compose run --rm ingester ...`；容器名/服务名以 `docker compose -p kg-hub ps` 实际为准。）

**验收标准**：
- [x] `tools/health_check.py --json` 在 NAS 一次性容器（`compose run --rm --no-deps` + 覆盖挂载 3 文件，只读、不 rebuild、不动 server）成功输出全部指标，无异常。
- [x] 基线文件 `data/health-baseline-2026-07-09.json` 生成（含 `generated_at` 时间戳），落持久卷 `kg-hub-data/ingest-state/`。
- [x] 指标与已知事实对齐：`total_episodes=2267`、`injected_ever_rate=1.15%`。
- [x] `--compare` 可用（对自身基线出 diff，无变化行）。

**WS-4 实测基线（2026-07-09）**：
| 指标 | 值 | 解读 |
|---|---|---|
| total_episodes | 2267 | — |
| injected_ever_rate | 1.15% (26) | 注入覆盖，**非**使用率 |
| capsule_dormant_rate | **0%** (0/9) | 9 胶囊全在用（排序重做后无饿死） |
| ops_noise_share | **1.15% (26)** | WS-3/WS-2 的目标集 = 这 26 条 |
| orphan_rate | 0.57% (33/5831) | 低，健康 |
| dup_clusters(近似) | 12 | 膨胀观测项 |

> **对原诊断的诚实校正**：第一轮把「全图 98.9% 沉睡」当作核心困境——**实测这被插桩盲区夸大了**。`capsule_dormant=0`，所谓 98.9% 只是「未经 canonical_context 注入」（claude-mem obs 本就走 kg_search 取用，不走胶囊注入）。真正站得住的问题收敛为：**运维自指 26 条、近重复 12 簇、无界增长**——不是「98.9% 死知识」。这不改变方案方向（冻结/降噪/防膨胀仍对），但把「减法」的预期收益调回现实量级。

**回滚**：删脚本；只读，无数据副作用。

---

## WS-3　收紧噪音闸：运维自指 bugfix 专项规则（#1）

> **⚠️ 设计前提修正（2026-07-09 复盘）**：初版方案只想「取消 bugfix 的 `bypass_threshold`，让阈值挡下低质 bugfix」——**这条路走不通**，证据：
> - `bugfix` 基础分 `type_weight = 90`（[config/ingest_filter.json:29](../config/ingest_filter.json)），而平台阈值仅 60（claude/codex/_default）/ 80（cursor）。**光靠打分，90 分的 bugfix 天然过线，阈值对它形同虚设。**
> - 唯一剩下的杠杆是配额，但 `QuotaTracker` 是**单次 ingester 进程内计数**，注释明写「For multi-run quota tracking we'd need persistent state — deliberately deferred」（[utils/ingest_filter.py:118](../utils/ingest_filter.py)）。launchd 每 15 分钟起一个新进程，所谓「日配额」不持久，拦不住细水长流。
>
> **结论**：不能只改 override 开关。必须引入**运维自指专项规则**，直接压 ops-noise bugfix 的分/单独设更高门槛，且不给它 override 豁免。真正 bugfix（他项目的功能修复）不受影响。

**目标**：堵住困境二的凶器——运维自指 bugfix（kg-hub 自身 Docker/FalkorDB/KeepAlive/… 那批）无门槛灌入。手段=在过滤器里加 `ops_noise` 签名，对命中者**扣分 + 提高门槛 + 剥夺 override 豁免**，让绝大多数运维噪音落到阈值之下，同时保证他项目的真 bugfix 与 decision/security 零误伤。

**前置**：先看 NAS 上的**实时**决策日志（本地 `data/.ingest_decisions.jsonl` 是部署前旧档，勿用），了解当前 accept/reject 按 layer 分布、`override` 层占比：
```bash
ssh commiao@100.123.208.32 'tail -n 500 /volume1/docker/kg-hub-src/data/.ingest_decisions.jsonl' | \
  spike-graphiti/.venv/bin/python -c "import sys,json,collections; \
  c=collections.Counter(); \
  [c.update([json.loads(l).get('layer','?')]) for l in sys.stdin if l.strip()]; print(c)"
```

**步骤**（**改代码 + 配置**，非纯开关；接线可先随 WS-4 部署，武装须走 commit→push→**rebuild 镜像→recreate 容器**，见步骤 5 gate。⚠️ 非热加载：config 与代码都 baked 进镜像）：

1. **共享签名 `is_ops_noise`**（单一真相源；文件 `utils/ops_noise.py` 已由 WS-4 先创建**并实测校正**，WS-3 是首个把它**接入过滤器**的消费者，WS-2 curate 也复用同一函数）。**已实现版本**（自我标记用文本、非 project；title 纳入检索）：
   ```python
   def is_ops_noise(obs: dict, cfg: dict) -> bool:
       # 钉子①：只治理 bugfix。decision/security_note 即使提到 kg-hub+Docker 也不命中。
       if (obs.get("type") or "") != "bugfix":
           return False
       oc = cfg.get("ops_noise") or {}
       # 标题+叙事+facts 一起搜（摄入期 title 独立；图谱期 narrative=完整 content 含首行标题）
       text = " ".join([obs.get("title") or "", obs.get("narrative") or "",
                        _decode(obs.get("facts"))]).lower()
       # 自我标记：正文必须提到 kg-hub 本身（project 区分不了，见上方实测校正）。fail-closed。
       markers = [m.lower() for m in oc.get("self_markers", [])]
       if not markers or not any(m in text for m in markers):
           return False
       keywords = [k.lower() for k in oc.get("keywords", [])]
       hits = sum(1 for kw in keywords if kw in text)
       return bool(keywords) and hits >= int(oc.get("min_keyword_hits", 2))
   ```
   （`enabled` 不在本函数——见钉子②，武装与否由步骤 2 的过滤器消费端控制。）

2. **在 `utils/ingest_filter.py` 的 `evaluate()` 里接入**（在 type_override 分支**之前**判定，使 ops_noise 无法借 bugfix 的 override 逃逸）：
   - **武装开关在消费端**：`armed = cfg.get("ops_noise", {}).get("enabled", False)`。
   - 若 `armed and is_ops_noise(obs, cfg)`：
     - **剥夺 override**：跳过 `type_overrides` 的 bypass 分支（即便 type=bugfix 也不再豁免）；
     - **扣分**：`score -= cfg["ops_noise"]["score_penalty"]`（默认 100 → base 90 的运维 bugfix 变负分）；
     - **抬门槛（双保险）**：与 `max(platform_threshold, cfg["ops_noise"]["min_score"])` 比较（默认 min_score=120，只有超长叙事+高 relevance 的运维记录才可能翻身）；
     - 记 `reasons += ["ops_noise: penalized & override revoked"]`，`layer="ops_noise"`（便于日志审计与验收统计）。
   - `enabled=false`（WS-4 落库时的默认）→ 即便 `is_ops_noise` 为真也**不惩罚**，行为与今日完全一致；分类器仍可被体检器独立调用。
   - 非 ops_noise → 逻辑完全不变（真 bugfix 仍 bypass、仍 90 分过线）。

3. **配置**（`config/ingest_filter.json` 顶层加块，可回滚。⚠️ **非热加载**——config baked 进镜像，改动须 rebuild+recreate 才生效，见步骤 5）。⚠️ **钉子②：默认 `enabled: false`（未武装）**——该块由 WS-4 先落库时就是 false，故 WS-3 代码一部署**不会自动生效**；只有本 WS 用 replay 验证达标后，才显式 flip 成 true：
   ```jsonc
   "ops_noise": {
     "enabled": false,                // WS-4 落库即 false；WS-3 replay 达标后才 flip true
     "self_markers": ["kg-hub", "kg_hub"],   // 正文标记（非 project），WS-4 实测校正
     "keywords": ["docker","falkordb","keepalive","push hook","l2 fallback",
                  "daemon","compose","watchdog","redeploy","container","tailscale","dump.rdb"],
     "min_keyword_hits": 2,
     "score_penalty": 100,
     "min_score": 120
   }
   ```
   > `type_overrides` **保持不动**（decision/bugfix/security 仍各自 bypass）——收紧完全由 ops_noise 专项完成，语义更清晰、停用只需把 `enabled` 设回 false。
   > 配额那条不作为主杠杆（它不持久）；如未来要真日配额，另立工作流实现持久化 QuotaTracker，本方案不依赖它。

4. **真实 obs 回放验证影响面**（`tools/filter_replay.py`，本地读 claude-mem.db，纯读不落库）。脚本对每条 obs 跑两遍——现行 config vs **候选**（deepcopy + 仅 flip `ops_noise.enabled=true`），报告 **delta = 现行 accept→候选 reject** 的净增拦截，按 `is_ops_noise`/type 拆解，并硬断言「非运维 / 受保护类型不得进 delta」：
   ```bash
   spike-graphiti/.venv/bin/python -m tools.filter_replay --last 0 --report data/ws3-replay-2026-07-09.json
   ```
   **✅ 已跑（2026-07-09，全量 9162 条 obs）**：delta=**30**，其中 ops_noise **30（100%）**，非运维 **0**，受保护类型误杀 **0**，武装反而放行 **0**。样本全是困境二那批（full-chain repair / KeepAlive deadlock / FalkorDB auto-start…）。**签名在 9162 条真实 obs 上零误报。**
   > delta（30）≠ 图里存量（26）：本回放测**未来流入**（当前靠 override 混入、武装后会被拦的），图里已存的是 WS-2 的活；两数同量级、互印证。

5. **⛔ 生产 gate（本方案边界之外，需单独放行）——达标后才 flip `enabled:true`**。flip 前必须先核清三件事（此前假设的"改配置下轮热加载"经查**不成立**）：
   - **代码 baked 进镜像、config/源码无 bind-mount**（compose 只挂 data 卷）。改 `config/ingest_filter.json` 或 `utils/*.py` **不会**进运行中的容器 → 武装需 **rebuild 镜像 + recreate 容器**（真 redeploy，重启 server）。
   - **claude-mem→kg 摄入已确认休眠（实测 2026-07-09）**：图内 `claude-mem-obs` 最大 id=**4639**，本地 db 最大 id=**9194** → obs 4640–9194（**~4,555 条**）从未进图；决策日志 `.ingest_decisions.jsonl` 冻结在 **6/10**；NAS 无 `claude_mem_obs` 进程/cron。compose 注释表明 claude-mem 步骤是一次性 rebuild 后**主动从 loop 删除的**（休眠大概率**设计使然**，非故障）。**故 ops_noise 武装当前无活流量可治，是"未来若重启 claude-mem 摄入时的保险丝"**；图内存量 26 条归 WS-2。**flip 只在决定重启摄入时才有意义。**
   - decision/security 永远 bypass（不受 ops_noise 影响，已由 type gate + replay 证明）。
   ```bash
   # gate 放行后：编辑 config ops_noise.enabled: false -> true，然后
   git add config/ingest_filter.json && git commit -m "arm(filter): 武装 ops_noise 专项闸" && git push
   # 按摄入实际所在（NAS 容器则 rebuild+recreate；确认后执行）部署
   ```

**验收标准**：
- [x] `tools/filter_replay` 报告（全量 9162 obs）：新增拦截 delta=30，ops_noise 占比 **100%**（≥80% 门槛）。
- [x] **零误杀**（硬门槛）：delta 中非运维 **0**、受保护类型 **0**、武装反而放行 **0**。
- [x] 单元测试（`tests/test_ops_noise.py`，**10/10 已绿**）：kg-hub 运维 bugfix→true；标记在 title→true；decision/security_note 含 kg-hub+Docker→false（钉子①）；他项目 infra bugfix 不提 kg-hub→false；Exit-node/tailscale 不提 kg-hub→false（对应实测排除集）；enabled=false 仍能分类（钉子②）。
- [x] 消费端行为不变：`enabled=false` 时一条会命中的运维 bugfix 仍 `layer=override / would_accept=True`（实测），未武装＝零影响。
- [ ] （**gate 后**）flip `enabled=true` 生效后观测 3 天：`ingest_decisions.jsonl` 出现稳定的 `layer=="ops_noise"` reject。
- [ ] （**gate 后**）重跑 WS-4：新增 episode 的 `ops_noise_share` 相对基线下降。

**回滚**：`config/ingest_filter.json` 设 `ops_noise.enabled: false`，按与武装相同的部署方式生效（若武装走 NAS rebuild，则停用也需 recreate；若摄入在别处则同步该处）——代码路径立即短路，行为回到今日。无需改代码。

---

## WS-2　一次性 curate：运维噪音 / 沉睡 / INCIDENT-RETRO（#11）

**目标**：执行那笔「欠了半个月的账」——你自己复盘认证过 ROI 最高的人工治理。WS-3 是**堵未来的水**，WS-2 是**清历史的库存**。

> ⚠️ 需**图谱写操作**，而 server 无写端点、FalkorDB 仅 NAS-local → 必须在 NAS 上跑受控管理脚本，**先 `--dry-run` 再 `--apply`**，且**先备份**。

**前置 0 —— health_check 加 archive-aware 口径（用户钉子，WS-2 blocker）**：现行 `health_check` 查全库 `MATCH (n:Episodic)`，一旦 WS-2 把 26 条移出主集，就看不出「主集↓/归档↑/总量不变」。**WS-2 开动前必须先给 health_check 加 active/archive split**——报 `active_episodes` / `archived_episodes` / `total = active+archived`，使归档不变式可验。
> 归档机制二选一（WS-2 定，决定 split 怎么写）：**(a) 节点属性** `archived=true`（同一 FalkorDB database，split=按属性过滤，最简）；**(b) 独立 database** `kg_hub_archive`（health_check 需连两个 database 求和）。**倾向 (a)**：单库、health_check 只加一个 WHERE、备份/回滚都在一处。`curate_ops_noise` 的 group_id 迁移方案据此定为「打属性」而非「换库」。

**前置 1 —— 备份图谱**（不可跳过）：
```bash
ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && sudo -n docker compose -p kg-hub exec falkordb redis-cli SAVE && \
  sudo -n docker compose -p kg-hub exec falkordb sh -c "cp -a /data/dump.rdb /data/dump.pre-curate-2026-07-09.rdb"'
# 或按本仓既有 data/ 备份约定；确认 dump 文件已生成
```

**步骤**：

**(a) 隔离运维自指 episode**（不删，降级出知识层）
1. 写 `tools/curate_ops_noise.py`（NAS 上跑，`from utils.ops_noise import is_ops_noise` —— **复用 WS-4 建的同一签名，不重写**；实测该签名命中图内 26 条、零误报）：查命中 episode，`--dry-run` 列出候选（含 id/narrative 前 80 字），`--apply` 给命中节点**打属性 `archived=true`（+ `archived_at`）**（前置 0 定的方案 (a)：同库属性，非换 database），使 dashboard/`kg_search` 默认加 `WHERE NOT coalesce(n.archived,false)` 过滤、归档区仍可专门查到——**不做物理删除**。
   > **小回滚清单（关键，非整库回滚）**：`--apply` **必须**同时写出 manifest `data/curate-manifest-<ts>.json`，每条记录 `{episode_id, prev_archived, ts, matched_keywords}`；并提供 `--restore <manifest.json>` 逐条清除 `archived` 属性。这样误归档只需 `--restore`，RDB 备份降为最后一道大锤。
   ```bash
   # 干跑：只看会动谁（不写图、不写 manifest）
   ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && sudo -n docker compose -p kg-hub exec ingester python -m tools.curate_ops_noise --dry-run'
   # 人工核对候选列表（应为 Docker/FalkorDB/KeepAlive/L2 那批），确认无误后 apply（自动生成 manifest）：
   ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && sudo -n docker compose -p kg-hub exec ingester python -m tools.curate_ops_noise --apply --manifest data/curate-manifest-2026-07-09.json'
   # 若发现误归档，逐条撤销（秒级、无需碰 RDB）：
   ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && sudo -n docker compose -p kg-hub exec ingester python -m tools.curate_ops_noise --restore data/curate-manifest-2026-07-09.json'
   ```

**(b) 归档 INCIDENT-RETRO 胶囊**
2. 该胶囊由 `tools/ingest_canonical_docs.py` 的 registry 管理。归档 = 从 registry 移除（或标 `scope=archived`）使其不再进 `/api/canonical_context` 候选。**注意约束 5**：勿走「重新全量 ingest」路径（会重置 usage_count）。做法：仅改 registry + 用受控脚本把该胶囊节点 scope 就地更新，不重灌其他胶囊。
   - 若无就地更新能力，则接受 usage_count 重置为一次性代价，但须在低峰执行并记录。
3. 验证：`curl http://100.123.208.32:17171/api/canonical_context?...` 结果中不再出现 INCIDENT-RETRO。

**(c) 沉睡淡出策略**（策略先行，执行可缓 —— **不依赖 usage_count**，见 WS-4 插桩盲区）
4. 因非 canonical 的真实使用不可测，**不**用 `usage_count` 判沉睡。改用**保守的年龄口径**：`健康检查标记「年龄 > 180 天 且 type∉{decision,security_*} 且 非 canonical 且 非本次已归档的 ops_noise」`为长尾候选。**本轮不删**，只标记 + 写一条策略到 DESIGN.md，执行留到下一季度 curate（届时人工过一遍候选名单再决定移入 `kg_hub_archive`）。

**验收标准**：
- [ ] 备份 `dump.pre-curate-2026-07-09.rdb` 已生成且可用。
- [ ] `curate_ops_noise --dry-run` 候选列表经人工抽样 20 条，**≥18 条确为运维自指**（精度 ≥90%，否则收紧签名再跑）。
- [ ] `--apply` 后重跑 WS-4（已加 active/archive split）：`active_episodes` 减少约 26、`archived_episodes` 增加约 26、`total=active+archived` **不变**（证明是隔离非丢失）；`ops_noise_share`（按 active 计）下降。
- [ ] `/api/canonical_context` 不再返回 INCIDENT-RETRO；其余 8 胶囊 usage_count 未被清零（或已记录一次性重置）。
- [ ] `kg_search`/dashboard 默认视图不再被运维噪音刷屏（人工目测「问题/局限」类查询）。

**回滚**（三级，由轻到重）：
1. **首选 · manifest 精准撤销**：`curate_ops_noise --restore data/curate-manifest-2026-07-09.json` —— 只清除本次动过节点的 `archived` 属性，秒级、不影响其他数据、无需停服。
2. INCIDENT-RETRO 误归档 → 把 registry/`scope` 改回，重跑就地更新脚本。
3. **兜底 · 整库大锤**（仅当 manifest 丢失/图谱状态错乱）：从备份恢复：
   ```bash
   ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && sudo -n docker compose -p kg-hub cp falkordb:/data/dump.pre-curate-2026-07-09.rdb ... && restart falkordb'
   ```
- 因此 (a)(b) **务必先 dry-run + 备份 + 确保 manifest 落盘**。

---

## WS-5　摄入四分机制 UPDATE / EXTEND / LINK / NEW（#8）

**目标**：本方案**唯一的新工程 / 加法**。防止「新增即流水账」——摄入一条 obs 时，先判断它应当 **UPDATE**（更新已有 episode）/ **EXTEND**（作为子节点扩展）/ **LINK**（与现有建关联）/ **NEW**（确属新知识才新建），而非无脑追加。这是压制 2,229 沉睡持续增长的根因治理。

> 这是文章对 kg-hub 最对症的一条，但也最重。放在最后，且**前置依赖 WS-4 的尺子**来证明它降低了膨胀率。

**设计**（在 `ingesters/claude_mem_obs.py` 的 `add_episode` 前插入决策层）：

1. **候选检索**：对将入的 obs，用其 narrative/facts 在现图检索 top-k 相似 episode（走 graphiti/embedding 或 fulltext）。
2. **四分判定**（先规则版，LLM 可选兜底，遵循 experimental 的教训——**不引入不可验证的贡献度判分**，只做「相似度/重叠」这种可验证信号）：
   - 相似度 ≥ 高阈值 且同 project → **UPDATE**（合并进最相似节点，不新建）
   - 相似度 中 且属同主题簇 → **EXTEND / LINK**（建边，narrative 作补充）
   - 相似度 < 低阈值 → **NEW**
3. **落地范围控制**：先只对高相似（近重复）做 UPDATE/LINK，宁可漏判为 NEW（保守），不可误合不同知识（激进）。

**步骤**：
1. 先离线评估收益：用 WS-4 的 `dup_clusters` 指标 + 一份历史 obs 回放，估计四分机制能减少多少 NEW。**若回放显示近重复 < 10%，本工作流 ROI 存疑，应暂缓**（诚实止损，符合 #9 精神）。
2. 实现决策层为独立可测模块 `utils/ingest_router.py`（纯函数 `route(obs, candidates) -> Literal["UPDATE","EXTEND","LINK","NEW"]`），单元测试覆盖四类。
3. 在 `claude_mem_obs.py` 接入，**先 shadow**（`ingest_router` 只记 decision 到日志，仍全部 NEW），观测一周判定分布与人工核对准确率。
4. 准确率达标（见验收）后再让 UPDATE/LINK 真正生效，按部署纪律上 NAS。

**验收标准**：
- [ ] 步骤 1 的回放报告存档：明确近重复率，据此决定 go/no-go。
- [ ] `utils/ingest_router.py` 单测覆盖 UPDATE/EXTEND/LINK/NEW 四分支，全绿。
- [ ] shadow 一周：路由判定人工抽样 30 条，**UPDATE/LINK 判定精度 ≥ 90%**（不得误合异质知识）。
- [ ] 生效后一个月，WS-4 的 `dup_clusters` 与新增 episode 增速相对趋势下降；沉睡增量放缓。
- [ ] 无回归：`decision/security` 类永远走 NEW（不被合并）。

**回滚**：`ingest_router` 恢复 shadow（只记不合），或直接旁路该模块——摄入退回纯 NEW，行为与今日一致。

---

## 总验收（端到端）

方案整体成功的判据（全部以 WS-4 体检器度量，对比 2026-07-09 基线）：

- [ ] **减法见效**：`ops_noise_share` 显著下降；`total_episodes`（主 group）增速放缓（沉睡真值不可测，故看增速而非绝对沉睡率）。
- [ ] **闸门收紧**：新增 episode 里运维自指占比下降，且 decision/security **零误杀**。
- [ ] **护栏在位**：`tests/test_experimental_frozen.py` 常绿；experimental 无线上引用。
- [ ] **可验证性**：`health_check --compare` 能随时产出「某次改动前 vs 后」的健康度 diff（这本身就是文章 #10「用起来检验」在 kg-hub 的落地物）。
- [ ] **膨胀被治理**（若 WS-5 go）：近重复 episode 不再无脑新增。
- [ ] **存档完整**：本文档 + 拒绝清单已提交，未来会话不再重走被拒路线。

---

## 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| curate 误删/误合知识 | 数据损失 | WS-2 强制先备份 + dry-run + 隔离非删除 |
| 收紧 bugfix 闸误杀真 bugfix | 丢有价值记录 | 先 shadow 回放核对；decision/security 保留 bypass |
| WS-5 误合异质知识 | 知识污染（比膨胀更糟） | 保守阈值 + shadow 一周 + 90% 精度门槛；宁漏判 NEW |
| NAS 偶发不可达（休眠/tailscale） | 脚本中断 | 操作加重试；curate 选 NAS 在线时段 |
| 重 ingest 重置 usage_count | 使用率指标断层 | INCIDENT-RETRO 归档避开全量重灌；如必须则记录时点 |
| 改运行时文件未 push 先 redeploy → 漂移 | 线上与 git 不一致 | 严守 commit→push→redeploy→校验 NAS sha==git HEAD |

---

## 附：一页速查（执行清单）

```
[x] WS-0 存档本文档 + DESIGN 指针 → 已 commit/push
[x] WS-1 tests/test_experimental_frozen.py → 直跑 PASS（含植入真实 import 探针变红验证）→ 已 commit/push
[x] WS-4 utils/ops_noise.py + tools/health_check.py → NAS 实跑 → 基线已存（total 2267 / ops_noise 26 / capsule_dormant 0%）
[x] WS-3 接线（is_ops_noise 接入 evaluate，enabled:false 短路）+ filter_replay → 全量 9162 obs 回放：delta 30 全是 ops_noise、零误杀、零反向放行 → 已 commit/push
[ ] WS-3 ⛔gate：flip enabled:true（需先核摄入所在 + rebuild/recreate，属生产）
[ ] WS-2 前置0：health_check 加 active/archive split → 备份 dump → curate_ops_noise --dry-run → --apply(打 archived 属性 + manifest) → 归档 INCIDENT-RETRO → 重跑 WS-4 对比（误动 --restore 秒回滚）
[ ] WS-5 近重复回放 go/no-go → ingest_router 单测 → shadow 一周 → 达标生效
[ ] 总验收：health_check --compare 出改造前后 diff，确认 ops_noise_share 真降
```
