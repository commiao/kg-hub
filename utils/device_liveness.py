"""NAS 侧 Tailscale 设备在线信号的共享读取与判定。

独立 producer 容器原子写入 ``tailscale status --json`` 的原始输出；server 与
watchdog 容器只读消费。在线状态来自 Tailscale 实况，文件 mtime 是采样时间。
过期、缺失、损坏或未来时间戳一律降级为 unknown，绝不把旧 ``Online: true``
当作在线证据。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path(os.environ.get(
    "KG_HUB_DEVICE_LIVENESS_PATH", "/device-liveness/tailscale-status.json"))
DEFAULT_CONFIG_PATH = Path(os.environ.get(
    "KG_HUB_DEVICE_LIVENESS_CONFIG", "/device-liveness-config/device-liveness.json"))
DEFAULT_CAPTURE_STALE_AFTER_S = 30 * 60
try:
    DEFAULT_MAX_AGE_S = int(os.environ.get("KG_HUB_DEVICE_LIVENESS_MAX_AGE_S", "180"))
except ValueError:
    DEFAULT_MAX_AGE_S = 180


def positive_int(value: object, fallback: int) -> int:
    """解析正整数；缺失、非法或非正数统一使用调用方给定的 fallback。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def normalize_host(value: object) -> str:
    """把 Tailscale HostName/DNSName 与探针 host 规整到同一匹配键。"""
    host = str(value or "").strip().lower().rstrip(".")
    if host.endswith(".local"):
        host = host[:-6]
    return host


def _peer_aliases(peer: dict[str, Any], peer_key: object = "") -> set[str]:
    aliases: set[str] = set()
    for value in (peer_key, peer.get("ID"), peer.get("PublicKey")):
        identity = normalize_host(value)
        if identity:
            aliases.add(identity)
    for field in ("HostName", "DNSName"):
        name = normalize_host(peer.get(field))
        if not name:
            continue
        aliases.add(name)
        if "." in name:
            aliases.add(name.split(".", 1)[0])
    return aliases


def parse_status(status: object, *, age_s: float,
                 max_age_s: int = DEFAULT_MAX_AGE_S) -> dict[str, Any]:
    """把原始 Tailscale JSON 变成稳定的小型查询结构。"""
    if not isinstance(status, dict):
        return {"source_state": "invalid", "age_s": age_s, "devices": {},
                "detail": "Tailscale 状态不是 JSON object"}

    backend_state = status.get("BackendState")
    if backend_state != "Running":
        return {"source_state": "backend-not-running", "age_s": int(age_s),
                "devices": {},
                "detail": f"Tailscale BackendState={backend_state!r}，在线态不可信"}

    peers = status.get("Peer")
    if not isinstance(peers, dict):
        return {"source_state": "invalid", "age_s": int(age_s), "devices": {},
                "detail": "Tailscale Peer 字段不是 object"}
    records = list(peers.items())
    devices: dict[str, dict[str, Any]] = {}
    for peer_key, peer in records:
        if not isinstance(peer, dict):
            continue
        online = peer.get("Online")
        state = "online" if online is True else (
            "offline" if online is False else "unknown")
        record = {
            "state": state,
            "peer_identity": normalize_host(peer_key),
            "host_name": str(peer.get("HostName") or ""),
            "dns_name": str(peer.get("DNSName") or ""),
            "last_seen": str(peer.get("LastSeen") or ""),
        }
        for alias in _peer_aliases(peer, peer_key):
            previous = devices.get(alias)
            if previous is None:
                devices[alias] = record
            elif previous.get("peer_identity") != record["peer_identity"]:
                # HostName/DNSName 可变且可能重复。同名时绝不让后遍历的 peer
                # 覆盖前一个在线态；显式配置稳定 node-key 仍可消除歧义。
                devices[alias] = {
                    "state": "unknown",
                    "ambiguous": True,
                    "peer_identity": "",
                }

    source_state = "fresh" if 0 <= age_s <= max_age_s else "stale"
    return {
        "source_state": source_state,
        "age_s": int(age_s),
        "devices": devices,
        "detail": (f"Tailscale 状态 {int(age_s)} 秒前采样"
                   if source_state == "fresh"
                   else f"Tailscale 状态已过期（{int(age_s)} 秒前）"),
    }


