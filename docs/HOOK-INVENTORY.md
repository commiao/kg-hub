# Hook 统一维护

各工具的 hook 职责、期望组件和配置来源统一维护在：

`config/hook_registry.json`

Mac 探针 `tools/capture_probe.py` 调用 `tools/hook_inventory.py`，读取真实配置后把
`hook_inventory` 随拓扑快照上报。面板 `/dashboard/topology` 的「Hook 面板」展示：

- 事件与 matcher
- 逻辑组件及职责
- 配置来源和生效范围（用户级 / 项目级 / 插件）
- Codex 原生 `hooks/list` 的当前 hash、信任状态及是否启用
- 最近执行证据（有日志时）或明确标注“只能确认配置”

## 状态语义

- 绿色：期望项已配置；需要批准的 hook 已批准。
- 黄色：配置存在但范围受限，例如只在一个 Cursor workspace 生效。
- 红色：必需组件缺失、配置无法解析，或 Codex hook 未批准。
- 灰色：可选项未接入或该工具不使用本机 IDE hook。

**配置存在不等于实际执行。** 面板把 `configured`、`approval` 和
`runtime_evidence` 分开，不能用“文件还在”证明链路健康。

## 维护流程

1. 新增或改变 hook 职责时先改 `config/hook_registry.json`。
2. 若宿主配置格式变化，只改 `tools/hook_inventory.py` 的解析器。
3. 运行：

   ```bash
   python3 -m unittest -v tests.test_hook_inventory
   python3 tools/capture_probe.py --html /tmp/kg-topology.html --exit-zero
   ```

4. 部署 `topology.py` 后运行一次 `capture_probe.py --report --exit-zero`，
   让线上面板收到新格式快照。

## Codex 核验来源

探针通过本机 Codex app-server 的 `hooks/list` 查询生效 hook，直接使用运行时的
`currentHash`、`trustStatus` 和完整 key。它不再扫描插件缓存，不再用配置里的
key 子串证明已批准，也不复制 Codex 的 hash 算法。`modified`（hash 不匹配）、
`untrusted`（未批准）、必需组件未加载、核验失败都报红；禁用项单列。
拓扑与详细清单复用同一次查询结果，配置警告标黄。探针不会批准任何 hook。

探针默认核对用户目录的全局生效配置；可在注册表的 Codex 项设置 `cwd` 核对
指定项目作用域。`KG_HUB_CODEX_BINARY` 可指定本机 Codex 可执行文件；常见
桌面安装路径与 PATH 为后备来源。接口不存在、超时或元数据不全时明确报核验失败。

修复信任后仍须验证真实会话登记、工具调用进入 observer task 队列和观察落库，
不能用配置绿灯替代采集证据。

## Codex hook 告警

Mac capture-probe 每 2 分钟读取一次 Codex 原生信任状态并上报；NAS watchdog
在后续一轮（轮间休眠 90 秒，另加检查耗时）通过现有飞书通道发送
`codex_hooks_unhealthy`。不以空闲时间或观察条数推断 hook 故障。

未批准、hash 已变化、必需组件未加载/禁用、原生核验失败都可触发。告警含
设备、事件、完整 key、当前 hash、处理入口。该事件独立于通用
`capture_blocked`；同一事件不重复发送，故障未清除时出现新 key 或新 hash
仍会发新告警。投递失败持久化到原有待发队列，下轮重试；新鲜证据恢复后
发送解除通知。快照过期或 API 不可读不冒充信任已恢复，沿用有界 unknown
机制，并由采集探针/监控源异常提醒排查。

设备睡眠时 launchd 不执行，无法承诺墙钟时延；唤醒后随下轮检查。常态
发现时间为 2 分钟采样间隔 + 一次探针耗时 + NAS 后续一轮 watchdog 耗时。

## 升级后的一键隔离告警演练

入口 `tools/drill_codex_hooks.py` 通过 SSH 把配套执行器的源码送入当前运行的
`kg-hub-watchdog` 容器，在一个独立 Python 进程中导入**已部署的**
`tools.watchdog`，复用判断、队列落盘、去重和飞书投递函数。无需复制文件到
NAS 源码目录，也无需重启服务。Mac 发布产物更新后可从稳定入口运行：

