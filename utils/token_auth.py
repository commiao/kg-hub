"""可撤销的 scoped Token —— 给「只能读」的设备用。

## 为什么要有

`KG_HUB_API_TOKEN` 是**一个**静态值，鉴权只做一次相等比较。任一持有者能打到
每一个受保护 API，包括 `/api/ingest`。所以在它之上**说不出**「只读」这个词：
不是没配好，是这个模型里根本没有「部分权限」这个概念（T-0052）。

## 边界（说准，别多说）

这里管的是 `/api/*` 那一面。`/`、`/portal`、`/dashboard` 前缀由 kg_hub_server 的
中间件整体豁免（含 12 条 POST），**2026-09-21 用户明确决定维持现状**：这台只在
内网、单人使用，不为面板按钮引入身份校验。所以：

    scoped token 的保证是「经 /api/* 写不进去」，**不是**「这个端口上写不进去」。

好在 `mcp_server.py` 只调 `/api/*` 六个读端点，所以一台只跑 MCP 的设备确实写不了。
任何对外说明都按这个口径写，别把它说成端口级的只读。

## 设计

- **不存明文**：注册表里只有 SHA-256 摘要。丢了注册表也漏不出 token。
- **每次请求按 mtime 重载**：撤销即时生效，不用重启服务。
- **显式白名单**：不在名单里的一律拒绝。将来新增端点默认不开放 —— 「损坏=按断开」
  的同一条道理：读不懂/没列出时，唯一安全的假设是不给。
- **文件缺失/坏掉 = 没有任何 scoped token**（失败关闭）。注意这与断路器那条
  「读不懂就按断开」方向一致：都是往「不放行」倒。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path

ACCESS_CONTROL_PATH = Path(
    os.environ.get("KG_HUB_ACCESS_CONTROL", "/access-control/read-tokens.json"))

# 只读设备能打的端点。**显式**列出，且只允许 GET。
# 这六个正是 mcp_server.py 用到的那六个（kg_search 走 search / search_semantic，
# kg_node_neighbors、kg_path_between、kg_episode_search、kg_stats 各一个）。
READ_SCOPE = "read"
SCOPE_ALLOWLIST: dict[str, frozenset[tuple[str, str]]] = {
    READ_SCOPE: frozenset({
        ("GET", "/api/stats"),
        ("GET", "/api/search"),
        ("GET", "/api/search_semantic"),
        ("GET", "/api/node_neighbors"),
        ("GET", "/api/path_between"),
        ("GET", "/api/episode_search"),
    }),
}

_lock = threading.Lock()
_cache: dict[str, object] = {"stamp": None, "entries": ()}


def _load() -> tuple[dict, ...]:
    """读注册表；按 (mtime, size) 变化重载。读不到就是「一个 scoped token 都没有」。"""
    try:
        st = ACCESS_CONTROL_PATH.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            _cache["stamp"], _cache["entries"] = None, ()
        return ()
    with _lock:
        if _cache["stamp"] == stamp:
            return _cache["entries"]            # type: ignore[return-value]
    try:
        raw = json.loads(ACCESS_CONTROL_PATH.read_text("utf-8"))
        items = raw.get("tokens") if isinstance(raw, dict) else None
        entries = tuple(e for e in (items or []) if isinstance(e, dict))
    except Exception:  # noqa: BLE001 —— 坏文件 = 没有 scoped token，不是放行
        entries = ()
    with _lock:
        _cache["stamp"], _cache["entries"] = stamp, entries
    return entries


def principal_for(token: str) -> dict | None:
    """按摘要查出这把 token 的身份与 scope；查不到返回 None。

    比对用 `hmac.compare_digest`：两把都不对的 token 不该因为「前几个字符碰巧对」
    而花掉不同的时间。
    """
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for entry in _load():
        if entry.get("disabled"):
            continue
        known = str(entry.get("sha256") or "")
        if len(known) == len(digest) and hmac.compare_digest(known.lower(), digest):
            scopes = [str(s) for s in (entry.get("scopes") or []) if isinstance(s, str)]
            return {"name": str(entry.get("name") or "unnamed"), "scopes": scopes}
    return None


def allows(principal: dict | None, method: str, path: str) -> bool:
    """这把 token 能不能打这个 (method, path)。名单之外一律 False。"""
    if not principal:
        return False
    want = (str(method).upper(), str(path))
    for scope in principal.get("scopes") or []:
        if want in SCOPE_ALLOWLIST.get(scope, frozenset()):
            return True
    return False


def digest_for(token: str) -> str:
    """给发放方算摘要用；服务端自己不需要明文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
