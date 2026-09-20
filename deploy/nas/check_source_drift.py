#!/usr/bin/env python3
"""NAS 上跑的 kg-hub 源码，是不是等于 git 里的某个 commit。

## 为什么要有它

2026-09-18：credvault 网关的构建源不是 git 检出，生产 `model_gateway.py` 有约
1400 行从未提交，两条线各自实现了同一个功能、双方都不知道对方做过。更早的
2026-09-07 还做过一次「全量纳入版本控制」的快照 —— **十天后又漂了 1400 行**。
只补快照不建检测无效。

kg-hub 此前完全没有这个检测：它不知道 NAS 上跑的是不是 git 里的东西。

## 与网关那份的区别

网关的指纹只覆盖 8 个构建输入，于是 `deploy/` 下**不进镜像、却在 NAS 上被直接
执行**的脚本落在覆盖范围之外（实测 `nas-compose-control.sh` 领先 git 85 行，而
当天的漂移检测显示「✅ 对得上」）。

kg-hub 没有这个洞：`release.sh` 用 `git archive <sha>` 发**整棵树**，运维脚本
天然在内。所以这里的清单就是那个 commit 的全部跟踪文件。

## `ok` 断言的是什么（只有一个答案）

**ok = NAS 上的文件等于某个 commit，且那个 commit 在 `origin/main` 这条线上。**

两个条件缺一不可。2026-09-21 实测四个兄弟服务的 `ok` 有三种含义，而 fleet-ops
的巡检把它们渲染成同一条绿（T-0099）：

    report-portal / task-hub   ok = 等于 origin/main
    kg-hub（补这段之前）        ok = 等于它自称的那个 commit —— 任何分支都行
    credvault                  ok = 等于最近 60 提交里某一个（git log --all）

后两种漏掉的正是准则 18：线上跑着一个不在主干上的版本。那种情况下下一个人从
主干出发做的任何事（发布、回滚、比对漂移）都会把它悄悄抹掉，**而且不会有冲突
提示，因为 main 从来就不知道它存在**。这条闸本来只活在 `release.sh` 里，而
**绕过发布脚本改生产**恰恰是这套检测存在的全部理由（credvault 实测被绕过两次，
准则 21）。闸和检测同时只剩一个的时候，剩下的那个是检测。

用 `merge-base --is-ancestor` 而不是「等于 origin/main」：回滚到一个更早的
commit 是正当操作，只要它确实在主干这条线上。

## 三态，不是两态

`ok` / `drift` / `error`。补这段之前 kg-hub 只有前两个：失败路径全是 `SystemExit`，
在写状态之前就退了，于是 ssh 一挂，状态文件就停在上一条判决 —— 消费侧要等 36 小时
年龄阈值才发现。**最长 36 小时的陈旧绿灯**，而那正是消费侧注释里写着要防的事。

## 两条必须保留的设计（照抄容易丢）

**一、清单和路径都从 `release.sh` 解析，不在这里抄第二份。**
否则那边改了发布范围、这边还按老规矩算，会静悄悄「对上」一个其实不同的版本 ——
而「两份清单各自漂」正是本文件要治的病。解析不出来就**拒绝运行**，不退回猜测。

**二、两侧都要列，取并集。**
只按 git 清单查，永远看不见「只在生产上存在」的文件 —— 那是最危险的一种漂移。
对 kg-hub 还多一层理由：`release.sh` 是逐文件 `mv` 覆盖、**从不删除**，所以从
git 里删掉的文件会永远留在 NAS 上继续被执行。

## 排除法在这里是安全的

扫描用黑名单，取样才用白名单 —— 按「漏掉的后果」选，不按形式选。这里漏排一个
模式只会**多报**一个文件让人来看；反过来用白名单，漏掉的就是永远看不见的那类。

用法：
    deploy/nas/check_source_drift.py                         # 查一次
    deploy/nas/check_source_drift.py --status-file ~/.cache/kg-hub/source-drift.status
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 这个工具的**输入**是 git 仓库本身（它要回答「线上跑的对应哪个 commit」），
# 所以仓库路径是数据，不是代码来源。二者必须能分开：Mac 侧作业改成跑发布产物之后，
# 产物里没有 .git —— 不分开的话，这个工具就只能继续跑工作树。
REPO = Path(__file__).resolve().parent.parent.parent
RELEASE = REPO / "deploy" / "nas" / "release.sh"


def set_repo(path) -> None:
    """覆盖被检查的仓库。默认是脚本自己所在的那棵树。"""
    global REPO, RELEASE
    REPO = Path(path).resolve()
    RELEASE = REPO / "deploy" / "nas" / "release.sh"

# NAS 上允许存在、但 git 里本来就不该有的东西。每一条都要能说出为什么，
# 否则就是在给真正的漂移留藏身处。
#
# 刻意**没有**排除的：`*.pre-*-<日期>` 这类手工备份留在仓库根上（实测有一个
# `kg_hub_server.py.pre-boundaries-20260820-1544` 就躺在真文件旁边）。多报一个
# 让人来删，比给它一条永久豁免好。
# 三类分开写，不用一把「子串匹配」糊过去。
# 2026-09-18 实测教训：原来写成 `pattern in name`，`.env` 那条把**仓库真正跟踪
# 的** `deploy/nas/.env.example` 一起吞了 —— 它要是在 NAS 上漂了，永远看不见。
# 豁免是这套检测里唯一能藏住漂移的地方，宁可写长也要精确。
EXACT_BASENAMES = (".env", ".DS_Store")          # 整个文件名恰好是这个
BASENAME_PREFIXES = ("._",)                      # macOS 资源叉，SMB 写入的噪音
BASENAME_SUFFIXES = (".pyc", ".pyo")             # 运行产物
PATH_COMPONENTS = ("__pycache__",)               # 整个目录都是运行产物


def release_config(path: Path = RELEASE) -> dict[str, str]:
    """从 release.sh 解析出 ssh 目标与源码目录。

    不在这里写第二份默认值：那边一改，这里就会对着错的目录报「一切正常」。
    """
    if not path.exists():
        raise SystemExit(f"找不到发布脚本 {path}；先确认发布方式再改这里")
    text = path.read_text("utf-8")
    config = {}
    for key, var in (("ssh", "KG_HUB_NAS_SSH"), ("src", "KG_HUB_NAS_SRC")):
        match = re.search(rf'\$\{{{var}:-([^}}]+)\}}', text)
        if match is None:
            raise SystemExit(
                f"在 {path.name} 里解析不出 {var} 的默认值；发布脚本结构可能变了，"
                "先确认再改这里 —— 不要退回一个猜出来的值继续查")
        config[key] = match.group(1).strip()
    # 发布方式本身也要确认：清单是「整棵树」这件事依赖它用 git archive。
    if "git archive" not in text:
        raise SystemExit(
            f"{path.name} 看起来不再用 git archive 发布；本检测按「整棵树」算清单，"
            "发布范围变了必须同步改这里")
    return config


def is_allowed_untracked(name: str) -> bool:
    """这个「只在 NAS 上」的文件该不该报。

    根下的点目录整体豁免：那里全是历次部署脚本留下的备份与暂存
    （`.deploy-backups/`、`.effective-quota.XXXXXX/`、`.dashboard-health.XXXXXX/`
    …实测 72 个文件）。这条排除是**精确**的而不是省事 —— git 在任何点目录下都没有
    跟踪文件，只跟踪 `.dockerignore` 和 `.gitignore` 两个点**文件**，所以它藏不住
    任何本该被跟踪的东西；那两个点文件改了照样报。
    """
    head, _, rest = name.partition("/")
    if rest and head.startswith("."):
        return True
    base = name.rsplit("/", 1)[-1]
    parts = name.split("/")
    return (base in EXACT_BASENAMES
            or base.startswith(BASENAME_PREFIXES)
            or base.endswith(BASENAME_SUFFIXES)
            or any(component in PATH_COMPONENTS for component in parts))


# 一眼能认出是备份的后缀。这些**照样要报**，只是排在后面 —— 它们是旧部署脚本
# 留下的一次性杂物（实测 15 个，其中一个叫 `kg_hub_server.py.bak.$(date +%s)`，
# 当年某条备份命令的引号写错了），清掉就没了，不该和「git 里删掉却还在生产上
# 跑」的那类混在一起看：后者可能还被别处引用着。
BACKUP_SUFFIXES = (".bak", ".pre-", ".orig", ".save")


def looks_like_backup(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return (name.startswith("backups/") or "/backups/" in name
            or any(marker in base for marker in BACKUP_SUFFIXES))


def live_commit(ssh_target: str, src: str) -> str:
    """线上正在跑的那个 commit。取自 release.sh 自己写的 .env，不是我们猜的。"""
    proc = subprocess.run(
        ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", ssh_target,
         f"grep '^KG_HUB_IMAGE_TAG=' '{src}/.env' 2>/dev/null | head -1 | cut -d= -f2-"],
        capture_output=True, text=True)
    value = proc.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", value):
        raise SystemExit(
            f"读不到线上镜像标签（拿到 {value!r}）：{proc.stderr.strip() or '无输出'}")
    return value


def fetch_origin() -> bool:
    """把 origin 拉新。失败返回 False。

    判「在不在主干上」之前必须拉一次：本地 origin/main 陈旧的话，一个已经合进
    主干的 commit 会被判成「不在主干上」—— 那是这条检测里最响的一档警报，
    拿它去报一件不存在的事，等于把警报变成噪音（准则 28）。
    """
    proc = subprocess.run(["git", "-C", str(REPO), "fetch", "origin", "--quiet"],
                          capture_output=True, text=True)
    return proc.returncode == 0


def on_trunk(ref: str, trunk: str = "origin/main") -> bool | None:
    """ref 在不在主干这条线上。本地没有这个 commit 时返回 None。

    用 `merge-base --is-ancestor` 而不是「等于 trunk」：**回滚到一个更早的
    commit 是正当操作**，只要它确实在主干这条线上（准则 18 原话）。
    写成相等的话，每一次正当回滚都会被报成漂移。
    """
    if subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{ref}^{{commit}}"],
                      capture_output=True).returncode != 0:
        return None
    return subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", ref, trunk],
        capture_output=True).returncode == 0


def tracked_at(ref: str) -> list[str]:
    """该 commit 的全部跟踪文件 —— 就是 git archive 会发出去的那些。"""
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", ref],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"本地仓库里没有 {ref}：先 git fetch，或线上跑着一个"
                         f"没推上来的版本（那本身就是最严重的漂移）")
    return [x for x in proc.stdout.splitlines() if x.strip()]


def remote_listing(ssh_target: str, src: str) -> list[str]:
    """NAS 上实际存在的文件（去掉允许的非跟踪项）。"""
    proc = subprocess.run(
        ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", ssh_target,
         f"cd '{src}' 2>/dev/null && find . -type f 2>/dev/null"],
        capture_output=True, text=True)
    out = []
    for line in proc.stdout.splitlines():
        name = line.strip().removeprefix("./")
        if name and not is_allowed_untracked(name):
            out.append(name)
    return out


def remote_hashes(ssh_target: str, src: str, names: list[str]) -> dict[str, str]:
    """一次 ssh 取回这些文件在 NAS 上的内容摘要。取不到的键不出现。"""
    if not names:
        return {}
    safe = [n for n in names if "'" not in n]
    got: dict[str, str] = {}
    # 清单可能上千条，命令行有长度上限；分批发。
    for i in range(0, len(safe), 250):
        quoted = " ".join(f"'{n}'" for n in safe[i:i + 250])
        proc = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", ssh_target,
             f"cd '{src}' 2>/dev/null && for f in {quoted}; do "
             f"[ -f \"$f\" ] && printf '%s %s\\n' "
             f"\"$(sha256sum \"$f\" | cut -d' ' -f1)\" \"$f\"; done"],
            capture_output=True, text=True)
        for line in proc.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                got[parts[1]] = parts[0]
    return got


def git_hash(ref: str, name: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:{name}"],
                          capture_output=True)
    return hashlib.sha256(proc.stdout).hexdigest() if proc.returncode == 0 else None


# compare() 里代表「NAS 上多出来、git 里没有」的两个标签。--list-extra 按它取，
# 报告也按它分堆 —— 「该删什么」与「报什么漂移」由此出自同一处定义，不会各自演化。
EXTRA_REASONS = ("只在 NAS 上，git 未跟踪", "备份杂物")


def compare(ssh_target: str, src: str, ref: str) -> list[tuple[str, str]]:
    """返回 [(文件, 问题)]。只报「不一致」，不猜方向。

    判断谁领先需要知道分叉基点，那是人该看的；这里只负责别让差异静悄悄过去。
    """
    names = tracked_at(ref)
    on_nas = remote_listing(ssh_target, src)
    # 只取跟踪文件的哈希就够：NAS 独有的那些是按**存在与否**报的，不比内容
    # （git 里根本没有对照物）。
    #
    # 「两侧取并集」这条设计落在上面两行 + 下面那个额外循环上，**不在这里**。
    # 2026-09-18 做变异验证时发现：把这里改成只按 git 清单取，测试仍然全绿 ——
    # 说明那个并集是空转的。真正承重的是 remote_listing 把 NAS 侧也列一遍。
    # 留这行只会让人以为安全性在它身上，删掉时再无人拦。
    remote = remote_hashes(ssh_target, src, sorted(names))
    bad: list[tuple[str, str]] = []
    for name in names:
        theirs = remote.get(name)
        if theirs is None:
            bad.append((name, "NAS 上没有"))
            continue
        ours = git_hash(ref, name)
        if ours is not None and theirs != ours:
            bad.append((name, "内容不一致"))
    for name in sorted(set(on_nas) - set(names)):
        # 最危险的一种：生产在跑，git 里连文件都没有。对 kg-hub 还有第二种来源 ——
        # release.sh 从不删除，所以 git 里删掉的文件会一直留在 NAS 上被执行。
        bad.append((name, EXTRA_REASONS[1] if looks_like_backup(name)
                    else EXTRA_REASONS[0]))
    return bad


def write_status(path: str | None, verdict: str, detail: str) -> None:
    """写一行判决 + 写入时刻。

    巡检要 ssh 打 NAS，跑在 launchd 里，hook 只读这个文件。所以**时刻必须一起
    写**：检查本身停掉的时候，读的人要能看出「这条绿是三天前的」，而不是当成
    现在还绿 —— 新鲜的时间戳盖着陈旧的数字，2026-09-18 一天栽了三次。
    """
    if not path:
        return
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t"
            f"{verdict}\t{detail}\n", "utf-8")
    except OSError as exc:   # 写不动不该把巡检本身弄失败
        print(f"   （状态文件写入失败：{type(exc).__name__}）", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="kg-hub NAS 源码漂移检测")
    ap.add_argument("--repo", default=None,
                    help="被检查的 git 仓库；默认取脚本自身所在的那棵树。"
                         "作业跑发布产物时必须显式给出——产物里没有 .git。")
    ap.add_argument("--status-file", help="把一行判决写到这里，供 SessionStart hook 读")
    ap.add_argument("--ref", help="指定要比对的 commit（默认取线上 .env 的镜像标签）")
    ap.add_argument("--skip-trunk", action="store_true",
                    help="跳过「线上 commit 在不在主干上」这一判。只给离线/测试用——"
                         "常规巡检不要带，带上就退回补这段之前的语义")
    ap.add_argument("--list-extra", action="store_true",
                    help="只列「NAS 上有、git 里没有」的文件，每行一个，供 release.sh "
                         "在发布时打印（观察期只打印不删）。清单与漂移报告取自同一处"
                         "定义（EXTRA_REASONS），所以「该删什么」与「报什么漂移」"
                         "不可能各自演化")
    args = ap.parse_args(argv)
    if args.repo is not None:
        set_repo(args.repo)

    config = release_config()
    ssh_target, src = config["ssh"], config["src"]
    ref = args.ref or live_commit(ssh_target, src)
    short = ref[:12]

    # 「ok」断言的是什么，必须只有一个答案 —— 而且必须是「等于主干这条线上的某个
    # commit」。补这段之前，这里的 ok 只说明「NAS 上的文件等于它自称的那个 commit」，
    # 那个 commit 可以在任何分支上、甚至从没推上来过。于是准则 18（只发主干）在这条
    # 检测里查不出来，它只活在 release.sh 的闸上 —— 而**绕过发布脚本改生产**正是这套
    # 检测存在的全部理由（credvault 实测被绕过两次，准则 21：绕过一次，正式发布路径
    # 本身也会失效）。闸和检测同时只剩一个的时候，剩下的那个是检测。
    #
    # 2026-09-21 实测四个服务的 ok 有三种含义，而巡检把它们渲染成同一条绿（T-0099）。
    # --list-extra 是纯列举模式：它给 release.sh 提供机器可读的清单，不出判决
    # （既有用例钉着「--list-extra 永远不写 verdict」）。主干这一判属于判决，
    # 不该挡住一个只读清单的调用。
    trunk_checked = not args.skip_trunk and not args.list_extra
    on_main = None
    if trunk_checked:
        if not fetch_origin():
            # 拉不到 origin 就判不了「在不在主干上」。此时**不许出 ok**：
            # 拿陈旧的主干去判，一个已经合进主干的 commit 会被报成不在主干上。
            detail = f"{short} 拉不到 origin，判不了在不在主干上，本次不作判决"
            print(f"🟠 {detail}")
            write_status(args.status_file, "error", detail)
            return 2
        on_main = on_trunk(ref)
        if on_main is None:
            # 拉过 origin 之后本地仍然没有这个 commit —— 线上跑的东西在远端**根本
            # 不存在**。这是最严重的一档：连"它漂没漂"都无从谈起，因为没有对照物。
            detail = f"{short} 在 origin 上根本不存在（线上跑着一个没推上来的版本）"
            print(f"❌ {detail}")
            print("   先把线上那份取回来建快照分支，再决定怎么合 —— "
                  "在那之前任何比对都是对着空气比。")
            write_status(args.status_file, "drift", detail)
            return 1

    try:
        subject = subprocess.run(
            ["git", "-C", str(REPO), "log", "-1", "--format=%ad %s",
             "--date=format:%m-%d %H:%M", ref],
            capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        subject = "(本地仓库里没有这个 commit)"

    bad = compare(ssh_target, src, ref)

    if trunk_checked and on_main is False and not args.list_extra:
        # 文件对不对得上它，在这里是次要的。主干上没有这个版本，意味着下一个人
        # 从 main 出发做的任何事（发布、回滚、比对漂移）都会把它悄悄抹掉，
        # **而且不会有冲突提示，因为 main 从来就不知道它存在**（准则 18 原话）。
        detail = (f"{short} 不在 origin/main 这条线上"
                  + (f"；另有 {len(bad)} 处文件差异" if bad else "，文件本身与它一致"))
        print(f"❌ 线上跑的 commit 不在主干上：{short} {subject}")
        print(f"   {detail}")
        print("   处置：把它合回主干，或从主干重新发一次。")
        write_status(args.status_file, "drift", detail)
        return 1

    if args.list_extra:
        # 机器可读、只此一样东西：不写状态文件、不打判决。观察期的唯一产物就是这份
        # 清单，谁要删由调用方决定——本脚本永远只读不写 NAS。
        for name, _ in [x for x in bad if x[1] in EXTRA_REASONS]:
            print(name)
        return 0

    if not bad:
        detail = f"{short} {subject}"
        print(f"✅ NAS 源码等于 {detail}")
        write_status(args.status_file, "ok", detail)
        return 0

    # 危险的排前面：内容对不上、该有却没有、git 里删掉却还在生产上跑。
    # 备份杂物单独归堆 —— 混在一起看，真信号会被 15 行噪音淹掉。
    junk = [x for x in bad if x[1] == EXTRA_REASONS[1]]
    real = [x for x in bad if x[1] != EXTRA_REASONS[1]]
    if not real:
        # 只剩杂物：如实说，但别报成实质漂移 —— 否则这条检查永远红，而永远红的
        # 检查等于没有，没人会再看它。
        detail = f"{short} 无实质漂移，另有 {len(junk)} 个备份杂物待清"
        print(f"✅ NAS 源码等于 {short} {subject}")
        print(f"   （另有 {len(junk)} 个备份杂物待清，如 {junk[0][0]}）")
        write_status(args.status_file, "ok", detail)
        return 0
    print(f"❌ NAS 源码与 {short} 有 {len(real)} 处实质不一致"
          + (f"，另有 {len(junk)} 个备份杂物" if junk else "") + "：")
    for name, why in real[:20]:
        print(f"   {name}  —  {why}")
    if len(real) > 20:
        print(f"   …还有 {len(real) - 20} 处")
    if junk:
        print(f"   备份杂物（清掉即可）：{junk[0][0]} 等 {len(junk)} 个")
    write_status(args.status_file, "drift",
                 f"{short} {len(real)} 处实质不一致：" +
                 "、".join(n for n, _ in real[:3]) + ("…" if len(real) > 3 else ""))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
