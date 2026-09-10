# 部署准则

2026-09-10 立。**本工作区所有服务的唯一部署依据**，不分机器、不分形态。
谁部署都照这一份，别再各写各的。

## 一句话

**部署物永远是一个已推到远端的 commit；线上永远能说出自己跑的是哪个 commit；
回滚永远是「把指针指回上一个」，不是抢救现场。**

三种落地形态只是这句话的三种写法：

| 形态 | 指针是什么 | 回滚动作 |
|---|---|---|
| Docker | 镜像标签 `<name>:<sha>` | 标签指回去，`up -d` |
| 本机常驻服务 | 符号链接 `current → releases/<sha>` | 链接指回去，重启服务 |
| 客户端/工具 | 服务端统一分发的版本 | 服务端回退，各端自动跟随 |

## 适用范围（2026-09-10 实测）

| 机器 | Docker | 本机直装 |
|---|---|---|
| **NAS** | 主战场：kg-hub 五服务、credvault 模型网关、task-hub、report-portal | 少量：看板是宿主机回环进程（安全契约不许进容器） |
| **Mac** | 有，7 个容器（recruitment-*、aliyun-observability-mcp）。**已出现两个跑无标签镜像的容器**（`docker ps` 里显示成一串 ID），正是本准则要禁的 | **11 个 launchd 服务，全部直接跑 git 工作区** —— 见下文「最大的洞」 |
| **Windows** | 无 | 无。目前只有 MCP 客户端接入（T-0050 未完成） |

## 最大的洞：Mac 上根本没有「部署」这一步

实测至少 8 个常驻服务的 launchd 直接指向 git 工作区：

```
com.credvault.model-gateway-tunnel  → workspace_claudeCode/credvault/session_forwarder.py
com.credvault.claude-mem-forwarder  → workspace_claudeCode/credvault/claude_mem_forwarder.py
com.credvault.connection-status     → workspace_claudeCode/credvault/credvault.py
com.task-hub.bridge                 → workspace_claudeCode/task-hub/client/bridge.py
com.kg-hub.capture-probe / capsule-watch / feedback-digest / weekly-report
```

后果不是理论上的：

- **改一个文件就是上线。** 没有构建、没有版本、没有验收、没有回滚。
- **没提交的半成品会自己上线。** 多 actor 工作区里，另一个人正在改的文件一保存，
  下次那个服务重启就跑他改到一半的代码。2026-09-10 实测 forwarder 在一个日志周期
  内重启了 16 次。
- **`git checkout` 换分支 = 线上一批服务集体换版本，而且没人知道。**

NAS 至少还有 build 把版本钉住，Mac 连这个都没有。

## 为什么要有这份东西

盘了一遍，**四个仓库四套发布方式**：

| 仓库 | 源码怎么传 | 镜像标签 | 能不能回滚 |
|---|---|---|---|
| credvault | 受审 cutover | 源码 SHA-256，不可变 | ✅ |
| kg-hub | `tar` 工作区（2026-09-08 已禁用） | `:latest` | ❌ 坏的 |
| task-hub | `tar` 工作区 | 无 image 标签 | ❌ 无 |
| report-portal | `tar` 工作区（白名单） | `:latest` | ❌ 无 |

只有 credvault 是对的——因为它管钱，被逼着做对了。其余三个都有同一个毛病：

**打包的是工作区，不是 commit。** 本地哪个文件脏了就把脏的传上去，于是线上跑的
东西跟仓库里任何一个 commit 都对不上，出事时没法对账、没法在别的机器复现。

**镜像用可变标签。** 新镜像一 build 就把 `:latest` 覆盖掉，旧的变成悬空层。于是
"回滚"这件事**根本没有对象**——kg-hub 的通用部署 2026-09-08 被禁用（T-0046），
表面理由是"Compose 会删掉重命名的旧容器备份"，但那只是症状：正因为旧**镜像**留
不住，才不得不去抢救旧**容器**，才有了那套脆弱的备份机制。

## 通用铁律（不分平台）

**一、发布物是一个 commit，不是工作区。**
用 `git archive <sha>` 传输。那个 commit 必须已经推到 `origin`——否则线上跑着的
东西在任何人的仓库里都找不到。

**二、镜像标签就是那个 commit 的 sha，永不复用。**
`<name>:<sha12>`。**任何自建镜像都不许用 `:latest`。** 旧镜像留在盘上就是回滚的
全部依据。（第三方镜像如 `falkordb/falkordb` 不在此列，那是上游的事。）

**三、compose 硬引用标签，不接受隐式默认。**
写 `image: foo:${FOO_IMAGE_TAG:?...}`，不要 `:-latest`。少了变量就应当当场失败，
而不是悄悄发一个来路不明的镜像。标签值放 NAS 上的 `.env`，由发布脚本写。

> ⚠️ 改成硬引用会**影响别人**：在发布脚本把标签写进 `.env` 之前，任何人跑
> `docker compose` 都会因变量缺失而失败。所以脚本必须在源码落地后**立刻**把当前
> 标签补进 `.env`，把这个窗口关掉，再去做耗时的构建。

