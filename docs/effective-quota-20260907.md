# T-0046：实际额度来源修正

## 问题与契约

见证库 `cost_policy_ceiling` 是审批最高上限，不能用作当前路由的额度分母。
`daily` 仍来自见证库副本；新增 `effective_limits` 来自网关只读、零供应商调用的
`GET /health/ready`。网关从已验证的 `RouteRegistry` 解析策略（包含网关默认值），
仅 readiness 成功时输出。无字段、超时、503、无效数值均显示当前额度未知。
旧字段 `ceilings` 仅作为“审批上限（非当前额度）”保留在详情中。

没有修改任何实际额度、密钥、业务模型、历史未决请求或见证记录。

## 最小发布范围（本文件不是执行脚本）

1. 网关仅更新 `model_gateway.py`，使用当前受控排空发布流程；不能直接 kill。
   该补丁已保留原始代码 2026-09-07 20:48 新增的 preflight 运维 CLI，但不运行它。
   同时保留该时刻 compose 的 local logging 轮转配置与 preflight CLI 新测试基线。
2. `kg-hub-server` 使用执行前核实的当前精确 image ID 作基底，仅 overlay `topology.py`。
   不从旧工作副本整包构建，不覆盖其他任务业务逻辑。
3. 用量导出由 compose 中 watchdog 每约 90 秒执行；仅 overlay
   `tools/export_gateway_usage.py` 到其当前精确 image ID 基底。保留原容器环境与挂载。
4. Docker 同网络内默认网关是 `http://model-gateway:39000`。宿主机手工执行时必须
   显式传 `--gateway-url <本地可达网关地址>`，或设置 `KG_HUB_GATEWAY_USAGE_URL`；
   不能假设宿主能解析 Docker 服务名。
5. 等待一次正常导出，验证 `effective_limits` 日额度与路由一致、`ceilings` 不被覆盖，
   看板分母来自前者。无需发起任何付费测试。

先发布网关，再发布导出器/看板，减少过渡期“未知”。若仅回滚网关或导出器，
看板应转为未知而不是误用审批上限。回滚也必须保留其他任务的新代码。

## 离线测试

在 kg-hub 工作副本执行：

```sh
/Users/mac/.pyenv/versions/3.13.2/bin/python3 -m unittest tests.test_topology_gateway tests.test_export_gateway_usage tests.test_dashboard_status -v
```

在 credvault 工作副本执行：

```sh
/Users/mac/.pyenv/versions/3.13.2/bin/python3 -m unittest tests.test_effective_limits_health tests.test_model_gateway.TestGatewayCore -v
```

截至首轮结果：看板/导出 27 项通过，网关核心与有效额度 12 项通过。
测试中的供应商调用均为内存 FakeAdapter，不是付费请求。

## opt-in 发布入口（设计说明；实际执行结果见下文）

`deploy/deploy-effective-quota.py` 默认仅只读 preflight；明确追加 `--apply` 才发布。
每个目标使用独立、现场采集且人工复核的 JSON spec，不包含固定过期镜像。
此入口不上传或覆盖 NAS 源码，需要先准备审查过的候选目录；网关候选目录必须保留
其他任务的最新文件。阶段二需要更新网关代码时应合并一次受控排空发布。

公共 pins：`docker_argv`（如 sudo -n + 实际 Docker 路径）、`container_name`、
`container_id`、`image_id`、`container_source_sha256`（容器代码绝对路径→SHA256）、
`file_sha256`（所有已审输入绝对路径→SHA256）。所有 SHA 必须来自执行前新鲜读取。

网关 spec：`kind=gateway`，`controller`（受控 nas-compose-control.sh 绝对路径）、
`controller_environment`（明确 runtime/witness 等地址）、`deployment_state_file`、
`deployment_state_sha256`（解析 JSON 后 sort_keys 序列化摘要）。file_sha256 必须覆盖
候选构建输入、控制器及当前 routes/config 输入。仍由现有控制器完成加锁、排空、
切换、验证与失败回滚，不使用历史一次性恢复脚本，也不取消/重放未知请求。

