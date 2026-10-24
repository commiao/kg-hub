# 抽取对比战役：一个编排者 + 每个模型一个子代理（任何工具通用）

> 目标：在**给定配额**上看各模型在生产中的产出画像，每批复盘、改链路，形成常态可复用的流程（模型高下以 bench 准入为准）。
> 消化积压不是目标；但每个模型抽的都是**不同的新观测、非空结果入库、空项正式记账**，已处理观测包含非空入库和正式判空，两者分列（10-22 三模型 2000 条的做法）。
> 工具：`tools/extract_campaign.py`。规则：开局时从 `config/agent_extract_prompt.md` 冻结的快照（`prompt.md`）+ 战役补充约定（累加，c1…cN 都生效）。四条红线见 [_common.md](_common.md)。
>
> **一句话入口**：skill `kg-model-eval`（仓库 `skills/kg-model-eval/`）——「使用kg-hub对比模型+冒烟 A、B」「使用kg-hub对比模型+准入 A、B」「使用kg-hub对比模型+盲测」「使用kg-hub对比模型+A、B、C 20个一轮 共1000条」。
>
> **定位**：bench 管**准入**（同一批冻结样本、小批、过滤不合格的模型），战役是**生产画像**（新样本、全入库、边生产边改链路）。
> 各模型在战役里抽的是各自的新观测，复盘表是「各模型在各自新样本上的产出画像」，**不是**因果意义上的模型排名；
> 模型能不能用、谁更好，看 bench 准入记录。

## 新运行的公共入口与版本

先按 [_common.md](_common.md) 设置真实的 `PY`、`KG_HUB_CODE` 和 `CLI`；所有工具均调用
`"$CLI" campaign ...`，不从会话目录加载任意旧的 `tools` 包。新战役默认 `agent-v5` / `extraction-quality-v1`。
开跑前 `"$CLI" campaign preflight` 核对实际代码、规则哈希、依赖、采集排除和服务端能力，失败先修环境。
质量表与正式修复的契约见 [quality.md](quality.md)。旧战役仍冻结旧正文，旧准入不自动升级到新版本；新规则必须重新准入或使用明确授权的例外。

## 开战役之前：bench 准入（每个模型一次）

`start` 只接受有准入记录、结论为 `pass`、且规则正文版本与本战役一致的模型（确需例外用 `--skip-admission "原因"`，原因记进战役）。

```
"$CLI" bench run   --bench ~/.kg-hub/bench/bench-20261022 --models <模型> --tag adm50 --limit 50
"$CLI" bench blind --bench <样本> --run <运行目录...> --n 10 --out <评审表>.json
#   评审者打开评审表（不含模型名），对照观测原文把每条的 verdict 填成 ok / error（error 写 note）；评完前不看 <评审表>.json.key.json
"$CLI" bench admit --bench <样本> --run <运行目录> --sheet <评审表>.json
```

| 门槛 | 要求 |
|---|---|
| 规则版本 | 候选、基线的规则版本与正文哈希需匹配；bench-20261022旧基线为agent-v4等价规则，新agent-v5不能伪装沿用旧pass |
| 硬性 | 未交 0、自检问题 0、边名越界 0、悬空边 0、未知类型 0；照抄标题 ≥95%、译文边 ≥95%；其余 fact 英文占比 ≥90%、长度落在 60–200 字符的 ≥80%（规则 23） |
| 质量 | 中英问句 top-10 命中各 ≥90%；在每个基线模型那部分不比该模型低 5 个点以上 |
| 正确性 | 盲评 10 条，事实错误（方向、数字、因果写反）≤1 条；评审表附模型调用时的 lookahead，依据它写的后续更正不算编造 |
| 空观测 | 空观测率不比基线在同一批观测上高出 5 个点以上 |
| 基线对照 | 至少有一个基线模型那部分能配上对（配不上直接判不过，不静默跳过子集检查） |
| 规则正文 | 准入记录带规则正文哈希；开战役时与开局快照比对，版本号相同但正文改过也要重新准入 |
| 条数 | ≥30 条才可能 `pass`；10 条冒烟最多 `screen_pass`（快速筛选通过，不算准入） |

