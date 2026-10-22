"""全设备共享的在途领取登记：同一台设备上，所有战役与 `agent_extract next` 领批前都在这里互相避让。

为什么需要：每个战役有自己的游标与锁，`agent_extract next` 也有自己的游标；两个战役同时跑，或战役和
`next` 同时跑，开局都从同一个位置取，会领到同一批观测。服务端的幂等键保证图里不重复（后到的提交记为 skipped），
但后到那份抽取的 token 白花了（2026-10-23 用户：对比测试的 token 不能白白浪费）。

规则：
- 登记文件不跟 KG_HUB_AGENT_EXTRACT_DIR 走，固定在 ~/.kg-hub/claims/（KG_HUB_CLAIMS_DIR 可改，测试用），
  所以不同状态目录、不同工具（Claude Code / qoder / codex / cursor / DSH）只要是同一台设备同一个用户就共用。
- 领取方（owner）：每个战役一个（`campaign:<id>`），每个 next 状态目录一个（`next:<目录>`）。
  自己占着的不挡自己（战役回收后重领自己的条目照常）。
- 领批时在登记锁里：剔除别人占着的 → 选批 → 把选中的登记到自己名下；正式提交后入库（ok / empty）的注销，
  没入库的留在原领取方名下，等它回收重领；登记超过 TTL（默认 12 小时，KG_HUB_CLAIM_TTL_HOURS）自动失效，
  防止中途放弃的领取一直占着。
- 被跳过的观测：领取方的游标会越过它们；若占着的一方最终没入库也没重领，它们留在积压里，
  由 refinery 或之后重置游标的执行者处理——不会丢，只是这一轮不抢。

跨设备不在本登记的范围内（那要把领取做到服务端）；跨设备同时跑仍靠服务端幂等键兜底。

命令行：python -m tools.claim_registry list [--owner X] | release --owner X [--ids 1 2 ...] | purge
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_TTL_HOURS = 12.0


def claims_dir() -> Path:
    return Path(os.environ.get("KG_HUB_CLAIMS_DIR", "~/.kg-hub/claims")).expanduser()


def ttl() -> timedelta:
    return timedelta(hours=float(os.environ.get("KG_HUB_CLAIM_TTL_HOURS", DEFAULT_TTL_HOURS)))


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Registry:
    """在 opened() 里使用：读入时清掉过期登记，退出时有改动才写回。"""

    def __init__(self, data: dict, now: datetime):
        self.now = now
        self.entries: dict[str, dict] = {
            k: v for k, v in (data.get("claims") or {}).items()
            if datetime.fromisoformat(v["expires_at"]) > now}
        self.dirty = len(self.entries) != len(data.get("claims") or {})

    def held_by_others(self, owner: str) -> set[int]:
        return {int(k) for k, v in self.entries.items() if v["owner"] != owner}

    def reserve(self, ids, owner: str, info: str = "") -> list[int]:
        """登记到 owner 名下；已被别人占着的不登记，返回实际登记的 id。自己已占着的续期。"""
        got, exp = [], (self.now + ttl()).isoformat()
        for i in ids:
            cur = self.entries.get(str(int(i)))
            if cur and cur["owner"] != owner:
                continue
            self.entries[str(int(i))] = {"owner": owner, "info": info, "claimed_at": self.now.isoformat(),
                                         "expires_at": exp}
            got.append(int(i))
        self.dirty = self.dirty or bool(got)
        return got

    def release(self, ids, owner: str) -> int:
        """只注销 owner 自己名下的。"""
        n = 0
        for i in ids:
            cur = self.entries.get(str(int(i)))
            if cur and cur["owner"] == owner:
                del self.entries[str(int(i))]
                n += 1
        self.dirty = self.dirty or bool(n)
        return n

    def release_owner(self, owner: str) -> int:
        return self.release([int(k) for k, v in self.entries.items() if v["owner"] == owner], owner)


@contextlib.contextmanager
def opened(now: datetime | None = None):
    d = claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "inflight.json"
    with open(d / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            data = json.loads(p.read_text()) if p.exists() else {}
            reg = Registry(data, now or _now())
            yield reg
            if reg.dirty:
                tmp = d / "inflight.json.tmp"
                tmp.write_text(json.dumps({"claims": reg.entries}, ensure_ascii=False, indent=1))
                tmp.replace(p)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def release(ids, owner: str) -> int:
    with opened() as reg:
        return reg.release(ids, owner)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全设备共享的在途领取登记")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--owner")
    rl = sub.add_parser("release")
    rl.add_argument("--owner", required=True)
    rl.add_argument("--ids", nargs="+", type=int, help="不给就注销这个领取方名下的全部")
    sub.add_parser("purge", help="清掉过期登记")
    a = ap.parse_args(argv)
    with opened() as reg:
        if a.cmd == "release":
            n = reg.release(a.ids, a.owner) if a.ids else reg.release_owner(a.owner)
            print(json.dumps({"released": n}, ensure_ascii=False))
            return 0
        if a.cmd == "purge":
            reg.dirty = True
            print(json.dumps({"left": len(reg.entries)}, ensure_ascii=False))
            return 0
        by_owner: dict[str, list[int]] = {}
        for k, v in reg.entries.items():
            if not a.owner or v["owner"] == a.owner:
                by_owner.setdefault(v["owner"], []).append(int(k))
        print(json.dumps({o: {"n": len(ids), "ids": sorted(ids)[:20]} for o, ids in sorted(by_owner.items())},
                         ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