```bash
# 默认预演：验证状态机和去重，不发飞书。
python3 ~/.local/share/kg-hub/current/tools/drill_codex_hooks.py

# 实际通道演练：发送一条模拟异常和一条模拟恢复，相隔至少 6 秒。
python3 ~/.local/share/kg-hub/current/tools/drill_codex_hooks.py --live
```

升级 claude-mem 后由下节的自动验收先运行默认预演。若需验证真实飞书通道，
再显式运行 `--live`，并在飞书核对相同演练编号的
两条消息。异常标题和恢复标题均带 `【演练】`；异常同时含虚拟的 PostCompact
未批准、PostToolUse hash 失效以及 `DRILL_ONLY` key。**不需要实际取消批准、
修改 hash 或修复任何真实 hook。** 同一异常和同一恢复各再判定一轮，确认各只投递一次。

### 依赖和参数

Mac 只需 Python 3 和 SSH；容器使用它已有的 Python/httpx。须能经 Tailscale
非交互 SSH 登录 NAS，并有现有 `sudo -n Docker` 权限；`--live` 还要求 watchdog
的通知配置启用且已有飞书 webhook，不接受 macOS 通知兜底。

默认 SSH 为 `commiao@100.123.208.32`，容器为 `kg-hub-watchdog`，Docker 入口
与发布脚本一致：`sudo -n /var/packages/ContainerManager/target/usr/bin/docker`。
可用 `--ssh-host` 或 `KG_HUB_NAS_SSH` 改目标，用 `--container` 改容器。
默认直连；使用已配置跳板别名时设 `KG_HUB_SSH_ALLOW_PROXYJUMP=1`。
不把 webhook 放进命令行或本地配置。

```bash
python3 ~/.local/share/kg-hub/current/tools/drill_codex_hooks.py --live \
  --output-dir ~/hook-drill-receipts
```

### 隔离边界与回执

- 每次生成新演练编号和新的 NAS `/tmp/codex-hook-drill-*` 目录，目录权限 0700。
  仅把当前独立进程的 `STATE_DIR`、`STATE_FILE`、`ALERTS_LOG` 指向该目录。
  不调用 watchdog 主循环，不读取或覆盖生产 `watchdog.json`、待发队列和去重记录。
- 虚拟快照只在内存中判定，不上报 topology；不读写真实 hook 信任配置、采集数据库，
  不暂停采集器。真实守护进程继续按原状态运行。默认预演连飞书配置都不读取，
  HTTP 被禁用，回执明确记录 `simulated=true`、`delivered=false`。
- 本地默认保存到 `~/.kg-hub/state/drills/<演练编号>/receipt.json` 和 `run.log`，
  文件权限 0600；`--output-dir` 指定父目录，每次仍创建新子目录。
  NAS 临时目录也保留 `watchdog.json`、`alerts.log`、`receipt.json`。
  回执包含部署中 watchdog 文件的 SHA-256、本次执行器 SHA-256（本地回执）、
  两次投递结果、去重结果、最终待发队列。日志和回执不保存 webhook URL/token。
- 退出码 0 表示该模式通过。`--live` 通过须两次 HTTP 均为 2xx、飞书业务码为整数 0，
  最终临时待发队列为空，重复判定未多发。默认预演通过只代表隔离状态机通过。

### 失败处理与验证范围

异常消息未确认送达时立即停止，不发模拟恢复；恢复消息失败时保留临时 clear 待发记录。
脚本没有自动重试或恢复生产状态的动作。SSH 超时或回执缺失时，投递结果记为 unknown，
退出码非 0：**先看本地回执、飞书演练编号和 NAS 临时证据，勿自动重跑**，避免
网络结果不明时重复发送。生产 watchdog 不会接手重试这份独立临时队列。
NAS 重启可能清理 `/tmp`，本地回执可长期留存；人工清理时仅处理已结束演练的准确目录。

