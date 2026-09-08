# T-0046 — 通用部署回滚验证（2026-09-08）

## 结论

**真实 NAS Compose 已证实：给旧容器改名不能保住回滚点。** 此次没有对任何生产服务
做失败实验、重启、部署、付费模型调用。缺陷复现实验阶段原仓库不变，独立副本基于
`c30c653918baa6839c04caf1c68259510d334b06`；后续获批入口禁用见下文。

2026-09-08北京时间15:26:29–15:26:50，在唯一项目
`t0046-rollback-fixture-20260908-79c09f4c4b66` 中：

1. 固定已有 Alpine 镜像启动 `/bin/sleep 120`，旧容器ID
   `61bc066106460dcc4d9b5264d00e2c5edc295547a7a71ba9eb54edc4c54fca1f`。
2. stop后rename为`…-rollback`，所有Compose标签仍完全相同。
3. 用同一项目/服务、故意不存在的entrypoint执行真实Compose up，返回1。
4. Compose先输出旧容器`Recreate`、`Recreated`，再启动候选失败；旧ID查询为不存在。
   新ID `12e648f23f8b4b43cc823027e6a8c29a53cd185887c153b97b9ecab66dcd6da6`
   状态仅Created；其`com.docker.compose.replace`指向被删除旧ID。
5. 只清理精确nonce标签及完整ID匹配的测试容器；最终测试项目容器集合为空。

原始脱敏证据：
[`compose-label-evidence-final-20260908.json`](../tests/integration/compose-label-evidence-final-20260908.json)。
可重复的显式opt-in脚本：
[`redeploy_compose_label_fixture.py`](../tests/integration/redeploy_compose_label_fixture.py)。

## 安全边界与平台限制

- NAS Compose版本`v2.20.1-6047-g6817716`；本机Docker daemon未运行，未擅自启动。
- 使用已存在且inspect固定的Alpine镜像
  `sha256:bf8527eb54c3680e728d5b4b383a8ba730d72dae7236fbc8dff97ed6b224a731`，不pull/build。
- network none，无挂载、端口、凭据环境变量；镜像仅有PATH；非privileged、只读rootfs、drop ALL capabilities。
- 实际内存32MiB、CPU shares=2。NAS不支持NanoCPUs；不声称硬0.1核配额生效。
- NAS内核丢弃PID限额（两种Compose写法均不生效），最终经明确批准继续单一sleep实验，
  保存实际PidsLimit=null，不声称16生效。
- 前三轮资源能力验证未进入故障注入；产生的临时sleep容器均按精确ID/标签清理。
- 没有运行原脚本硬编码的kg-hub名称/ghost清理函数。没有删除任何镜像或生产资源。

## 原测试为何没有抓住

`tests/test_redeploy.py` 的Docker stub只按文件名模拟容器名，并没有Compose项目/服务标签。
所以rename后stub会直接生成新容器而不发现旧的备份，导致“完全相同旧容器恢复”断言假通过。

