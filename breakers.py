"""人工断路器：按业务 key 切断模型调用。

**为什么要有它。** 2026-09-10 实测：qianfan 恢复后采集全速跑，claude-mem 每小时
约 96 次在流式响应完成前放弃，其中约 28 次在网关留下 `unknown` 记录。那种记录
**永远不会过期**（网关只删 completed/error，因为过期不能证明供应商没扣过钱），
每一条都挡住下一次 cutover。六小时攒了 171 条，而当时**没有任何手段**能单独切断
某一路的模型调用——网关只有全局排空，一切就把无关业务也切了。这个模块就是那个
缺掉的开关。

**它是硬逻辑，不是建议。** 判定发生在 kg-hub 唯一的模型出口
（`model_gateway_client.install_gateway_request_contract`）里，所以试金石
「调用方不看开关 / 看错了，钱还会花出去吗」的答案是不会：调用方绕不过去。
refinery 另有一层「跳过不提交」，那层是为了不做无用功，不是安全边界。

**三种状态，故意不对称：**

- 文件不存在 → **通行**。这是「从没配过」的正常初始状态，不能让它把管线焊死。
- 文件存在且合法 → 按里面写的来。
- 文件存在但读不动/不合法 → **一律按已断开处理**（fail-closed）。这是花钱的闸门，
  读不懂的时候唯一安全的假设是别花。沿用 `HistoricalOutcomeQuarantine` 对损坏
  证据的同一个先例。损坏必须在拓扑上显式标出来，否则整条管线会静悄悄停住而没人
  知道为什么。

**谁写谁读。** kg-hub 服务端可写（拓扑页的开关按钮），其余容器只读挂载。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# 采集链路上会调用模型的两个 business key。拓扑图上对应 claude-mem 与 refinery
# 两个节点。这里写死是有意的：断路器是控制面，能被切断的东西必须是一份人工审过
# 的清单，不能由运行时数据（比如网关回给我们的路由表）自己长出来。
KNOWN_KEYS: tuple[str, ...] = ("claude_mem.observation", "kg_hub.entity_extract")

# 每个 key 在拓扑上挂在哪个节点：给 UI 用，避免前端再写一份映射。
KEY_NODES: dict[str, str] = {
    "claude_mem.observation": "claude-mem",
    "kg_hub.entity_extract": "refinery",
}

# 这一路是否**真的有东西在执行这个开关**。
#
# 一个按下去没有任何效果的开关，比没有开关更危险：操作员以为已经断了，就不会再去
# 想别的办法，而钱照烧。所以「接没接线」必须是代码里的一个事实，由 UI 如实显示，
# 不能靠文档或记忆。
#
# kg_hub.entity_extract：refinery 每轮开头读它决定提不提交（停流），
#   model_gateway_client 里另有一层硬挡（兜底）。已接线。
# claude_mem.observation：状态存得下，但 claude-mem 的 worker 不在本仓库，
#   目前**没有任何执行方**。接线方案见 T-0066：由 Mac 探针据此停/起 launchd
#   worker（进程停 = 队列在 SQLite 里等，不撞墙）。在那之前 UI 必须显示「未接线」。
ENFORCED: dict[str, bool] = {
    "claude_mem.observation": False,
    "kg_hub.entity_extract": True,
}

DEFAULT_PATH = Path(os.environ.get("KG_HUB_BREAKERS", "/breakers/breakers.json"))
MAX_BYTES = 64 * 1024
_REASON_MAX = 200


class BreakerOpen(RuntimeError):
    """该业务 key 的模型调用已被人工切断。"""

    def __init__(self, key: str, reason: str = ""):
        self.key = key
        self.reason = reason
        detail = f"：{reason}" if reason else ""
        super().__init__(f"模型调用已被人工断路：{key}{detail}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blank(key: str) -> dict:
    return {"tripped": False, "at": None, "by": None, "reason": ""}


def _clean_entry(raw: object) -> dict | None:
    """只接受完全合法的一条；任何不合法都让整份判为损坏。"""
    if not isinstance(raw, dict):
        return None
    if type(raw.get("tripped")) is not bool:
        return None
    entry = {"tripped": raw["tripped"], "at": None, "by": None, "reason": ""}
    for field in ("at", "by"):
        value = raw.get(field)
        if value is None or isinstance(value, str):
            entry[field] = value
        else:
            return None
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        return None
    entry["reason"] = reason[:_REASON_MAX]
    return entry


def read_state(path: Path | None = None) -> dict:
    """读断路器状态。永不抛异常——调用点在付费路径上，它必须总能拿到一个判决。

    返回 ``{"ok": bool, "corrupt": bool, "missing": bool, "breakers": {...},
    "error": str|None}``。``corrupt`` 为真时每个 key 都已被置成 tripped，调用方
    照常读 ``breakers`` 即可，不需要自己再判一次。
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    blank = {key: _blank(key) for key in KNOWN_KEYS}

    def corrupt(error: str) -> dict:
        # 读不懂就当全部断开：这是花钱的闸门，唯一安全的假设是别花。
        return {"ok": False, "corrupt": True, "missing": False,
                "breakers": {key: {**_blank(key), "tripped": True,
                                   "reason": "断路器状态不可读，已按断开处理"}
                             for key in KNOWN_KEYS},
                "error": error}

    try:
        if not target.exists():
            # 从没配过 ≠ 出了问题：不能让缺省状态把管线焊死。
            return {"ok": True, "corrupt": False, "missing": True,
                    "breakers": blank, "error": None}
        if target.is_symlink() or not target.is_file():
            return corrupt("断路器状态不是普通文件")
        if target.stat().st_size > MAX_BYTES:
            return corrupt("断路器状态文件过大")
        document = json.loads(target.read_text("utf-8"))
    except OSError as exc:
        return corrupt(f"断路器状态读取失败：{type(exc).__name__}")
    except (ValueError, UnicodeError):
        return corrupt("断路器状态不是合法 JSON")

    if not isinstance(document, dict) or document.get("version") != 1:
        return corrupt("断路器状态版本不认识")
    raw = document.get("breakers")
    if not isinstance(raw, dict):
        return corrupt("断路器状态结构不对")
    result = dict(blank)
    for key, value in raw.items():
        if key not in KNOWN_KEYS:
            # 清单外的 key 说明这份文件不是我们认识的那份：宁可全断，不要只断一半。
            return corrupt(f"断路器状态含未知业务 key")
        entry = _clean_entry(value)
        if entry is None:
            return corrupt("断路器状态某一条不合法")
        result[key] = entry
    return {"ok": True, "corrupt": False, "missing": False,
            "breakers": result, "error": None}


