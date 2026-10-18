# 执行器任务说明：公共部分

> 任何能跑命令行、读写 JSON 的工具都按本文接入 kg-hub 的会话自抽取路径。本文是
> [EXTRACTION-EVAL-FRAMEWORK.md](../EXTRACTION-EVAL-FRAMEWORK.md) §2 的执行器契约，
> 三份工具模板（[claude-code.md](claude-code.md)、[codex.md](codex.md)、[qoder.md](qoder.md)）都引用它，
> 只补各自的接入点。执行循环的细节见 [AGENT-EXTRACT-PROTOCOL.md](../AGENT-EXTRACT-PROTOCOL.md)。

## 四条红线（必须写进每个工具的任务说明）

1. 抽取只用 `config/agent_extract_prompt.md` 里的 schema，不自创实体类型和边名；观测里没有知识就提交空 `entities`，不凑数。
2. 每批先 `submit --dry-run`，看 `reuse_mismatch` 里明显并错的，在 results 里改名或在 lessons 记下，再正式提交。
3. 不碰断路器、不直连 FalkorDB、不把观测正文（batch / results 文件）提交进仓库。
4. 每批把执行器自报的 token 与耗时记下来，run 结束时写进运行清单（会话型执行器的成本账本里没有，只能自报）。

## 五步命令

一次实验一个 `run_id`（格式建议 `<日期>-<执行器>-<模型简称>-<规则版本>-s<样本数>`），四个变量（执行器、模型、规则、样本）一次只动一个。

```
export KG_HUB_EXECUTOR=<工具名>          # claude-code / codex / qoder / script
export KG_HUB_RUN_ID=<run_id>
python -m tools.agent_extract run start --model <模型id> [--sample ab-sample-20261017] \
        [--server-commit <kg-hub-server 的 commit>] [--knob gateway.max_tokens=8192 ...]   # 0. 建运行清单

python -m tools.agent_extract next   --n 20 --out batch.json                       # 1. 取待抽取观测
<执行器按 config/agent_extract_prompt.md 抽取，写 results.json：{"items":[{"id", "entities", "edges"}]}>  # 2.
python -m tools.agent_extract submit --batch batch.json --file results.json --dry-run   # 3. 看消解会并到谁
python -m tools.agent_extract submit --batch batch.json --file results.json \
        --model <模型id> --session <会话或运行id>                                   # 4. 正式提交（--executor/--run-id 从环境变量取）
python -m tools.agent_extract status --group-by run_id                            # 5. 提交口径：ok/empty/reuse_mismatch/时延
python -m tools.agent_extract retro -- checkpoint --lessons-file lessons.md        # 每 500 条复盘

python -m tools.agent_extract run end --input-tokens <N> --output-tokens <N> [--wall-s <秒>]   # 收尾：自报成本
python -m tools.eval_report --run-id <run_id> [--run-id <对照 run>] --graph-json compare.json  # 三面合一张表
```

`compare.json` 由容器内 `python3 -m tools.extractor_compare --json --group-by run_id` 导出。
`run start` 只接受 `tools/digest_retro.KNOBS` 登记过的旋钮名；网关侧四个旋钮（`gateway.post_normalize`、
`gateway.custom_extraction`、`gateway.max_tokens`、`gateway.edge_empty_retry`）是服务端环境变量，
改了要发布，清单里记的是本次实验假定的值。

## 环境

`KG_HUB_URL`、`KG_HUB_API_TOKEN`（只从环境变量或 `~/.claude-mem/.env` 读，不打印）、`KG_HUB_NAS_SSH`、
`KG_HUB_AGENT_EXTRACT_DIR`（默认 `~/.kg-hub/agent-extract`，batch / results / log / runs 都在这里，不进仓库）。
