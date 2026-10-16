# 消化执行的学习闭环：每 500 条复盘一次

> 用户要求（2026-10-17）：消化 kg-hub live 积压时，**每执行 500 个任务，执行者要自行总结知识
> 入库过程的经验教训，并运用到下一轮；最终对比时还要给出实践过的优化意见。**
> 本文把这个要求定成协议，工具是 `tools/digest_retro.py`，测试是 `tests/test_digest_retro.py`。
> 指标口径与 [gateway-model-quality-20261017.md](gateway-model-quality-20261017.md) 第 3 节一致。

## 1. 一轮是什么

- 「任务」= 账本 `task_executions` 里一条 `succeeded` 的观测入图（failed/uncertain 不计，
  但它们的占比是本轮指标之一）。
- 「轮」= 自上次复盘以来累计 500 条 succeeded（`--every` 可改，A/B 小样本用 50）。
- 轮的边界由工具按账本判定，不由执行者凭感觉；`gate` 退出码 2 就是「不许再派发，先复盘」。

## 2. 执行者的循环

```
while 还有积压:
    python -m tools.digest_retro gate --state $STATE --journal $JOURNAL   # rc 2 → 进复盘
    if rc == 2:
        1. 看本轮指标：status 输出 + 上一轮 round-N.md 的「规则发现」
        2. 写经验教训（lessons.md）：三段——本轮观察到的问题、原因判断（证据）、下一轮要改什么
        3. 决定调参：只能调 KNOBS 里「已实现」的旋钮，并真的去改（改 compose 环境变量并重启 refinery、
           跑 normalize_edge_names --apply、请用户改路由……）
        4. checkpoint --lessons-file lessons.md --adjust k=v ... --applied "上一轮调参 X 已生效"
           （在 kg-hub-server 容器内跑才能拿到图库指标；容器外加 --no-graph 只有账本指标）
    继续派发（refinery 自己跑；执行者只管门禁与复盘）
结束: python -m tools.digest_retro final --state $STATE > final.md
```

`$STATE` 放 refinery 状态卷（如 `/volume2/4T/kg-hub-data/refinery-state/digest-retro.json`），
round 报告落 `~/.kg-hub/reports/retro/round-NNN.md`（`KG_HUB_RETRO_REPORTS` 可改）。

## 3. 经验教训要写什么（lessons.md 模板）

```
## 观察
- 边名合规率 36%，新出现的边名里 60% 是 X_Y 大写自创名；零边 episode 12%
## 原因判断（给证据）
- 样本 obs-29995：edge_phase 一次返回 27 条边，9 种名字不在 schema；提示词里的 edge_types 没被遵守
## 下一轮改什么
- 轮末跑 normalize_edge_names --apply（旋钮 normalize_edge_names=true）
- live_per_cycle 30 → 20，观察 uncertain 是否随并发下降
```

不要写「大体正常」这种空话；没有问题就写「无越线项，本轮不调参」。

## 4. 旋钮

| 旋钮 | 在哪生效 | 状态 |
|---|---|---|
| `live_per_cycle` / `backlog_per_cycle` | refinery 环境变量，改后重启 refinery | 已实现 |
| `parallel_tasks` | 服务端 `KG_HUB_PARALLEL_TASKS` | 已实现 |
| `normalize_edge_names` | 轮末 `python -m tools.normalize_edge_names --apply` | 已实现 |
| `route_model` | credvault 生产路由表（用户在网关侧改） | 已实现 |
| `edge_schema_map_on_ingest` | 入图时 schema 外边名 → relates_to 并保留原名 | **待实现** |
| `entity_name_filter` | 入图时 commit 哈希/绝对路径/任务号降级为属性 | **待实现** |

没实现的旋钮 `checkpoint` 会拒绝，报告里只能记为「提出但未实践的建议」。要新增旋钮，先在
`KNOBS` 登记（写明生效位置），再在协议表里加一行。

## 5. 最终对比报告包含什么

`final` 输出三块：各轮指标表；**实践过的优化意见**——每次调参的旋钮、from→to、当时的教训、
该旋钮对应指标在下一轮的变化；提出但未实践的建议（越线但旋钮未实现、或执行者没采纳）。
A/B 实验的最终对比在此之外再加各路由并排表（见 A/B 方案）。

## 6. 红线

- 工具只读图库与账本，不写图、不调模型、不动断路器；调参由执行者在对应位置做并在
  `--applied` 里如实声明。
- 账本 7.3 GB：`load_tasks` 只扫 `task_executions`（万级行）并按任务索引数调用，不扫 `model_attempts`
  全表；NAS 低峰执行。
- 没有经验教训的复盘不算复盘：`checkpoint` 拒绝空 `--lessons-file`。
