# Mac 本机服务定义

kg-hub 在这台 Mac 上的 6 个 launchd 服务。**这里是唯一真源**，机器上
`~/Library/LaunchAgents/` 里的那份是渲染出来的副本。

## 为什么存在

2026-09-10 盘点发现：这些服务的定义**只活在一台机器的一个目录里**，仓库里一个字
都没有（`capsule-watch` 连名字都没在代码里出现过）。脚本本身一直在 git 里，缺的
是「应该跑什么、多久跑一次、带什么参数」这一半。

后果：这台 Mac 挂了要凭记忆重建；改调度没有历史也没人 review；另一个 actor 不
知道你动过。

## 6 个服务

| Label | 频率 | 干什么 | 挂了会怎样 |
|---|---|---|---|
| `claude-mem-guard` | 5 分钟 | 杀 claude-mem 空转 hook（插件 CPU 死循环）+ 同步断路器开关 | 断路器失灵；空转进程烧 CPU |
| `capture-probe` | 10 分钟 | 采集链路 Mac 侧探针，拓扑图数据来源 | 拓扑图变瞎 |
| `claude-mem-ingest` | 15 分钟 | 同步 claude-mem 库到 NAS 供 refinery 消费 | NAS 侧没有新数据可吃 |
| `capsule-watch` | 每天 9:30 | 胶囊排序有变化时发飞书 | 少一封飞书 |
| `feedback-digest` | 每天 9:35 | 处理反馈待办⑥ | 少一次自动处理 |
| `weekly-report` | 周日 9:00 | 周报 | 少一封周报 |

前三个是链路的一部分，后三个是报表。

## 用法

```
deploy/mac/install.sh          # 渲染并安装全部（会重载服务）
deploy/mac/install.sh --check  # 只比对不改动 —— 发现有人手改了 plist
deploy/mac/install.sh com.kg-hub.weekly-report   # 只装指定的
```

**改调度的正确姿势**：改 `agents/` 里的模板 → 提交 → 跑 `install.sh`。
不要直接改 `~/Library/LaunchAgents/`——那样 `--check` 会报不一致，而且改动会在
下次安装时被覆盖掉。

## 占位符

模板里不存绝对路径也不存机密：

- `__REPO__` → 仓库路径，安装时按脚本位置推出来
- `__HOME__` → `$HOME`
- `@KG_HUB_FEISHU_WEBHOOK@` → 从 `<repo>/.env`（0600、已 gitignore）或同名环境变量取

取不到机密就**拒绝安装那一个**，不会装一个带着 `@VAR@` 字面量的坏 plist 上去。

## 依赖

**只有一份清单：`deploy/nas/requirements.txt`**（73 个，全部钉版本）。Mac 的 venv
用的就是它，`tests/test_mac_agents.py` 每次跑都会比对一遍，漂了就报。

> 目录名叫 `nas/` 但两边共用，是历史遗留。**没有第二份 mac/requirements.txt**：
> 2026-09-10 我一度加过一份 freeze，随后核实两边逐包逐版本完全一致——两份内容
> 相同的清单不会带来任何好处，只会给真正的漂移留一个藏身处。删掉了。
>
> （同一次我还误报过「两边有 5 个包对不上」。那是比对脚本的 bug：venv 那侧把
> 下划线规范成了连字符，清单那侧没有，于是 `docstring_parser` 和
> `docstring-parser` 被当成两个包。实际差异为零。）

## 没有备份目录

源在 git 里，任何一版都能 `git archive` 秒级还原且逐字节一致，所以不留 N 份历史
副本。这一点和 Docker 不同：镜像重建要几分钟且未必产出同样的字节（依赖解析、基础
镜像都会漂），那边才需要留旧镜像。