`admit` 不带 `--sheet` 只预览、不写记录（避免一次随手的预览压过此前的 pass）；要撤销准入用 `extract_bench revoke --model <模型> --reason <原因>`。
准入自检只计硬错误（schema、悬空边、缺 edges 键）；照抄标题、译文边交给比例门槛，没交的交给「未交 = 0」。
结论依次为 `fail` → `screen_pass` → `pending_blind`（缺盲评）→ `pass`，带 `--sheet` 的每次判定都追加进本机
`~/.kg-hub/bench/admissions/<模型>.json`（模型、bench、规则版本、逐项检查、盲评、日期），以最新一条为准。
阈值写在 `tools/extract_bench.py` 的 `THRESHOLDS`，改阈值走 PR。成本不设门槛：处理条数（配额）就是成本上限。

## 规则冻结、回收与续跑

- **规则冻结**：`start` 把规则正文存成战役目录里的 `prompt.md`，`rules` 只读这份快照；仓库里的规则之后再改，本战役不受影响
  （`rules` 会提示）。战役内改链路只走 `chain` 补充约定；入图的 `prompt_version` 记为「正文版本+约定版本」（如 `agent-v5+c2`；旧战役仍保留其 `agent-v4+c2`），
  改链路前后在图里也能分开。换规则正文请开新战役。
- **补充约定是累加的**：`rules` 按登记顺序输出 c1…当前版的全部补充约定（前面加一行「累加：c1、c2… 依次生效」），
  后登记的与前面冲突时以后登记的为准；每个批次只带它领取时那一版及之前的约定（c1 时领的批次不会看到 c2）。
  所以 `chain` 只登记**本轮新增或改动**的约定，不用再把旧约定抄进新文件；要废止旧约定，在新约定里写明「取消 cN 的某条」。
  口径记在 `campaign.json` 的 `chain_mode: cumulative`（`status` 显示 `chain_versions`）。2026-10-24 之前开的战役没有这个字段，
  仍只输出当前一版——它们已审、已预览的批次绑定的是当时的规则文本，改口径会让质量表与 dry-run 标记全部失效；
  这类战役想让旧约定继续生效，仍要登记合并版（10-24 战役的 c4 就是这样补救的）。
- **续跑**：子代理中途挂掉，批次文件和已写的结果都还在——新派的子代理用同一个批次号接着 `lint` / `submit` 即可，不用回收。
- **回收**：批次做坏了不想交、或子代理带着它走了：`"$CLI" campaign release --id <战役> --batch <批次> --reason "<原因>"`。
  没正式提交的整批放回队列；已提交的只放回没入库的那几条。放回的条目还给配额，下一次任何模型 `claim` 时优先领
  （这期间已在别处入库的自动丢掉）。不要手改 `campaign.json`。

## 自检（lint）查什么

`lint` 不过不许 dry-run、不许正式提交（`submit` 先跑一遍 lint）。子代理不用再自己写脚本查下面两项长度：

| 检查 | 口径 | 何时查 |
|---|---|---|
| 悬空边、实体类型 / 边名越界、缺 `edges` 键 | schema 硬错误 | 一直查 |
| 第一条边照抄标题、第二条边是标题译文 | 规则 21、22 | 一直查 |
| 其余 fact 超长 | 第 3 条边起，空白归一后**超过 200 字符**（与复盘表「其余长度合规%」同一量法）；逐条列出边序号与两端实体。按规则 31 拆成几条边或重写，不截断 | 一直查 |
| 新建实体 summary 过短 | 少于 **30 字**（`strip` 后的字符数，规则 24）；只查 dry-run 返回的 `new_entities` 里的实体——并到图里旧实体的、并到同批前面条目新建实体的（`dry_run_pending`）都不查 | dry-run 之后 |

