# Hook 统一维护

各工具的 hook 职责、期望组件和配置来源统一维护在：

`config/hook_registry.json`

Mac 探针 `tools/capture_probe.py` 调用 `tools/hook_inventory.py`，读取真实配置后把
`hook_inventory` 随拓扑快照上报。面板 `/dashboard/topology` 的「Hook 面板」展示：

- 事件与 matcher
- 逻辑组件及职责
- 配置来源和生效范围（用户级 / 项目级 / 插件）
- Codex trusted hash 是否批准
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