这与Docker官方源码的发现和替换语义一致：
[服务发现按project/service/config标签过滤](https://github.com/docker/compose/blob/v2.39.4/pkg/compose/containers.go#L54)，
[替换路径在新容器启动前删除旧容器](https://github.com/docker/compose/blob/v2.39.4/pkg/compose/convergence.go#L572)。
实际NAS版本行为由上述真实实验验证，不只依赖不同版本源码推断。

另一个静态缺陷：`discard_staged`发生在脚本最后HTTP健康验收之前。up成功而health503时
仍会失去旧容器；现有健康失败测试只断言退出非零，没有证明旧容器恢复。

## 最小处置建议

暂时让危险通用入口在任何同步/SSH/build/stop之前fail-closed，提示明确的修复原因。
不要提供绕过开关。安全的后续方案必须在旧运行环境保全、真实健康验收和明确提交点之间建立
完整事务；不能用“当前compose+旧image重新创建”冒充精确旧runtime恢复，也不能使用
docker commit烘焙业务数据或凭据。当前受限模型网关发布事务的通过不替代kg-hub通用发布验证。

本轮只证明缺陷、阻止不安全路径；通用可用的回滚发布器仍是独立待办，不能标已修复完成。

用户现已明确同意禁用入口；最小6行fail-closed guard无绕过开关，在任何外部命令前exit2，
顶部明确“已暂停”，下方旧实现只保留作审阅，不是当前可运行说明。
名称模型mock已替换为禁用契约：默认/跳过同步/旧环境覆盖/force参数/CLI flags/普通source
均无SSH、Docker、sudo、scp、rsync等外部动作；普通source会退出调用shell，非函数库用途。
另保留3条旧实现静态配置/同步契约及bash语法，总计10项通过。
主动覆盖shell builtin或抽取脚本尾部执行属于已有任意代码权限，不是本轮防御边界。

## 本轮禁用与重新开放条件

本地原仓库仅精确合并入口、必要测试及此报告/证据，未部署服务、未改业务配置。
原HEAD=`c30c653918baa6839c04caf1c68259510d334b06`，合并前工作区干净；
本地入口SHA256=`999867ad7e5292e8a4ca7fc7e6c4cc6b33f7874f1ba877c6aacd7f58f045f7ed`。
独立审核执行入口、device_liveness、NAS配置三组共36项通过（0.430秒），
包含10项禁用契约；没有将已删除的名称-only mock当作回滚成功证据。

NAS入口路径 `/volume1/docker/kg-hub-src/deploy/nas/redeploy.sh`，权限0700。
禁用前SHA256=`71dd68d25400c27873ff64036cbce3a89402a2047dd4a081ea66fa92bedcc5b1`。
它比本地旧脚本早：缺少SSH跳板选择、device_liveness服务及其映射、watchdog后置顺序；
两个回滚缺陷仍存在。本轮经授权仅精确插入暂停说明与guard，保留这些版本差异，
没有整份复制本地代码覆盖NAS旧实现。

北京时间15:52:06后验确认NAS已禁用（未记录原子替换的精确秒数）：SHA256变为
`14f31e8d04a1bf899bc846eeecf3d4f88874259c349292f9388655f71486a389`。
执行前检查无运行redeploy进程；两次字节CAS并核对设备/inode/权限/属主；
备份和目录fsync、候选bash语法验证后原子替换，原0700与属主保留。
备份 `/volume1/docker/kg-hub-src/.t0046-redeploy-disable-20260908-5sxdb8uu/redeploy.sh.before`
权限0600、目录0700，备份hash与禁用前一致。默认调用与`KG_HUB_SKIP_SYNC=1`实际均
返回2及`DEPLOY_BLOCKED`，没有进入部署逻辑。未执行任何Docker变更操作、服务重启或业务配置修改。
独立代理15:52:06只读后验与15:47:24前基线对照：7容器完整ID、StartedAt及restart=0
均不变且running；dashboard PID/启动标识`28512:109214764`、源码`62271a…`不变；
gateway HTTP200/status=ok/external_calls=0，dashboard HTTP200/ready/issues=[]，
kg-hub HTTP200/status=ok；六配置摘要仍为
`4adcaceb5190d3d9f4608571d2e2ba76a9740e045c985e7905b30dcbe1da32ad`。
这证明所采样前后状态一致，不扩称采样间每一瞬间都有持续观测。

本地直接调用者 `deploy/nas/deploy-device-liveness.sh` 会在第2步调用此入口，因此会停止；
其第1步可能已安装公开设备身份配置，不能将外层wrapper描述为整体无副作用。
此次没有执行该wrapper。NAS所核实的canonical路径未发现该wrapper文件。

验证命令（只运行本地哨兵与静态测试，不调用NAS）：

```sh
/Users/mac/.pyenv/versions/3.13.2/bin/python3 -m unittest tests.test_redeploy -v
```

重新开放必须另行评审：精确保全原容器运行环境（不以当前compose+旧镜像冒充），
启动失败、应用健康失败均经真实隔离Compose故障实验证明可恢复；发布前后CAS与并发
约束明确，健康验收在提交/删除回滚点之前，失败保留唯一可恢复旧实例。
不得通过删除exit、临时环境开关或手工绕过来恢复使用。本轮不实现替代发布器。
