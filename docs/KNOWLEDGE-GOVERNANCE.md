# 知识库统一治理方案 — OpenClaw ↔ NAS kg-hub

> 2026-07-23 定稿。回答一个问题:**两边的知识库怎么管,才既不重复建设、也不互相污染。**
> 原则:**联邦,不合并**。各库干各自擅长的事,中间用管线+质量闸门连接。

## 1. 全景架构(谁存什么)

```
OpenClaw (oc-vps, 7×24)                          NAS kg-hub(中央图, 7×24)
─────────────────────                            ─────────────────────────
~/clawd/notes/capsules ← kb-001 04:00 提炼        Episodic/Entity/RELATES_TO
        │                                        ├─ openclaw-capsule-*(知识胶囊)
kg-push-capsules.py(cron 30min,确定性脚本)        ├─ claude-mem obs(coding 会话,Mac 推)
        └────── POST /api/ingest ───────────────▶├─ kg-hub-canonical-*(注入胶囊×9)
                 │格式门:无**来源**→隔离区         └─ 案例包 / ArticleFeedback / ToolStat
                 │入图即自动打 provenance                  │
        ◀────── kg-query.sh(读,external 沉底)──────────────┘
其余 1000+ 笔记(原料)不进图;main.sqlite=空壳未启用 ⚠
```

**单一事实源**:跨工具共享的知识,以 **NAS 图为准**;OpenClaw 的 notes 是它自己的工作区(原料+成品混放),不是共享层。

> **2026-07-23 直推割接**:原"VPS→Mac 拉取→Mac ingest"与隐藏的"VPS root cron :19 tar→NAS 容器 600s 轮询"**两条并行旧线曾双写重复入图**,同日全部退役(Mac plist 在 retired-launchagents/,VPS root crontab 注释行,容器循环只剩 canonical)。数据在哪从哪推、图在哪抽取在哪跑;Mac 不再是 OpenClaw 数据面的一环。
>
> **2026-07-23 历史重复去重**:33 对同名同内容胶囊(6 月原始入图 + 7-22 gz 支持上线重入图)已去重,105→72。用 `graphiti.remove_episode` 移除每对较新副本(保留较早、时间锚更准),并剔除幸存边 `episodes` 数组里的陈旧 uuid;零孤儿边。全文备份 `data/dedup-backup-20260723.json`,工具 `tools/dedup_capsules_once.py`。

## 2. 术语表(强制,消除歧义)

| 术语 | 是什么 | 数量级 | 在哪看 |
|---|---|---|---|
| **注入胶囊** (canonical) | kg-hub 项目预热文档,SessionStart 由 PUSH hook 注入,有 usage 排序 | 9 个 | 注入胶囊看板 `/dashboard/capsules` |
| **知识胶囊** (OpenClaw capsule) | OpenClaw 每日自动提炼的知识/教训,进图可搜**不可注入** | ~70+,每天增长 | 知识库速览 `/dashboard/knowledge` 搜 |

看板、文档、对话里**禁止**单说"胶囊"指代其中一种;必须带前缀。

## 3. 数据流与质量闸门

### 写入(OpenClaw → NAS,全自动)
1. `com.kg-hub.openclaw-sync`(Mac launchd,1800s)拉 `notes/ memory/ plans/ reports/ capsules/` 快照。
2. `ingesters/openclaw_capsule.py` **只认 `CAPSULE-*/capsule-*` 的 .md 和 .md.gz**,其余笔记一律不进图(原料不进案例库)。
   - gz 支持是 2026-07-23 补的:此前 29 个胶囊因"成功 ingest 前被 OpenClaw 原地 gzip"而**静默丢失**(整个 6 月 daily-extract + 07 月初 + 07-17/18 批)。水印按**解压后内容 sha** 记账,md→gz 改名不会重复入图(dedup-alias)。
3. `tools/tag_provenance.py` 幂等补标(sync 每轮 ingest 后自动跑):

| provenance | 含义 | 判定(来源行,精确优先) | 待遇 |
|---|---|---|---|
| `firsthand` | 亲历:MEMORY.md 提炼、cron 日志、投资报告、会话复盘 | 默认 | 正常 |
| `external-article` | 公众号/掘金/博客**文章派生**,二手未验证 | `文章《` | 降权沉底 + 📰 badge + verified=false |
| `external-community` | 小红书/知乎/微博等社区采集 | 平台关键词 | 同上 |