服务 overlay spec：`kind=overlay`、`service`、`project`、`project_directory`、
`env_file`、`compose_files`（按线上使用顺序，包含已有网络/其他 overlay）、
`compose_config_sha256`（compose config --format json 原始 stdout 摘要）、
`runtime_sha256`（stable_runtime(inspect) sort_keys 序列化摘要）、
`compose_matches_live_reviewed=true`（必须先人工核对当前 compose 与运行容器一致）、
`backup_parent`、`overlay_context`、`dockerfile`、`payload_relative_path`、`target_tag`。
file_sha256 必须包含 env_file、所有 compose 文件、Dockerfile、payload 及审查过的其他输入。
server 使用 effective-quota-server.Dockerfile + topology.py；watchdog 使用
effective-quota-exporter.Dockerfile + tools/export_gateway_usage.py。

镜像基底强制为 inspect 所见 image ID，发布指定服务 --no-deps --no-build；
不会触及 refinery。备份目录0700，runtime/spec文件0600，禁止把这些完整文件贴到日志。
每个 overlay 发布后必须检查服务健康、用量输出及 runtime 一致性，才能推进下一项。

回滚：网关使用受控控制器的正式回滚流程。overlay 的备份保留旧 image ID 的
rollback.json 和精确 runtime-before/after；回滚前重新核对当前容器仍是本次 candidate、
compose/env 文件摘要未漂移。然后用相同 compose 文件顺序，最后追加该 rollback.json，
仅对该服务执行 up -d --no-deps --no-build --timeout 120。若任何基线变化则停止复核，
不得自动套用旧配置。旧镜像、备份与源码均未被脚本删除。

### 生产预检发现的必要网络增量

2026-09-07 实测 watchdog 原来只有 `kg-hub_default`，不能访问私网网关。
已获明确批准使用 `effective-quota-network.override.json` **仅**给 watchdog 增加现有
`model-gateway-private`，原网络不删、不加端口、不复制模型 caller 凭据、不改 refinery。
部署检查仅允许 `service=watchdog` + `allowed_network_additions=["model-gateway-private"]`；
其他 runtime 字段仍严格等值。备份 spec 的 `rollback_compose_files` 保存增量前文件列表，
回滚 watchdog 时须使用这个原始列表再追加旧 image 的 rollback.json，恢复原网络集合；
不能沿用新增网络 override 再声称完整回滚。

Env 先拒绝重复变量名，Binds 先拒绝重复挂载目标；只有实际 Mounts source/dest/RW
完全相同时，允许 Docker Compose 重建造成的纯列表顺序差异。没有放宽任何值的比较。

候选/批准 pins 保存于 NAS 私有目录 `/volume1/docker/kg-hub-src/.effective-quota.Q0rDgi`。
网关与看板精确旧源码备份位于 `/volume2/4T/model-gateway/.effective-quota.um3Uo6`。
这些路径包含运行环境元数据，只能私下检查，不能完整贴进日志。

## 2026-09-07 21:27（北京时间）上线验收记录

这是共享凭证合并**之前**的阶段 1/3 验证，不代替后续新 credential_ref 消费验收。

- 网关经旧控制器正常排空切换：image `66cb4cfa3b4883b87944910ca34b5022cbc70bba35fad574ea50a86540c7d4c9`，
  source `dceb556073158e1f1047c8cf3eac6054cb8e4453bbdb7e9862144bacb411df38`，
  config 保持 `f004883d3fb09ab8d102c9971709a466abf3a4eb99baf897bb71f7a29c43242d`。
  实际健康 GET 200，两个业务有效日额度均5000，external_calls=0。
- server 仅 overlay topology.py 后 image `ffcc06f017034ca3beffd075fdaaa3060d2c916677ce2a685c5fb5cffad1d4b5`；
  环境和 Binds 仅顺序变化，按前述严格等值规则再次验证，没有重复重启。
