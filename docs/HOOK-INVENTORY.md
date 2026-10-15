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