def load_status(path: Path | str = DEFAULT_PATH, *, now: float | None = None,
                max_age_s: int = DEFAULT_MAX_AGE_S) -> dict[str, Any]:
    """读取原子快照；任一异常均返回 unknown 来源，不向调用方抛错。"""
    source = Path(path)
    clock = time.time() if now is None else now
    try:
        stat = source.stat()
    except FileNotFoundError:
        return {"source_state": "missing", "age_s": None, "devices": {},
                "detail": f"未找到设备在线快照 {source}"}
    except Exception as exc:  # noqa: BLE001
        return {"source_state": "invalid", "age_s": None, "devices": {},
                "detail": f"设备在线快照不可读: {type(exc).__name__}"}

    age_s = clock - stat.st_mtime
    # 容忍很小的时钟/文件系统取整偏差；明显来自未来的采样不可作为证据。
    if age_s < -5:
        return {"source_state": "invalid", "age_s": int(age_s), "devices": {},
                "detail": "设备在线快照时间戳来自未来"}
    age_s = max(0.0, age_s)
    try:
        status = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"source_state": "invalid", "age_s": int(age_s), "devices": {},
                "detail": f"设备在线快照 JSON 损坏: {type(exc).__name__}"}
    return parse_status(status, age_s=age_s, max_age_s=max_age_s)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """读取不含 secret 的设备身份/阈值配置；失败时安全退回空配置。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _identity_candidates(host: object, aliases: object) -> list[str]:
    """capture host 加显式 Tailscale HostName/DNSName/node 身份映射。"""
    candidates = [normalize_host(host)]
    if not isinstance(aliases, dict):
        return candidates
    target = normalize_host(host)
    mapped: object = []
    for capture_host, identities in aliases.items():
        if normalize_host(capture_host) == target:
            mapped = identities
            break
    if isinstance(mapped, str):
        mapped = [mapped]
    if isinstance(mapped, list):
        candidates.extend(normalize_host(value) for value in mapped)
    return [candidate for candidate in dict.fromkeys(candidates) if candidate]


def device_state(liveness: dict[str, Any] | None, host: object,
                 aliases: object = None) -> tuple[str, str]:
    """返回 ``(online|offline|unknown, detail)``。只有 fresh 来源可给出确定态。"""
    if not isinstance(liveness, dict):
        return "unknown", "没有独立设备在线信号"
    source_state = str(liveness.get("source_state") or "unknown")
    if source_state != "fresh":
        return "unknown", str(liveness.get("detail") or f"设备在线信号 {source_state}")

    devices = liveness.get("devices") or {}
    record = None
    matched = ""
    ambiguous = False
    if isinstance(devices, dict):
        for identity in _identity_candidates(host, aliases):
            candidate = devices.get(identity)
            if isinstance(candidate, dict):
                if candidate.get("state") in {"online", "offline"}:
                    record, matched = candidate, identity
                    break
                ambiguous = ambiguous or bool(candidate.get("ambiguous"))
    if not isinstance(record, dict):
        if ambiguous:
            return "unknown", f"Tailscale 身份 {host} 存在同名冲突，请配置稳定 node-key"
        return "unknown", f"Tailscale 快照未找到 {host}（含身份映射）"
    state = str(record.get("state") or "unknown")
    if state not in {"online", "offline"}:
        return "unknown", f"Tailscale 未给出 {host} 的 Online 布尔值"
    return state, (f"Tailscale {matched} 明确"
                   f"{('在线' if state == 'online' else '离线/睡眠')}"
                   + (f"（LastSeen {record['last_seen']}）" if record.get("last_seen") else ""))
