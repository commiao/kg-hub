# 战役恢复机制改造方案（草案）

> 起草：2026-10-29，Claude（本机 kg-hub 协调会话）。状态：原则（A 类自动重试不需人批）已接受；P0 已合入（#302），P1 已实现待发布，P2/P3 未做。
> 范围：`tools/extract_campaign.py` 及 `tools/campaign_*.py` 的监督/恢复层。不触碰抽取规则、质量门、图写入协议、NAS 服务。

## 1. 问题陈述

10-25 开 8000 条战役以来，两条战役（8000 条、Sol-2000）累计人工放行 10 次、合并 48 个修复 PR（10-29 当天 17 个），战役工具膨胀到 24 个模块、约 8600 行，8 个槽位为 160 个批次拉起了 107 次原生进程。NAS 本身始终健康（10-29 17:47 探测：200、0.14 秒、未排空）。

8000 条战役事件回执里的停报分类：

| recovery_kind | 次数 | 性质 |
|---|---|---|
| quality_process_violation | 71 | 自家质量规则主动冻结 |
| business_permission_denied | 50 | 自家权限门主动冻结（跨槽评审、共享摘要预览） |
| infrastructure_failed | 16 | NAS 锁等 3600 秒、温控预算耗尽 exit 76、HTTP 502、WebSocket 断连 |
| 模型容量（turn.failed "at capacity"） | 9 | OpenAI 侧 |

**三层根因**：

1. **恢复门"遇新即封"。** `campaign_recovery_manager.classify()` 对任何未列举的终态返回 `engineering_reconciliation_required`；监督器对任何非零退出或非 `none` 的 recovery_kind 返回 `native_worker_blocked`。每种新故障形态（部分上传、NAS 拒绝、容量、502、温控、旧 session 为空、PYTHONPATH 污染……）都要新增一个 `reconcile-*` 子命令（现已 13 个）、过全量测试、隔离回滚、归档证据、发 Mac 版，再由人批准 resume。
2. **故障分类靠 worker 自报。** `campaign_supervision.py` 的提示词要求 Codex worker 在最终 JSON 里自己写 `recovery_kind`。worker 没写、写错、或被容量错误直接打断时，分类就是空的，监督器只能封死。
3. **协调者是会话，不是守护进程。** 45 秒 observer 和 supervise 循环都挂在 codex 会话进程下；codex 会话反复失联（T-0370/T-0384/T-0389 各有多次"失联自动降级"），会话一掉，战役没人续。

每次放行都要用户亲自批准是既定规矩（图写入、发布须本人批准，不绕过真实拒绝），本方案不动这条，只把"不需要人判断的事"从人工路径里拿出来。

## 2. 目标与非目标

**目标**
- 基础设施类、模型容量类故障：监督器自动退避重试，不产生 coordinator_required，不需要新代码、不需要人批。
- 质量类、权限类、审批类停报：维持现状，必须人工介入。
- 未知故障：有界退避后转人工，而不是立即封死。
- 协调不依赖任何交互会话存活。
- 新故障形态出现时，默认落入通用退避，而不是新增一个 reconcile 子命令。

**非目标**
- 不改抽取规则、c1…c15 链、结构门槛、逐条语义评审要求。
- 不重放终态不明的写入；不绕过 NAS 75/76 拒绝、审批拒绝、业务权限拒绝。
- 不新增模型评测、来源配额。
- 不为了"跑完"放松质量判定。

## 3. 设计

### 3.1 三类固定分类（客观信号判定，不靠自报）

监督器在 native 退出后，按**客观信号**先分类，worker 自报的 `recovery_kind` 只作旁证，不作依据：

| 类别 | 判定信号 | 处置 |
|---|---|---|
| **A 基础设施/容量** | events 里 `turn.failed` 且 message 匹配 "at capacity"；HTTP 502/503/504、连接重置、ReadTimeout；`Reconnecting...`/WebSocket reset 通知；温控 exit 76；NAS 锁超时 exit 76；NAS `/health` 不通 | 自动退避重试，同 SID 续跑 |
| **B 业务/质量/审批** | NAS 75（真实拒绝）；worker 自报 `approval_rejected` / `business_permission_denied` / `quality_process_violation`；`uncertain_submit`（终态不明写入） | 维持现状：冻结，等人 |
| **C 未知** | 以上都不匹配 | 有界退避（见 3.2）最多 N 次，仍失败再转 coordinator_required，并把原始 stderr 尾部、events 终态写进 blocker 字段（现在 slot 3 的 `unchanged_blocked` 连 reason 都是空，必须修） |

A 类的安全前提：**重试前先做一次只读对账**（现有 `reconcile-*` 里的 `confirmed_not_written` 能力下沉为监督器内置步骤），确认上一批未落库再续同一批，已落库的来源跳过。这样"重试"不会产生重复写入，也就不需要人来证明"可以重试"。