演练覆盖“虚拟未批准/hash 失效 → 已部署判定 → 隔离队列 → 飞书 → 恢复/去重”。
飞书 API 接受不等于人已阅读，也不验证定时探针实际时延或真实采集落库；这些仍需
查看线上 Hook 面板的新鲜原生信任证据，并用真实会话的采集证据验证。

## claude-mem 升级后的自动验收

`com.kg-hub.claude-mem-upgrade-acceptance` 在本机每 120 秒运行
`tools/claude_mem_upgrade_acceptance.py`。它检测**当前原生 hooks/list 选中的**
claude-mem hook key/hash/信任/启用状态、定义及包清单内容，以及本机
`~/.local/share/claude-mem/current/RELEASE.json` 的 commit 和验收脚本内容。
不扫描 cache 猜最新版本；插件管理器更新和本机 runtime 发布均可触发。

第一次安装，或上述指纹变化后，验收依次执行：

1. 只读核对 claude-mem hook 已加载、启用，信任为 trusted/managed；
   未批准、hash 失效、禁用、必需插件缺失和原生接口不可读均失败。
2. 执行 `drill_codex_hooks.py --output-dir <独立验收目录>/runs`，不传 `--live`。
   仅接受 dry-run、消息模拟且未送达、去重通过、临时队列为空、生产状态未使用的回执。
3. 保存验收指纹、结果和预演回执路径。成功且指纹未变时后续轮次跳过预演；
   失败时下轮重试默认预演。任何自动路径都不会发送那两条演练通知。

升级命令完成后也可立即执行同一个入口作为验收门：

```bash
python3 ~/.local/share/kg-hub/current/tools/claude_mem_upgrade_acceptance.py --force
```

退出码 0 为通过/成功指纹未变；1 为验收失败，2 为控制器错误，75 为另一次验收持锁
（本次没有取得验收结果，稍后重试，不能按通过处理）。
实际升级操作本身仍沿用各工具原流程；该入口不执行升级、批准 hook、重启 worker、
切换采集世代或写入真实采集数据库。单独的 launchd 作业避免延长或阻塞 capture-probe。

### 失败告警与隔离

失败发送 `claude-mem 升级验收失败` 通知，包含指纹、原因和回执路径。
这是一条**真实验收失败通知**，与 `--live` 的两条模拟异常/恢复通知有不同标题。
使用已安装的 `feishu-notify` 技能，指定 `kg-hub` 命名通道；凭据仍在
`~/.config/feishu-notify/webhooks.json`，不放命令行。验收通过时静默，不发模拟恢复。

默认状态目录是 `~/.kg-hub/state/claude-mem-upgrade-acceptance/`，仅含本任务的
`acceptance.json`、锁和 `runs/` 下每次新建的预演回执。目录 0700，状态 0600。
**不使用生产 watchdog.json、生产待发队列和去重状态**；NAS 预演也仍只用独立进程
和 `/tmp/codex-hook-drill-*`。真实拓扑、hook 信任配置和采集数据均不参与故障注入。

告警先落本任务的 pending 再投递；投递失败下轮重试，确认成功后才清除。
同一失败指纹已通知后不重复发送；新版本/定义失败会重新告警。验收后来通过会
撤销已过期的待发失败消息，下次独立失败可再次通知。通知超时或进程在确认前崩溃
可能重复投递；不以无回执推断已送达。状态损坏会保留原文件、报控制器失败并直接
尝试通知；此时无法依赖损坏状态去重，须人工修复验收状态，不能清理生产队列代替。

常态触发时延为下一轮 2 分钟 + 原生检查/预演/投递耗时；Mac 睡眠时不会运行，唤醒
后再检查。状态、日志和飞书命名通道异常应及时排查；此检查不替代 NAS 独立 hook
健康告警、真实会话采集落库验收或显式 `--live` 的真实飞书通道验证。

## 升级验收失败的可重复隔离演练

`tools/drill_upgrade_acceptance_failure.py` 复用当前产物里的
`claude_mem_upgrade_acceptance.accept()` 状态机，在一个独立进程中替换其检查输入。
真实检查、hook、采集和生产告警均不注入故障。每次创建新演练编号和私有状态目录，
按以下顺序验证：本地 HTTP 503 → pending 第 1 次保留 → 第 2 次成功重试 → 两轮
重复失败静默 → 模拟恢复清空待发/通知标记 → 两轮恢复后跳过且不发消息。

