# Mac 本机服务定义

这台 Mac 上由本仓库管的 7 个 launchd 服务。**这里是唯一真源**，机器上
`~/Library/LaunchAgents/` 里的那份是渲染出来的副本。

其中 6 个姓 `com.kg-hub.*`，还有一个是 `com.claude-mem.worker`——它不姓 kg-hub，
但同样归这里管，理由见下表那一行。

## 为什么存在

2026-09-10 盘点发现：这些服务的定义**只活在一台机器的一个目录里**，仓库里一个字
都没有（`capsule-watch` 连名字都没在代码里出现过）。脚本本身一直在 git 里，缺的
是「应该跑什么、多久跑一次、带什么参数」这一半。

后果：这台 Mac 挂了要凭记忆重建；改调度没有历史也没人 review；另一个 actor 不
知道你动过。

## 7 个服务

| Label | 频率 | 干什么 | 挂了会怎样 |
|---|---|---|---|
| `claude-mem-guard` | 5 分钟 | 杀 claude-mem 空转 hook（插件 CPU 死循环）+ 同步断路器开关 | 断路器失灵；空转进程烧 CPU |
| `capture-probe` | 10 分钟 | 采集链路 Mac 侧探针，拓扑图数据来源 | 拓扑图变瞎 |
| `claude-mem-ingest` | 15 分钟 | 同步 claude-mem 库到 NAS 供 refinery 消费 | NAS 侧没有新数据可吃 |
| `capsule-watch` | 每天 9:30 | 胶囊排序有变化时发飞书 | 少一封飞书 |
| `feedback-digest` | 每天 9:35 | 处理反馈待办⑥ | 少一次自动处理 |
| `weekly-report` | 周日 9:00 | 周报 | 少一封周报 |
| `com.claude-mem.worker` | 常驻 | claude-mem 采集 worker 的**兜底看门人** | 见下 |

前三个是链路的一部分，中间三个是报表，最后一个见下。

### 为什么 `com.claude-mem.worker` 在这里

它是 claude-mem 的服务、不是 kg-hub 的，label 也保持 claude-mem 自己的名字——
**必须保持**，这样装上去是替换插件那份，而不是与它并存（并存会有两个 job 各起
一份 worker）。放进来是因为它原本的监管是名存实亡的，而 kg-hub 的采集完全依赖
它产出的数据。

两个独立的根因（2026-09-17 查实）：

1. 旧 plist 写死版本号路径 `13.24.5/…`。插件升到 13.24.23、旧 cache 被回收后，
   program 路径彻底不存在。
2. 更隐蔽：旧 plist 指向的 `worker-wrapper.cjs` 在内层 worker 崩溃时走
   `process.exit(0)`；被 SIGKILL 时退出码是 `null` → 转成 0，正好落进
   `KeepAlive={Crashed:true, SuccessfulExit:false}` 的「不重启」那一档——
   **崩溃反而是唯一不会被拉起的情形**。9-11 15:54 那次 SIGKILL 之后 worker 死了
   10 小时，直到 9-12 01:51 下一个会话的 hook 才把它拉起来。

现在由 `tools/claude_mem_worker_launchd.sh` 现查版本后直接前台 exec
`worker-service.cjs`，`KeepAlive` 改成无条件 `true`。

**它是待机而不是抢占。** worker 靠一个固定端口做单例（`~/.claude-mem/settings.json`
的 `CLAUDE_MEM_WORKER_PORT`）。端口被占就每 5s 重查、原地等，不抢也不空转重启。
有会话在跑时 hook 6 秒就能拉起 worker，比 launchd 的 `ThrottleInterval` 快，常常
抢先——这没关系，有人拉起就行。**launchd 的价值在 hook 够不着的地方：没有任何
会话在跑的时候。** 那正是上面那 10 小时的缺口。

## 用法

```
deploy/mac/install.sh          # 渲染并安装全部（会重载服务）
deploy/mac/install.sh --check  # 只比对不改动 —— 发现有人手改了 plist
deploy/mac/install.sh com.kg-hub.weekly-report   # 只装指定的
```

**改调度的正确姿势**：改 `agents/` 里的模板 → 走分支 → 合主干 → 跑 `install.sh`。
不要直接改 `~/Library/LaunchAgents/`——那样 `--check` 会报不一致，而且改动会在
下次安装时被覆盖掉。

**改脚本（`tools/*.py`、`tools/*.sh`）则要先发布**，否则改了不生效：

```
# fleet-ops/bin/mac-release.py —— 从主干 commit 落一份不可变产物，原子切 current
python3 ~/workspace_claudeCode/fleet-ops/bin/mac-release.py kg-hub \
    --repo ~/workspace_claudeCode/kg-hub
python3 ~/workspace_claudeCode/fleet-ops/bin/mac-release.py kg-hub \
    --repo ~/workspace_claudeCode/kg-hub --check    # current 对应哪个 commit
```

2026-09-20 之前，这 8 个作业的 plist 直接指向开发工作树 —— 于是"改完即生效"，
而那不是优点，是**没有发布这一步**的另一种说法：在分支里改不生效、所有会话被迫
在同一棵树上直接改生产、脚本还可能正被执行时被原地改写（sh 边读边执行、字节偏移
错位崩溃，见本仓库 `a5325e0`）。

## 占位符

模板里不存绝对路径也不存机密：

- `__CODE__` → 发布产物目录 `~/.local/share/kg-hub/current`，**生产的代码来源**
- `__VENV__` → 解释器环境 `~/.local/share/kg-hub/venv`。它是**环境**不是代码，
  不进 git archive，所以住在产物之外（内容由 `deploy/nas/requirements.txt` 决定）
- `__GITREPO__` → 开发工作树。**只有 `source-drift` 用得上**，而且是作为输入数据：
  它的职责就是比对 git 仓库，而发布产物里没有 `.git`
- `__HOME__` → `$HOME`
- `@KG_HUB_FEISHU_WEBHOOK@` → 从 `<repo>/.env`（0600、已 gitignore）或同名环境变量取

机密同样不进产物：`claude_mem_guard.sh` 默认读 `$SCRIPT_DIR/../.env`，那是工作树
布局的假设，在产物里不存在，会**静默**拿不到 webhook。所以它的 plist 显式给
`KG_HUB_ENV_FILE` 指到 `~/.config/kg-hub/.env`。

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