### 读取(OpenClaw ← NAS)
- `kg-query.sh` → `/api/search`(env.sh 已于 2026-07-23 修至 NAS `:17171`;此前指向已退役的 mac-office:8080,**读通道休眠了一个月**——只在 5/18、5/19、6/21 被真正用过)。
- `/api/search` 现返回每条 fact 的 `provenance`(internal/external),external **稳定沉底但不过滤**——亲历知识排前面,二手可见可查。(实现:超采 3 倍候选再排序截断;极端分布下 external 仍可能占满结果,是缓解不是保证。)
- **注入隔离天然成立**:SessionStart 只注入 canonical,知识胶囊/外部内容永远不会被自动灌进任何会话上下文。

### 人工闸门(唯一需要人的地方)
反馈待办 `/dashboard/inbox` 自动列队:AI 预判可见性 → 人一键确认 `visibility` / `verified` → 整理台合成案例包 → 运营反馈录表现。📰 外部 badge 帮你识别二手内容,**别把文章派生的"知识"误标为已验证**。

## 4. 明确不做的事(边界)

- **不合并两库**:OpenClaw notes 里 1000+ 原料笔记不进图。图是案例库不是素材矿(见 LANDING-PLAN-cognitive-asset.md)。
- **不接 main.sqlite**:OpenClaw 内置向量记忆从未启用(0 chunk)。接上要动 gateway 配置+重启,违反"不重启 gateway"铁律,且现有 markdown notes + kg-query 已覆盖需求。**留空文件,当它不存在**;VPS 侧 `~/clawd/notes/knowledge-architecture.md` 已注明,防后人误判。
- **不自动打分胶囊贡献度**:结论见 docs/CONTRIBUTION-SIGNAL.md,不可自动测,人工季度扫。

## 5. 运维手册(出问题看哪)

| 症状 | 看哪 | 常见原因 |
|---|---|---|
| 图里搜不到新知识胶囊 | `~/.kg-hub/logs/openclaw-sync.out.log` | NAS FalkorDB 冷启动(err.log 有 not ready 重试,自愈);LLM 限频 |
| OpenClaw 查不到图 | oc-vps 上跑 `~/clawd/scripts/kg-query.sh test 1` | env.sh 的 KG_HUB_URL/TOKEN;tailscale |
| 外部内容没有 📰 标 | Mac 上 `python -m tools.tag_provenance --dry-run` | 新来源写法没命中规则 → 改 utils/provenance.py 后 `--force` 重标 |
| 部署 server 前 | **必查漂移**:NAS `/volume1/docker/kg-hub-src/kg_hub_server.py` sha256 == `git show HEAD:kg_hub_server.py` sha | 两会话共用工作树 |

## 6. 已知余留

- 胶囊真实产线是 OpenClaw **内置 cron 的两个 LLM job**:`kb-001 胶囊提取`(每日 04:00,写 CAPSULE-*)与 `evo-002 记忆压缩`(每日 05:00,gzip 归档,标准逐日即兴——"假 .md.gz" 即它所致)。
  - **kb-001 prompt 已于 2026-07-23 收紧**(经 `openclaw cron edit`,首连可能握手闪断、重试即通):固定 `**来源**:` 模板(禁标题式/缩写/路径别名)、禁自家文章回灌、禁流水账占胶囊命名空间、成囊前 14 天查重。改前备份 `jobs.json.bak-kb001-20260723-*`。
  - `session-knowledge-manager` 技能是**僵尸**(hook 从未被 gateway 加载、cron 行 4 月已删、核心模块全是桩、宣称的功能实为 kb-001/evo-002 完成),**已于 2026-07-23 退役**:tar 备份 `~/clawd/backups/skm-retire-20260723.tar.gz`,原目录挪至 `~/clawd/backups/retired-20260723/`(含 730 个路径 bug 存根)。恢复=mv 回去。
- 胶囊来源行格式会**漂移**(行首式→列表式→标题式)。消费端分类器已双保险:兼容 `## 来源` 标题式、`**数据来源**:` 变体、xhs/social-search 别名(2026-07-23,全语料回归零误伤)。新漂移形态出现时,改 `utils/provenance.py` 后 `python -m tools.tag_provenance --force` 重标。
- `docs/REPORTS.md` / `docs/PORTAL-HANDOFF.md` 属门户会话维护,勿在本会话改。