### 3.2 退避策略参数化

退避序列写进 `campaign.json` 的 `recovery_policy`，调参不发版：

```json
"recovery_policy": {
  "version": "recovery-policy-v1",
  "infra_backoff_seconds": [300, 600, 1200, 2400, 3600],
  "infra_max_window_seconds": 21600,
  "capacity_backoff_seconds": [300, 600, 1200],
  "capacity_max_retries": 6,
  "unknown_backoff_seconds": [600, 1800],
  "thermal_resume_below_c": 80,
  "blackout_windows": [{"start": "01:50", "end": "02:20", "tz": "Asia/Shanghai"}]
}
```

- 现在 `CAPACITY_WAIT_SECONDS=300`、`MAX_CAPACITY_RETRIES=3` 是硬编码；3 次×5 分钟＝15 分钟就封死，而容量拒绝常常持续半小时以上。
- 温控：从"预算耗尽 exit 76 封死"改成"等温度降到阈值以下再续"。
- 02:00 前后家里路由器重启窗口直接跳过，不计入失败次数（见记忆 home-router-0200-restart）。
- 退避期间每次唤醒先探 NAS `/health`，不通继续等，不消耗重试次数。

### 3.3 持久守护，脱离会话

- `campaign supervise` 和 `recovery watch` 由 launchd 托管（每战役一个 label），崩溃自动拉起，日志落 `recovery/service.*.log`。
- codex 会话只做 B 类工作：逐条语义评审、修复登记、放行。会话失联不影响 A 类续跑。
- 守护进程的"人工放行"入口仍是现有 `resume`，批准语义不变。

### 3.4 冻结恢复工具面

- 13 个 `reconcile-*` 子命令不再增加；新故障形态一律走 3.1 的 C 类通用路径。
- 现有 `reconcile-worker` / `reconcile-local` / `reconcile-settled` 的"只读对账"部分抽成一个内部函数供监督器调用；它们的 CLI 保留给人工取证。
- 新增一条明确规则写进 `docs/`：**出现没见过的故障，先看它为什么没落入 A/C 类自动路径，再考虑是否真的需要新代码。**

## 4. 分阶段落地

| 阶段 | 内容 | 产出 | 风险 |
|---|---|---|---|
| **P0 不改代码** | 让当前 8000 条剩余 300 和 Sol-2000 按现状跑完；本阶段只把 `recovery_policy` 参数（容量重试 3→6 次）和 slot 3 空 blocker 修一下 | 1 个小 PR | 低 |
| **P1 客观分类 + 自动退避** | 改 `campaign_supervision.py`：客观信号分类、A 类内置只读对账后同 SID 续跑、C 类有界退避；改 `classify()` 不再对未知直接封死。**已实现**（2026-10-29，见 [docs/executors/campaign.md](executors/campaign.md) 「终态客观分类与自动退避」；`thermal_resume_below_c` / `blackout_windows` 两键未做，温控等价于 76 退避＋`/health` 探测） | 1 个 PR + 故障注入测试（容量、502、温控、WebSocket、未知） | 中：对账步骤要覆盖"部分写入"，已有测试可复用 |
| **P2 launchd 守护** | supervise/watch 迁入 launchd；codex 会话去掉循环职责 | 1 个 PR + plist 模板 | 低 |
| **P3 工具面收敛** | 冻结 reconcile-* 列表；合并重复的 continuation/rework 模块 | 删代码的 PR | 中：要保证公开 CLI 合同不变 |

发布仍走 deploy-standard：精确主干测试、隔离新旧新回滚、私有证据、用户亲自批准。P1 发布后新战役生效；在途战役不迁移。

## 5. 验收标准

- 故障注入：容量拒绝连续 5 次、NAS 502 持续 20 分钟、温控 exit 76、WebSocket reset、未知 exit 3，监督器都**不产生** coordinator_required，且不重复写入任何来源。
- B 类：NAS 75、审批拒绝、quality_process_violation 仍封死并等人，行为与现在逐字一致（现有测试全绿）。
- 杀掉 codex 会话进程后，launchd 托管的 supervise 继续推进正式计数。
- 一次新战役 2000 条，期间人工放行次数 ≤ 2（只为质量停报）。

## 6. 明确不做

- 不自动重试 NAS 75、审批拒绝、权限拒绝、终态不明写入。
- 不自动放行质量停报。
- 不改 Mac 发布的"用户亲自批准"规矩。
- 不动 NAS 服务端。

## 7. 待拍板

1. 是否接受"A 类自动重试不需要人批"这条原则（这是整个方案的前提）。
2. P0 是否现在就做，还是等两条在途战役跑完。
3. 守护进程用 launchd（本机）还是放到 NAS 上跑（NAS 跑的话要改 NAS 部署，不建议）。
