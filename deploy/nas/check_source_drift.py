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

REPO = Path(__file__).resolve().parent.parent.parent
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
        bad.append((name, "备份杂物" if looks_like_backup(name)
                    else "只在 NAS 上，git 未跟踪"))
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
    ap.add_argument("--status-file", help="把一行判决写到这里，供 SessionStart hook 读")
    ap.add_argument("--ref", help="指定要比对的 commit（默认取线上 .env 的镜像标签）")
    args = ap.parse_args(argv)

    config = release_config()
    ssh_target, src = config["ssh"], config["src"]
    ref = args.ref or live_commit(ssh_target, src)
    short = ref[:12]

    try:
        subject = subprocess.run(
            ["git", "-C", str(REPO), "log", "-1", "--format=%ad %s",
             "--date=format:%m-%d %H:%M", ref],
            capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        subject = "(本地仓库里没有这个 commit)"

    bad = compare(ssh_target, src, ref)
    if not bad:
        detail = f"{short} {subject}"
        print(f"✅ NAS 源码等于 {detail}")
        write_status(args.status_file, "ok", detail)
        return 0

    # 危险的排前面：内容对不上、该有却没有、git 里删掉却还在生产上跑。
    # 备份杂物单独归堆 —— 混在一起看，真信号会被 15 行噪音淹掉。
    junk = [x for x in bad if x[1] == "备份杂物"]
    real = [x for x in bad if x[1] != "备份杂物"]
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
