# T-0046 网关零模型调用告警接线

状态：代码已实现并合并本地工作区，已完成离线验证；尚未部署或发送测试通知。
本文只保留可公开的实现与验收说明，具体部署身份、路径和现场取样记录另存于受控工作区。

## 行为

复用 `GET /api/topology/latest`，增加固定 schema 的 `gateway_monitor`，不包含供应商正文、Key、请求/响应、任意错误文本或凭据路径。KG server 仍只读取现有网关 `/health/ready`，60 秒缓存；watchdog 在既有约 90 秒循环内额外一次读取 topology，不调用模型。新增 GET 禁用代理环境和重定向，只接受既有本地/Compose 服务地址或 Tailnet IPv4 的 8080/17171 端口，不接受浏览器或 notify 配置指定 URL。

分别报告 readiness 未通过、结果未决、实际状态写入失败证据、自然业务鉴权失败、自然供应商失败与监控证据不可用。旧 `provider_state_write_failed` 在写失败计数为零且存在可读 armed markers 时解释为未决，不能叫磁盘故障或 Key 失效。无业务结果不等于无效 Key。格式错误、过期或不可读证据保留上一轮业务告警，并单独报告监控故障；不产生虚假恢复。

网关 503/健康读取异常不改变成功的 capture topology HTTP 状态，不覆盖采集异常。历史审批隔离警告不作为新故障。新异常直接交现有 `deliver_alerts`：写 pending 后投递、失败保留、每轮重试、成功后确认、恢复沿用 clear。现有配置键 `KG_HUB_NOTIFY_CONFIG` / `feishu_webhook`（或 `KG_HUB_FEISHU_WEBHOOK`）不变；不添加通知服务。

## 验证

命令（Python 3.13.2）：

```sh
python3 -m unittest tests.test_gateway_watchdog tests.test_watchdog_delivery tests.test_dashboard_status -q
```

最终 29 项、0.012 秒、全部通过、无 skip。其中一个兼容用例逐一执行原 `test_watchdog_capture` 的 39 个函数；新 sampler 被明确 mock，避免旧 main 测试访问网络。新 fixture 覆盖实际 main 的 capture 与 gateway 同时投递、HTTP503、旧误名、实际写失败、自然401、空流量、缺字段/非法类型、过期、secret不透传、固定GET禁代理禁跳转、pending跨轮重试、capture取数不中断。旧累计写失败计数大于零、却没有新分离字段时无法知道是否仍在失败，因此判监控证据不完整，保留旧告警；不将历史计数当当前磁盘故障。通知和 HTTP 均为 mock，没有真实发送。

附加现场分类验证采用目标主机内存内计算、仅返回固定分类布尔的低暴露方式，不导出原始健康正文或业务元数据。完整授权与取样记录保留在受控工作区；分类验证不代表告警已部署或实际送达。

独立审查者复跑最终29项0.012秒全通过；另三个坏metrics、缺少at、历史累计写失败反例通过。安全和代码质量无阻断。

## 后续部署/投递验收前提

1. 独立审查后针对当前两个实际镜像分别制作仅相关文件的受控增量，保留所有其他并发变更与现有配置/挂载。禁止已禁用的通用 redeploy。
2. server Python 已导入模块，需要安全加载新代码。先确认所有 producer 的受控维护隔离和在途抽取完成；一次 active=0 不能证明无竞态。不为补告警中断可能付费的模型流、不强杀，不更改 gateway/refinery 配置。若无法证明安全停止条件则等待维护授权。
3. 先验证 server 的投影/现有 capture 都可读，再激活 watchdog，避免混合版本制造暂时 source_unknown 告警。watchdog 本身不处理模型业务，但重建时需避免旧新循环并行投递，并保留持久 pending/状态/日志与通知配置；不能声称宿主复制文件会让其下轮更新。
4. 新代码本地测试不等于真实投递验收。尚需明确授权一次真实通知或等待实际自然异常，通过脱敏的发送时间、事件类别、渠道返回业务成功与 pending 清除核实送达；不读/打印 webhook token。既有其他类型飞书成功不能冒充网关告警成功。
5. 不清理、隔离或重放未决模型记录；该事项需要操作者分类授权。KG 通用回滚仍是禁用，不是已修复。