```bash
# 默认：全部为本机模拟，只访问 127.0.0.1，不连接 NAS，也不读取飞书通道配置。
python3 ~/.local/share/kg-hub/current/tools/drill_upgrade_acceptance_failure.py

# 仍用本地模拟通知，但额外只读核验 NAS 生产告警状态。
python3 ~/.local/share/kg-hub/current/tools/drill_upgrade_acceptance_failure.py --verify-nas

# 显式允许：首轮仍在本机被拒，重试时向飞书发一条带编号的失败演练告警。
# 此模式同时执行 NAS 前后只读核验。
python3 ~/.local/share/kg-hub/current/tools/drill_upgrade_acceptance_failure.py --send-feishu
```

该入口和配套传输器都不接受 `--live`，也不会调用 `drill_codex_hooks.py`。
本演练的 preflight 是标明 `local-simulated` 的本地夹具，不冒充真实 NAS 预演。
既有升级自动验收仍调用真实默认预演；它不自动运行本节的失败演练。

`--send-feishu` 使用已安装 `feishu-notify` 技能的 `kg-hub` 命名通道，只允许一次
真实发送尝试。标题和正文均标注 `【演练】`，无需修复任何真实 hook；没有第二条
恢复通知。成功须 HTTP 2xx 且业务码为整数 0，不能只看 HTTP 或发送器退出码。
实际接收可按演练编号在飞书回读核对；该脚本本身不申请或使用群消息读取权限。
超时、拒绝或回执不完整会立即停止并保留 pending，不自动重试真实通道，不能
因为结果不明就重新执行。先核对飞书演练编号和回执，避免重复发消息。

### 生产隔离核验与回执范围

- 默认在演练前后只读比对本机 hook 配置、当前正式验收状态及其登记的插件源文件
  SHA-256；配置存在时还读取对应采集库计数和 worker 健康/PID。源库以 SQLite
  `mode=ro` 打开，worker 仅查询本机 `/api/health`，不启动或重启进程。健康读取
  最多两次、每次 5 秒；持续不可读时返回未能确认，不冒充采集健康。
- 默认不启动 Codex 原生检查进程，不验证实时信任是否健康，只证明已检查配置文件
  未变化。采集实例未配置时记录 `capture_checked=false`，不能当作真实采集验收。
  允许正常采集增加观察数；worker PID、数据库实例、健康状态须一致。
- 默认 NAS 回执范围为 `not-queried-local-mode`。`--verify-nas` 和 `--send-feishu`
  通过 SSH 独立进程只读比对真实 watchdog 的异常标志、pending 摘要、hook 告警身份
  和告警日志摘要，确认演练编号没有混入。正常 last_run 更新不算污染；真实告警
  同时变化时演练返回非零，需排查并发变化，不自动清理或恢复生产状态。
- NAS 使用与现有演练一致的 Docker 入口和 `kg-hub-watchdog`；可用 `--ssh-host`
  或 `KG_HUB_NAS_SSH` 指定 SSH 目标，跳板别名需 `KG_HUB_SSH_ALLOW_PROXYJUMP=1`。
  默认模式不依赖 NAS 或飞书配置；显式发送需要已安装的命名通道发送技能。
- 本地目录默认 `~/.kg-hub/state/drills/upgrade-failure/<演练编号>/`，也可通过
  `--output-dir` 指定父目录。目录 0700，回执和快照 0600，保留各轮状态、传输回执、
  生产核验快照、控制器 SHA-256；不记录 webhook 或发送器原始错误输出。
  演练结束后只有被动证据，没有定时任务会接手这份临时 pending。

退出码 0 表示**所选模式及核验范围**全部通过，1 表示失败/无法确认，2 表示参数错误。
若失败，查看 `receipt.json` 的 stage、failure_reason/error_type、deliveries 和各轮快照；
不要把 `ok=false` 的部分回执当成功，不改真实 hook 或生产队列来让演练变绿。