def is_tripped(key: str, path: Path | None = None) -> tuple[bool, str]:
    """(是否已断开, 原因)。清单外的 key 一律放行——只有列进来的才归它管。"""
    if key not in KNOWN_KEYS:
        return False, ""
    entry = read_state(path)["breakers"][key]
    return bool(entry["tripped"]), str(entry.get("reason") or "")


def assert_closed(key: str, path: Path | None = None) -> None:
    """付费路径上的判定点：已断开就抛，绝不发请求。"""
    tripped, reason = is_tripped(key, path)
    if tripped:
        raise BreakerOpen(key, reason)


def set_tripped(key: str, tripped: bool, *, by: str, reason: str = "",
                path: Path | None = None) -> dict:
    """扳一个开关，原子落盘。

    损坏的现有文件会被这次写入覆盖掉——这是刻意的：损坏时管线已经全停，操作员
    唯一的出路就是重新写一份干净的。
    """
    if key not in KNOWN_KEYS:
        raise ValueError(f"未知业务 key: {key}")
    if not isinstance(tripped, bool):
        raise ValueError("tripped 必须是布尔")
    target = Path(path) if path is not None else DEFAULT_PATH
    state = read_state(target)
    # 损坏时不要把「全部按断开」当成既有事实写回去，那会把没人动过的另一个 key
    # 也永久扳断。以空白为底，只落这一次真实的操作。
    breakers = ({k: _blank(k) for k in KNOWN_KEYS} if state["corrupt"]
                else {k: dict(v) for k, v in state["breakers"].items()})
    breakers[key] = {"tripped": tripped, "at": _now(),
                     "by": str(by)[:64], "reason": str(reason)[:_REASON_MAX]}
    document = {"version": 1, "breakers": breakers}
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent,
        prefix=target.name + ".", suffix=".tmp", delete=False)
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return document
