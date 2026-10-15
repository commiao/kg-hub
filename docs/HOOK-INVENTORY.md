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

升级 claude-mem、Codex 或 watchdog 后运行 `--live`，再在飞书核对相同演练编号的
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