- watchdog 仅 overlay exporter 后 image `b8caf1ecc5e940b3515da25cc16847c7326f313223c219dbe47385cab69833a3`，
  仅增加批准私网；本地导出成功，未调用模型；refinery 未操作。
- 21:27 实际拓扑接口：kg-hub 今日3050 / 实际5000（61%），审批120000单独标注“非当前额度”；
  claude-mem 今日1063 / 实际5000（21%）。网关黄色“可用·历史记录已隔离”，不是全局阻断。
- 凭据看板修复另含严格内置 adapter `__main__`/import 命名空间兼容，不改网关持久代际。
  21:26:52、54、56 连续三次真实 snapshot 均 HTTP200/ready/issues=[]，历史隔离清单仍登记7/5且禁止重放。
  dashboard SHA `510ba1180f83c7b4b17597d375b58f70db0de3e330aebe687647b6ef3418abac`。
- 测试：网关94项通过；额度/原状态27项通过；部署命令stub11项通过；最终看板44项中40项通过、4项跳过。

现有 host-control 的 restart 存在全局 `target` 被 listener helper 覆盖的问题，实际只 stop；
本轮没有修改该控制器，已用独立 `start dashboard` 恢复，并以真实 snapshot 验收。
后续若需重启，修复前应独立执行 stop/start 两个控制器调用，不可仅凭 restart exit0 认定成功。

后续只读核实特别说明：历史7/5是 quarantine 清单登记数，不是当前 live 待处理数。
这些旧身份在当前 live 存储命中0/0、在17:55备份命中7/5；变化来源尚未确认。
本次额度/看板发布没有删除、恢复或重放这些记录，不得宣称这7/5仍在 live 原样保存。

为避免下次整包构建回退，已批准把 topology.py 和 tools/export_gateway_usage.py
按精确旧SHA备份同步到本地及NAS canonical源码，另新增
`deploy/effective-quota.override.yml` 记录本次精确release镜像与唯一watchdog私网增量。
未来升级须保留源码修复并按新审查更新release image，不能把固定本次镜像当作永远不变的latest。
原stage/backups保留；当前容器compose标签仍指向阶段candidate文件，不得清理这些目录。

### 最终历史文案收尾（21:34–21:36，北京时间）

凭据看板最终仅再修正文案为“历史隔离清单登记，不是当前待处理数”，
SHA `3b6d38a6e6b39e71c1f8d000c9155dca4a90711d4d8ef6822e11144ea5ecc4d9`。
21:34:42 实际 snapshot HTTP200/ready/issues=[]，两业务均已引用 `qwen.token_plan`，
模型 qwen3.8-flash、daily5000；HTML新文案存在、旧“原始证据保留”声明不存在。
这里只证明配置及看板生效，不代替 kg-hub 新代际实际产出验收。

拓扑 helper dashboard_status.py 同步更正三处文案，10项测试通过并独立复核；
以当前完整业务镜像再 overlay 该单文件，保留此前 topology 额度修改。
先 SIGTERM 正常无限等待，旧进程自然退出 code0，未超时强杀；之后 compose
--no-deps --no-build --timeout120 只启动 server，未操作 gateway/refinery/watchdog。

最终server image `8940eb2f65eed181e8492a07f07a96574dfcaffa8985d7c83ce1adda66c19302`，
container `6428227af4b01ffa782adf71d0db9621af98f7ada280925446174c8c7e37f26c`，
StartedAt `2026-09-07T13:34:53.265806983Z`。
最终helper SHA `01086087cb99a8b4b6589f40e937c2e574f3deffaedb66957f2d21e9516d1efa`；
已CAS同步本地/NAS canonical helper，并更新永久release overlay到最终镜像。
该overlay SHA `a14872e258182066dbf4b7051ba7d08305e9938ab3f2ecc2c5e48f9f5dc0988c`。
最终文案阶段备份在原private stage的 `history-wording/` 子目录，未清理任何旧阶段/备份。