**四、回滚 = 把标签指回去，不是抢救容器。**
前提是容器不持有状态。kg-hub 的服务满足（模型、备份、refinery-state、
gateway-usage、device-liveness 全是 bind mount，图库是另一个不动的容器），所以
"从旧镜像重建"对它就是精确恢复。
**credvault 的模型网关不满足**：它有部署身份/见证语义，必须恢复到同一个容器实例，
那边继续用它自己那套受审 cutover，不要套用本准则。

**五、项目名固定，写进脚本，不靠记忆。**
多个 actor 用不同的 `-p` 项目名会各建一套容器抢同一个端口。
kg-hub = `kg-hub`，report-portal = `report-portal-src`。

## A. Docker 部署（NAS 与 Mac 通用）

**部署位置**

| | NAS | Mac |
|---|---|---|
| 源码落地 | `/volume1/docker/<项目>-src`（由 `git archive` 覆盖，**不是** git 检出） | 同形态，各项目自定 |
| 数据/状态 | `/volume2/4T/<项目>-data/*`，全部 bind mount | 各项目自定 |
| 标签变量 | 源码目录下的 `.env`，由发布脚本写 | 同 |

**部署流程**（`deploy/nas/release.sh` 就是这个流程的实现）

```
1. 校验 commit 已推到 origin        ← 不在远端就拒绝
2. 拿部署锁                          ← 同时只允许一个人在发
3. git archive <sha> → 源码目录      ← 发的是 commit，不是工作区
4. 立刻把当前标签补进 .env           ← 关掉「变量缺失」窗口
5. build -t <name>:<sha>            ← 不动 latest
6. 排空：停生产者，等在飞的做完       ← 见下
7. 写 .env 新标签 + up -d --no-build
8. 验收：镜像 ID 对得上 + 健康通过
9. 不通过 → 标签指回上一个 + up -d
```

**git 管理**：源码目录不是 git 检出（NAS 上没装 git，也不需要）。可追溯性靠
`.env` 里的标签 = commit sha，任何时候 `docker ps` 能反查到是哪一版。

### 只允许一个人在发

多 actor 工作区（claude-code / codex / 你本人）里，两个发布同时跑会互相覆盖
`.env`、抢同一批容器。脚本自己拿锁，不靠约定。崩溃留下的死锁超时后可被抢占。

### 换容器之前先排空

`docker compose up -d` 是直接替换容器，正在跑的请求会被掐断。对 kg-hub 这不只是
"重试一下"的事：抽取是**流式**的，中途断开会在模型网关留下 `unknown` 记录，而那种
记录**永远不会过期**（网关只删 completed/error，因为过期不能证明供应商没扣过钱），
每一条都挡住下一次发布。2026-09-10 六小时攒了 171 条。

**也就是说：不排空的发布，会自己制造挡住下次发布的东西。**

做法：先停生产者（refinery / ingester），再等服务端的 `active_extractions` 归零，
然后才换。这个字段必须由服务端暴露——没有它就只能盲等一个路由超时。

**等的是在飞的抽取，不是积压。** 积压有几千条、要跑几个月，等积压等于永远发不了。
在飞条数由 `INGEST_CONCURRENCY` 封顶（现为 2），每条约 3 分钟，所以正常最坏等
3 分钟左右。积压躺在库里和水印里，生产者停了就停，回来接着跑，一条不丢。

**排空不掉就别发。** 硬换只是把这次发布的代价转嫁成永久记录。此刻源码已同步、
镜像已构建，但容器还没换，中止是干净的，过会儿重跑即可。确实需要掐断时用
`KG_HUB_FORCE_SWAP=1` 明确表态——默认安全，例外必须写出来。

**生产者停下之后，任何一条失败路径都必须把它们起回来。** 否则发布失败会连带把
整条采集静悄悄停掉，那比发布失败本身严重得多。脚本用 trap 兜住。

### 验收与失败处置

验收要证明**跑的确实是刚建的那个镜像**：比对 `docker image inspect` 的 ID 和容器
实际的 `.Image`。只 curl `/health` 是不够的——那只证明"有个东西在听"，compose 完全
可能压根没重建容器。

不通过就自动把标签指回上一个并重新 `up`——旧镜像还在盘上，这一步一定做得成。
脚本必须如实报"已回滚"，不许谎报成功。

### 哪些没从 credvault 抄

credvault 的 cutover 有约 60 个函数。没抄的：人工确认口令、配置快照事务、精确容器
身份记录（容器 ID / 网络 ID / 启动随机数原子落盘）、排空后的 admission 恢复信号。

那些是为「管钱、且必须恢复到**同一个容器实例**」设计的。kg-hub 的容器不持有状态，
恢复到"同一个镜像的新容器"就是精确恢复，套过来只是负担。**这个判断依赖于容器无
状态——哪天 kg-hub 的容器开始自己存东西了，这条要重新评估。**

