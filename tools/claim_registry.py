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

操作日志（2026-10-24 加）：每次登记 / 注销 / 过期清理都在登记锁里往 journal.jsonl 追加一行（操作、领取方、观测号、
批次、进程号、父进程、工作目录、调用命令），注销时还记下「想注销却不在自己名下」的条目；登记文件带版本号 rev，
每次经本模块写入加一并记进日志，读到的 rev 与日志最后一行对不上，就记一条 external_change（有人绕过锁改了文件）。
起因：观测 38252 被 codex 战役领走后，一分钟内又被另一个战役领到，事后无从查证那条登记是谁、何时注销的。
日志超过 20MB 滚动一份旧档（journal.jsonl.1）。

命令行：python -m tools.claim_registry list [--owner X] | release --owner X [--ids 1 2 ...] | purge
       | history --id <观测号> | tail [--n 20]
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
JOURNAL_MAX_BYTES = 20 * 1024 * 1024


def claims_dir() -> Path:
    return Path(os.environ.get("KG_HUB_CLAIMS_DIR", "~/.kg-hub/claims")).expanduser()


def ttl() -> timedelta:
    return timedelta(hours=float(os.environ.get("KG_HUB_CLAIM_TTL_HOURS", DEFAULT_TTL_HOURS)))


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Registry:
    """在 opened() 里使用：读入时清掉过期登记，退出时有改动才写回；各操作攒进 events，写回时追加进日志。"""

    def __init__(self, data: dict, now: datetime):
        self.now = now
        self.rev = int(data.get("rev") or 0)
        self.events: list[dict] = []
        old = data.get("claims") or {}
        self.entries: dict[str, dict] = {k: v for k, v in old.items()
                                         if datetime.fromisoformat(v["expires_at"]) > now}
        expired: dict[str, list[int]] = {}
        for k, v in old.items():
            if k not in self.entries:
                expired.setdefault(v["owner"], []).append(int(k))
        for owner, ids in sorted(expired.items()):
            self.events.append({"op": "expire", "owner": owner, "ids": sorted(ids)})
        self.dirty = bool(expired)

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
        skipped = sorted({int(i) for i in ids} - set(got))
        if got or skipped:
            self.events.append({"op": "reserve", "owner": owner, "info": info, "ids": got,
                                **({"skipped_held": skipped} if skipped else {})})
        self.dirty = self.dirty or bool(got)
        return got

    def release(self, ids, owner: str) -> int:
        """只注销 owner 自己名下的。"""
        done, not_mine, absent = [], {}, []
        for i in ids:
            cur = self.entries.get(str(int(i)))
            if cur and cur["owner"] == owner:
                del self.entries[str(int(i))]
                done.append(int(i))
            elif cur:
                not_mine.setdefault(cur["owner"], []).append(int(i))
            else:
                absent.append(int(i))
        if done or not_mine or absent:
            self.events.append({"op": "release", "owner": owner, "ids": done,
                                **({"not_mine": not_mine} if not_mine else {}),
                                **({"absent": absent} if absent else {})})
        self.dirty = self.dirty or bool(done)
        return len(done)

    def release_owner(self, owner: str) -> int:
        return self.release([int(k) for k, v in self.entries.items() if v["owner"] == owner], owner)


def _context() -> dict:
    """谁在动登记：进程、父进程、工作目录、调用命令（只取前几个参数，不含环境变量）。"""
    return {"pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(),
            "argv": [os.path.basename(sys.argv[0])] + sys.argv[1:6] if sys.argv else []}


def _last_journal_rev(j: Path) -> int | None:
    if not j.exists() or j.stat().st_size == 0:
        return None
    with open(j, "rb") as fh:
        fh.seek(max(0, j.stat().st_size - 65536))
        lines = fh.read().decode("utf-8", "replace").strip().splitlines()
    for line in reversed(lines):
        try:
            return int(json.loads(line)["rev"])
        except (ValueError, KeyError, TypeError):
            continue
    return None


def _append_journal(d: Path, events: list[dict]) -> None:
    j = d / "journal.jsonl"
    if j.exists() and j.stat().st_size > JOURNAL_MAX_BYTES:
        j.replace(d / "journal.jsonl.1")
    with open(j, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


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
            last = _last_journal_rev(d / "journal.jsonl")
            if last is not None and last != reg.rev:
                # 文件版本和日志最后记下的不一致：有人绕过本模块改了登记文件
                reg.events.insert(0, {"op": "external_change", "file_rev": reg.rev, "journal_rev": last,
                                      "claims": len(data.get("claims") or {})})
                reg.dirty = True
            yield reg
            if reg.dirty:
                reg.rev += 1
                tmp = d / "inflight.json.tmp"
                tmp.write_text(json.dumps({"rev": reg.rev, "claims": reg.entries}, ensure_ascii=False, indent=1))
                tmp.replace(p)
            if reg.events:
                ctx, ts = _context(), _now().isoformat()
                _append_journal(d, [{"ts": ts, "rev": reg.rev, **e, **ctx} for e in reg.events])
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def history(obs_id: int, limit: int = 50) -> list[dict]:
    """某条观测在登记里的全部经历（含滚动出去的旧档），按时间顺序。"""
    d, out = claims_dir(), []
    for f in (d / "journal.jsonl.1", d / "journal.jsonl"):
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            hit = obs_id in (e.get("ids") or []) or obs_id in (e.get("skipped_held") or []) \
                or obs_id in (e.get("absent") or []) \
                or any(obs_id in v for v in (e.get("not_mine") or {}).values())
            if hit:
                out.append(e)
    return out[-limit:]


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
    hi = sub.add_parser("history", help="某条观测在登记里的全部经历")
    hi.add_argument("--id", type=int, required=True)
    hi.add_argument("--limit", type=int, default=50)
    tl = sub.add_parser("tail", help="最近的操作日志")
    tl.add_argument("--n", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "history":
        for e in history(a.id, a.limit):
            print(json.dumps(e, ensure_ascii=False))
        return 0
    if a.cmd == "tail":
        j = claims_dir() / "journal.jsonl"
        for line in (j.read_text(encoding="utf-8").splitlines()[-a.n:] if j.exists() else []):
            print(line)
        return 0
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