新建与否离线分不出来（哪些名字会并到旧实体要服务端消解才知道），所以 summary 这一项取**最近一次 dry-run** 的结果：
每次 dry-run 把各条的 `new_entities` 记进批次的 dry-run 标记文件（`dryrun-<批次>.json`），同一条再预览就覆盖。
还没 dry-run 的条目这一项不查，`lint` 会在标准错误提示「dry-run 后再 lint 一次」；正式提交要求每条都 dry-run 过，
所以提交前那次自检一定查得到。改了 summary 结果就变了，要重新 dry-run（与改名同一规矩）。

## 领批：凑满一批再交

`claim`（以及 `agent_extract next`）从游标往后逐段扫（每段 `max(还差条数×4, 40)` 条，最多 8 段，
`tools/agent_extract.FILL_MAX_ROUNDS`），跳过质量闸拒掉的、已入库的、别人正领着的，凑满一批再交。
积压稀疏时一段凑不满（2026-10-23 战役第 1 轮：游标后 80 条只剩 4 条可抽），以前会交出不满额的一批、子代理领完就收工。
8 段都凑不满才交不满额的批次（或提示游标已前移、再领一次）；批次 `stats.rounds` 记扫了几段。

## 抽取会话不产生新观测：战役一律在专用目录里跑

2026-10-24 实测：跑战役的 codex 会话 2 小时里产生了 214 条 claude-mem 观测（同期它入库 293 条）——领批、读观测、抽取、提交的每一步
都被记成了新观测、又进了待抽队列，看板「剩余」几乎不降。所以：

- **先建专用目录**：`"$CLI" campaign workspace --repo <已核实Git仓库>` 建（或同步）`~/work-ai/kg-hub-campaign`（kg-hub 主干的独立 worktree，
  分离 HEAD，不在里面改代码；`KG_HUB_CAMPAIGN_WORKSPACE` 可改位置），并把它追加进 claude-mem 的 `CLAUDE_MEM_EXCLUDED_PROJECTS`
  （先备份 `~/.claude-mem/settings.json`，原有规则保留）。claude-mem 的钩子与记录服务两侧都按会话目录排除，对所有工具都生效。
- **编排者与子代理都在专用目录里打开会话**——只在命令里 `cd` 进去不算，钩子按会话目录判断。
  从别的目录（例如工具为会话自动建的 worktree）触发战役时，在那个会话里只出计划、等确认，确认后到专用目录直接开新会话跑，
  不给触发会话的目录追加排除、也不加 `--allow-tracking`（2026-10-24 用户约定：战役固定在专用目录，不每次新加目录加白）。
- **领批前自检**：`start`、`claim` 先查会话目录是否已排除（或设了 `CLAUDE_MEM_INTERNAL=1`），没有就拒绝；确需例外用
  `--allow-tracking "原因"`，会记进战役的 `tracking_exceptions`。
- `launch` 生成的命令都在真实采集已排除的专用目录里执行，不自动设置 INTERNAL 绕过；qodercli 加 `--setting-sources local`，codex 加 `--ignore-user-config`。
- 别的设备（NAS 上的 DSH、Windows）各有自己的 claude-mem 设置，要在那台设备上同样跑一次 `workspace`（或手工加排除规则）。

## 空观测也记录：记为「已处理（空）」

执行者判为「没有可抽取知识」的观测，正式提交时发一条 `empty: true` 的记录：服务端只记 IngestedKey `status=empty`（带判定的模型、
执行工具、run_id），不写任何图数据；refinery 对账把它记进水位的 `rejected`，看板「剩余」随之下降，断路器恢复后网关也不会再抽一遍。
领批查重把 `empty` 与 `ok` 一样当作已处理。早先只记在本地日志里的空观测用 `"$CLI" campaign mark-empty --id <战役>`
（普通执行器的状态目录用 `"$CLI" agent mark-empty`）补记，可重跑。普通领批不会再抽正式记空的来源；若复核发现漏抽，使用正规修复入口将 empty 升级为非空，并保留原来源键和审计，不把它当成新增样本。