## B. 本机常驻服务部署（Mac / Windows）

**目前是裸奔状态，这一章是目标形态，尚未实施。**

**铁律：常驻服务不许指向 git 工作区。** 工作区是用来改代码的，不是用来跑生产的。

**部署位置**

| | Mac | Windows |
|---|---|---|
| 发布根 | `~/.local/<项目>/releases/<sha>/` | `%LOCALAPPDATA%\<项目>\releases\<sha>\` |
| 当前版本指针 | `~/.local/<项目>/current` → `releases/<sha>` 符号链接 | 同名目录联接（junction） |
| 服务定义 | `~/Library/LaunchAgents/com.<项目>.<服务>.plist`，**只准指向 `current/`** | 计划任务 / 服务，同样只指 `current\` |
| 可变状态 | `~/.<项目>/`，与发布根分开，换版本不动它 | `%APPDATA%\<项目>\` |

**部署流程**

```
1. 校验 commit 已推到 origin
2. git archive <sha> → releases/<sha>/       ← 新目录，不碰正在跑的那个
3. 在新目录里跑一次自检（能 import / --version 之类）
4. current 原子切到 releases/<sha>            ← ln -sfn 到临时名再 mv
5. 重启服务（launchctl kickstart -k / 重启计划任务）
6. 验收：服务起来了，且能报出自己的 commit
7. 不通过 → current 切回上一个 + 重启
```

**git 管理**：同样只发已推送的 commit。旧的 `releases/<sha>` 保留最近若干个——
它们就是回滚的对象，跟 Docker 那边留旧镜像是同一件事。

**为什么不直接 `git pull` 到一个检出目录**：那样 `git checkout` 一次就会把所有
服务一起换版本，而且工作区脏了照样上线——等于把现在这个洞换个地方挖。
发布必须是**只增不改**的目录 + 一次指针切换。

## C. 客户端与工具分发（跨设备）

**服务端是唯一真源。** task-hub 已经这么做了（`redeploy.sh` 里写着「客户端随服务端
一起分发：服务端是唯一真源，避免各设备版本漂移」），照抄这条即可。

各设备不许各自 `git pull` 一份客户端——那正是版本漂移的来源。Windows 目前只有
MCP 客户端接入（T-0050），先按这一条办，别提前套 B 章那套常驻服务的东西。

## 落地位置速查

| 东西 | 在哪 | 谁写 |
|---|---|---|
| NAS 容器源码 | `/volume1/docker/<项目>-src` | 发布脚本（git archive） |
| NAS 容器数据 | `/volume2/4T/<项目>-data/*` | 服务自己 |
| NAS 镜像标签 | 源码目录 `.env` 的 `*_IMAGE_TAG` | 发布脚本 |
| Mac 本机发布 | `~/.local/<项目>/releases/<sha>` + `current` | 发布脚本（目标形态） |
| Mac 服务定义 | `~/Library/LaunchAgents/com.<项目>.*.plist` | 人工，只准指 `current/` |
| Mac 可变状态 | `~/.<项目>/` | 服务自己 |
| Win 本机发布 | `%LOCALAPPDATA%\<项目>\releases\<sha>` + `current` | 目标形态，未实施 |

## 参考实现

`deploy/nas/release.sh`（kg-hub）。用法：

```
deploy/nas/release.sh            # 发布 HEAD
deploy/nas/release.sh <commit>   # 发布指定 commit
deploy/nas/release.sh --rollback # 回到上一次的标签
DRY_RUN=1 deploy/nas/release.sh  # 只打印，不碰 NAS
```

## 这份准则是被测试钉住的

`tests/test_deployment_standard.py` 检查第二、三条在 kg-hub 里成立。**光写文档会
漂**——半年后没人记得为什么不能用 `:latest`，然后就有人加回去了。

## 还没收口的（照实记，别当已完成）

| 项 | 现状 | 归属 |
|---|---|---|
| kg-hub NAS 发布 | ✅ 已按本准则 | 本文参考实现 |
| credvault 模型网关 | ✅ 自有受审 cutover，**不套用本准则**（它必须恢复到同一个容器实例） | codex |
| task-hub NAS 发布 | ❌ 仍 tar 工作区、无 image 标签、无回滚 | task-hub 自己的任务 |
| report-portal NAS 发布 | ❌ 仍 tar 工作区、`:latest`、无回滚 | report-portal 自己的任务 |
| **Mac 本机 11 个 launchd 服务** | ❌ **全部直接跑 git 工作区**，B 章尚未实施 | 需与 codex 协调（其中 3 个是 credvault 的） |
| Mac 上的 docker 容器 | ❌ 已有两个跑无标签镜像 | recruitment-* 属另一项目 |
| Windows | 无服务，只有 MCP 客户端待接入 | T-0050 |

改这些属于各自的任务，本准则先立在这里作为目标形态。**没做的就写没做，不许在
文档里假装已经统一了** —— 一份说谎的准则比没有准则更糟，因为别人会照着它假设。
