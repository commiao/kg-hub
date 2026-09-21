#!/usr/bin/env python3
"""NAS `.env` 里的「非机密配置」不许和 git 说的不一样。

## 为什么

准则 19 的判据**不是**「把 .env 放进 git」，而是逐键问两句：改了线上行为会变吗？
它含机密吗？「会变 + 不含」才必须进 git。2026-09-21 逐键核过，12 个键里 5 个是
非机密行为配置，它们的默认值 **2026-09-20 的 87a2dad 已经搬进 docker-compose.yml**。

但那次只搬了值，**`.env` 里那几行没删**。现在两边恰好一样，所以看不出问题 ——
任何人在 NAS 上改一下，git 就又开始说假话，而那正是 87a2dad 要治的病。

准则 2：检测与机制缺一不可。只搬值不建检测，下一次漂移照样没人知道。

## 这个检查一眼都不看机密

- **机密键**（密码 / 令牌 / webhook）：只确认它还在，**值不出 NAS**。
  实现上是先只取键名，再按白名单在**远端**过滤，只有非机密键的值会过网络。
- **机器特定键**（数据根、镜像标签）：本来就该只活在 .env 里，不参与比对。
  镜像标签尤其不能进 git —— 它是「线上是哪个 commit」，写进 git 会自相矛盾。

## 判据

    非机密键，git 有默认值，值不同      ❌ 漂移：git 在说假话
    非机密键，git 完全不认识它           ❌ 只活在 NAS 上的配置
    机密键缺失                          ❌ 服务起不来
    非机密键，值与 git 默认相同          ⚠️ 冗余覆盖：删掉它，否则它是下一次漂移的入口
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_source_drift import release_config  # noqa: E402 —— 不写第二份 ssh 默认值

# —— 键的分类。这是这个检查的**唯一**判据来源，别在别处再写一份 ——
# 含机密：值绝不出 NAS。
SECRET_KEYS = frozenset({
    "FALKORDB_PASSWORD",
    "KG_HUB_API_TOKEN",
    "KG_HUB_MODEL_GATEWAY_TOKEN",
    "KG_HUB_FEISHU_WEBHOOK",      # URL 自带令牌，等同机密
})
# 机器特定/发布状态：本来就只该活在 .env 里。
MACHINE_KEYS = frozenset({
    "KG_HUB_DATA_ROOT",           # 这台 NAS 的盘布局
    "KG_HUB_IMAGE_TAG",           # release.sh 写的发布状态
    "KG_HUB_IMAGE_TAG_PREV",
})

SSH = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25"]
DEFAULT_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):-([^}]*)\}")


def git_defaults(repo: Path) -> dict[str, str]:
    """docker-compose.yml 里 `${KEY:-默认值}` 声明的那些默认值。"""
    text = (repo / "docker-compose.yml").read_text("utf-8")
    return {m.group(1): m.group(2) for m in DEFAULT_PATTERN.finditer(text)}


def remote_keys(ssh_target: str, src: str) -> list[str]:
    """只取键名，不取值。"""
    proc = subprocess.run(
        SSH + [ssh_target, f"grep -oE '^[A-Z_][A-Z0-9_]*=' '{src}/.env' 2>/dev/null"],
        capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise SystemExit(f"读不到 NAS 上的 .env：{proc.stderr.strip() or '无输出'}")
    return [line.rstrip("=") for line in proc.stdout.split() if line.endswith("=")]


def remote_values(ssh_target: str, src: str, keys: list[str]) -> dict[str, str]:
    """只取**这些**键的值。过滤在远端做 —— 机密的值一个字节都不过网络。"""
    if not keys:
        return {}
    pattern = "|".join(re.escape(k) for k in keys)
    proc = subprocess.run(
        SSH + [ssh_target, f"grep -E '^({pattern})=' '{src}/.env' 2>/dev/null"],
        capture_output=True, text=True)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def readable_keys(keys: list[str]) -> list[str]:
    """哪些键的**值**允许过网络。机密与机器特定的一律不取。

    单独成函数是为了能直接验它 —— 这是这个工具唯一一条「不该发生的事」，
    埋在 main 里的一句列表推导没人验得到。
    """
    return [k for k in keys if k not in SECRET_KEYS and k not in MACHINE_KEYS]


def classify(keys: list[str], values: dict[str, str],
             defaults: dict[str, str]) -> tuple[list[str], list[str]]:
    """返回 (致命, 警告)。判据全在这里，远端取数与判断分开，便于测。"""
    fatal: list[str] = []
    warn: list[str] = []
    present = set(keys)
    for key in sorted(SECRET_KEYS - present):
        fatal.append(f"{key} —— 机密键不在 .env 里，服务起不来")
    for key in sorted(present - SECRET_KEYS - MACHINE_KEYS):
        value = values.get(key)
        if value is None:
            continue
        if key not in defaults:
            fatal.append(f"{key} —— 非机密配置只活在 NAS 上，git 里没有它的默认值")
        elif defaults[key] != value:
            fatal.append(
                f"{key} —— 线上是 {value!r}，git 默认是 {defaults[key]!r}：git 在说假话")
        else:
            warn.append(f"{key} —— 与 git 默认相同的冗余覆盖，建议从 .env 删掉")
    return fatal, warn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="kg-hub NAS .env 非机密配置漂移检测")
    ap.add_argument("--repo", default=None, help="被检查的 git 仓库；默认取脚本所在那棵树")
    ap.add_argument("--strict", action="store_true", help="把「冗余覆盖」也算失败")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[2]
    # release_config 返回的是 dict，不是元组 —— 第一版按元组解包，拿到的是键名
    # 字符串 "ssh"/"src"，于是去连一台叫 ssh 的主机。**假设返回形状**而不是去读它，
    # 正是准则 22 那个坑；这次它响得很早，但值得留一句。
    config = release_config(repo / "deploy" / "nas" / "release.sh")
    ssh_target, src = config["ssh"], config["src"]
    keys = remote_keys(ssh_target, src)
    readable = readable_keys(keys)
    fatal, warn = classify(keys, remote_values(ssh_target, src, readable),
                           git_defaults(repo))

    for line in fatal:
        print(f"❌ {line}")
    for line in warn:
        print(f"⚠️  {line}")
    if fatal:
        return 1
    if warn:
        print(f"（{len(warn)} 处冗余覆盖；值当前一致，所以线上行为没问题，"
              f"但它们是下一次漂移的入口）")
        return 1 if args.strict else 0
    print("✅ NAS .env 里的非机密配置与 git 一致，且没有冗余覆盖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