## 同一台设备上多个工具、多个战役同时跑

- **同一个战役**：领批、提交、记成本都在战役目录的文件锁里，多个工具（Claude Code、qoder、codex、cursor、DSH）同时跑不会领到同一条。
- **不同战役之间、战役与 `agent_extract next` 之间**：走**全设备在途领取登记**（`tools/claim_registry.py`，
  默认 `~/.kg-hub/claims/`，不随 `KG_HUB_AGENT_EXTRACT_DIR` 变）。领批时剔除别人正领着的观测，领到的登记到自己名下
  （每个战役一个领取方，每个 next 状态目录一个）；正式提交后入库的注销，没入库的留着等回收重领；登记 12 小时
  （`KG_HUB_CLAIM_TTL_HOURS`）没动静自动失效。被跳过的观测领取方的游标会越过去，若占着的一方最后没入库，
  它们留在积压里，不会丢。
- **同一状态目录里多个工具同时 `agent_extract next`**：游标与批次号在登记锁里读，领取方按批（`next:<状态目录>:<批次号>`），
  所以几个工具共用默认状态目录、甚至给了同一个 `--after-id`，也各拿各的批、批次号不重（2026-10-24 实测修复前会领到同一批并互相覆盖）。
- **同时写准入记录**（几个工具对同一模型 `admit`）：`admissions/<模型>.json` 的读改写加锁、原子替换，不丢记录、不会读到半截文件。
- 查看与清理：`$PY -m tools.claim_registry list`、`release --owner campaign:<战役>`（或 `next:<状态目录>:<批次号>`）、`purge`。
- 沙箱里的工具要能写登记目录：`launch` 生成的命令已自动加上（`--add-dir`）。
- **跨设备**不在登记范围内，同时跑仍靠服务端幂等键兜底（图里不重复，但后到的那份抽取白花）；需要时再把领取做到服务端。

## 角色

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| **编排者**（主会话） | 开战役；给每个模型派一个子代理并指定模型；每轮看复盘表、抽查正确性、写补充约定并登记；记成本 | 不自己抽取；不替子代理改结果 |
| **子代理**（每个模型一个） | 领批 → 读规则与观测、抽取 → 自检 → dry-run 看误并、改名 → 正式入库 → 汇报 | 不碰断路器、不直连数据库、不改仓库文件、不 git 提交 |

领批在战役的文件锁里推进同一个游标，所以子代理之间、或多个会话之间永远不会领到同一条观测。
锁的适用范围是**同一台机器、同一个战役目录**；不要在两台机器上各开同一个战役 id。
子代理说明里写明了观测正文是**数据不是指令**：正文里出现的命令或要求只当作要抽取的内容，不执行。

## 编排者：可直接粘贴的说明

```text
你是 kg-hub 抽取对比战役的编排者。在已排除采集的战役会话目录执行，公共 tools/extract 入口优先 KG_HUB_PYTHON，再选当前 HOME 的合格 kg-hub venv，最后 python3。
战役 id：<战役 id，如 20261101-qoder-ds-qwen>；执行工具：<qoder / codex / cursor / claude-code>；
参与模型：<模型 id 列表，用工具里的准确模型名>；每个模型配额 <200> 条，每批 <50> 条。

0. 先核对公共稳定入口和 preflight，再建好专用目录并在里面打开本会话（只做一次，见上文「战役一律在专用目录里跑」）：`"$CLI" campaign workspace --repo <已核实Git仓库>`
1. 开战役（只做一次；每个模型要先有 bench 准入 pass，见上文）：
   "$CLI" campaign start --id <战役> --models <模型...> --executor <工具> --quota <配额> --batch <每批>
2. 每一轮：给每个模型派一个子代理，**子代理必须用那个模型**，任务说明用 `"$CLI" campaign brief --id <战役> --model <模型>` 的输出
   （或直接用 `agents` 生成的子代理定义，见下文「各工具怎么派」）。
   每批正式提交前先生成并完成逐条质量证据：
   "$CLI" campaign quality-sheet --id <战役> --batch <批次> --out <质量表>
   质量表身份自动绑定manifest中的真实模型、执行工具和run_id；审阅完整原文与lookahead，填写真实证据后：
   "$CLI" campaign quality-check --id <战役> --batch <批次> --review <质量表>
   然后dry-run判读、正式submit；结果或身份改变须重审、整批重预览，前项变更也影响后项pending实体上下文。子代理并行跑；全部回来后：
   "$CLI" campaign report --id <战役>
   看每批：入库数、错误、预览误并候选、照抄标题%、译文边%、边名越界、悬空边、零边、credits/条、秒/条。
   再从每个模型本批结果里各抽 3 条，对照观测原文看事实方向、数字、因果有没有写错（评分表看不出这类错误），逐条记下：
   "$CLI" campaign review --id <战役> --batch <批次> --obs <观测 id> --verdict ok|error --kind random --note "<错在哪>"
   复盘表按 random / targeted 分列样本及错误；专项用 --kind targeted，不能和随机分母混算。
   旧无kind记录单列legacy_reviewed / legacy_review_errors（旧记录未分类/旧记录错误），不计入random_*；reviewed仍是全部评审总数。
   系统性归属错误要全量核对belongs_to，高空率要阅读全文复核空项；发现未修问题如实保留。
3. 有新教训时把本轮发现写成补充约定；没有就记沿用。补充约定是累加的，新文件只写本轮新增（只写可执行的规则，附一句理由），存成文件后登记：
   "$CLI" campaign chain --id <战役> --file <补充约定文件>
   之后领的批次自动带新版本，复盘表按「模型 × 约定版本」分组，改链路前后可以直接比。
4. 成本分两本账：子代理汇报里的 credits / tokens / seconds（拿不到是 null）记进它那一批，复盘表的每条成本只含子代理：
   "$CLI" campaign cost --id <战役> --batch <批次> --credits <N> --tokens <N> --seconds <N>
   编排者自己的开销单独记，复盘表另列合计、不摊进任何模型：
   "$CLI" campaign cost --id <战役> --overhead --credits <N> --note "<这一轮做了什么>"
   （qoder 编排时，`qodercli -o json` 的 `modelUsage` 按模型分开给出 credits，可直接拆成各子代理与编排者两份。）
5. 子代理失败：能接着做就用同一批次号续跑；做坏了就 `release --reason`，条目回到队列。
6. 重复 2–5，直到所有模型核对最新终态达到配额且 claim 返回 done。最后给出：各模型 × 约定版本的产出画像表、
   每轮改了什么、改完指标怎么变（同一模型、不同约定版本之间比）、每条成本。
   注意：不同模型抽的是不同观测，模型之间的高下以 bench 准入记录为准，不要从本表下因果结论。
```

## 子代理说明：由命令生成，不手抄

说明模板只有一份，在 `tools/extract_campaign.py` 的 `SUBAGENT_BRIEF`。按战役与模型填好：

```
"$CLI" campaign brief  --id <战役> --model <模型 id>      # 打印一份，粘给子代理或某个会话
"$CLI" campaign agents --id <战役>                        # 每个模型一个子代理定义（JSON，给 qodercli --agents）
"$CLI" campaign agents --id <战役> --format md --dir <目录> # 写成带 frontmatter 的定义文件（含 model 字段）
```

子代理的流程：读 `rules` → `claim` 领一批 → 每 10 条一组读全文抽取（起草时就按一句一事实写短句）→ `lint` 自检 →
`quality-sheet` 生成待审表 → 逐条提供原文证据并核对归属/完整句/译文/覆盖及判空 → `quality-check` →
`submit --dry-run` 看复用候选 → 再 `lint` 一次（查新建实体 summary）→ `submit` 入库。
被权限、hook 或分类器拦下时立即停下汇报，不换写法绕过。
若修改结果或身份，重新生成并审阅质量表、重新核验和整批预览。汇报包括实际终态、未修问题与成本覆盖。红线同 [_common.md](_common.md)。

## 各工具怎么派「指定模型的子代理」

| 工具 | 方式二（一个编排会话派多个模型的子代理） | 证据（2026-10-22 实测） |
|---|---|---|
| **Claude Code** | ✅ 子代理工具带 `model` 参数（opus / sonnet / haiku） | 10-22 三模型 2000 条实验就是这样跑的 |
| **qoder** | ✅ 子代理定义的 `model` 字段生效。命令行：`qodercli -m <编排者模型> --agents "$("$CLI" campaign agents --id <战役>)"`；界面：`agents --format md --dir <仓库>/.qoder/agents` | 主会话 Qwen3.8-Max，子代理分别指定 DeepSeek-Flash / Kimi-K3，输出 `modelUsage` 的内部模型名随之变为 `dfmodel` / `kmodel_latest`，credits 按模型分开记 |
| **cursor** | ✅ 子代理定义的 `model` 字段生效，派子代理的工具本身也带 `model` 参数。`agents --format md --dir <仓库>/.cursor/agents` 生成定义，编排者在 cursor 里按名字调用 | 主会话 gpt-5.2，`taskToolCall` 参数里是 `model: gemini-3.7-flash-high`，子代理回复自报 Gemini 3.7 Flash |
| **codex** | ⚠️ 未确认：派生工具自报有 `model` 参数，但 `codex exec --json` 的事件流里两次都没有派生记录（只有一次空的 `wait`），不能确认子代理真按指定模型跑 | 先用退路 |
| **DSH**（DeepSeek Harness） | ❓ 待验证：DSH 有子代理插件（`subagent` / `subagent_fork`），能否给子代理单独指定模型要在 DSH 里实测（让编排者派一个指定了模型的子代理，看 DSH 的用量记录按哪个模型计费） | 先用退路：`launch --tool dsh`，每个模型的会话开跑前在 DSH 模型设置里切好模型 |

做不到方式二（或想并行开多个会话）时，用**每个模型一个会话**，仍是同一个战役：

```
"$CLI" campaign launch --id <战役> --tool codex|cursor|qodercli|dsh [--model <模型>...]
```

打印每个模型的启动命令：子代理说明由 `brief` 现场生成；都把战役状态目录加成可写（`--add-dir`）、放开网络
（子代理要连 NAS 领批、连 kg-hub 提交）；codex 还要关掉标准输入（`< /dev/null`），否则会一直等输入。
启动参数由公共工具生成；第一次在新设备/新执行器使用时先预检并完成1批，不能用别的设备成功替代本机验证。

注意：cursor 与 codex 会加载用户级设置里的钩子（实测 cursor 会话开头被注入了一行 kg-hub 上下文）；
公共入口不会改变会话目录；必须仍使用排除的会话目录或已核验的internal模式，不能把加载新代码误当采集已排除。

## 和其他用法的关系

- **冒烟**（新模型进场前，不入库）：`tools/extract_bench.py` 在冻结样本上离线评分，见 [qoder.md](qoder.md) 的「对比模式」。
- **运行器**（`extract_bench run --executor qodercli|codex|claude`）：把 qoder、codex 或 Claude Code 命令行当纯模型通道、无人值守地比模型本身；不入库，用于冒烟与准入。
- **战役**（本文）：多模型生产画像，入库，每批复盘、改链路；模型能不能用、谁更好看 bench 准入记录。
