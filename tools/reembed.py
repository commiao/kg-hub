"""tools/reembed.py — 换向量模型时，在本机重算全图向量并分批写回 NAS（T-0339）。

为什么在本机算：NAS 那颗 CPU 单核跑 31 秒就升温 11°C，全量 23 万条在 NAS 上推算要数小时，
此前两次计划外重启都是过热保护。本机算完，NAS 只承担写入。方案：docs/EMBEDDING-MODEL-SWITCH-PLAN.md。

子命令（都要能连上 NAS 的 FalkorDB；凭据同 tools/eval_recall，从环境变量或 ~/.claude-mem/.env 取）：
  plan     只读：条数、图里向量的来源模型（标记节点）、目标模型、各阶段文件状态
  backup   只读：把现有 name_embedding / fact_embedding 原样导出到本地，用于回退
  compute  只读：用目标模型在本机算全部实体名与 fact 的向量，存检查点
  write    写图：排空 → 等在飞归零 → 补算新增/改动 → 分批写回（每批续期排空）→ 更新标记 → 解除排空
  restore  写图：用 backup 的向量写回，并把标记改回备份时的模型
  verify   只读：抽样读回线上向量，与本地检查点比对

写图的两个子命令默认只在 23:00–01:45（Asia/Shanghai）执行：避开白天入图与凌晨 02:00 路由器重启。
写完后要立刻按发布流程把 KG_HUB_EMBED_MODEL 切到目标模型并重启服务——在那之前，
build_graphiti 的一致性检查会让新起的进程拒绝启动，而正在跑的进程仍用旧模型算查询向量。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.embedding_profile import (  # noqa: E402
    DEFAULT_MODEL, MARKER_READ, MARKER_WRITE, PROFILES, graph_model_from_rows)

DEFAULT_DIR = "~/.kg-hub/reembed"
DEFAULT_URL = "http://100.123.208.32:17171"
PAGE = 10000
TZ = timezone(timedelta(hours=8))
WINDOW = ((23, 0), (1, 45))          # 跨午夜：[23:00, 次日 01:45)
DRAIN_RENEW_SEC = 900

READ_NODES = "MATCH (n:Entity) RETURN n.uuid, n.name SKIP $s LIMIT $l"
READ_EDGES = "MATCH ()-[e:RELATES_TO]->() RETURN e.uuid, e.fact SKIP $s LIMIT $l"
READ_NODE_VECS = "MATCH (n:Entity) RETURN n.uuid, n.name_embedding SKIP $s LIMIT $l"
READ_EDGE_VECS = "MATCH ()-[e:RELATES_TO]->() RETURN e.uuid, e.fact_embedding SKIP $s LIMIT $l"
WRITE_NODES = ("UNWIND $rows AS r MATCH (n:Entity {uuid: r.u}) "
               "SET n.name_embedding = vecf32(r.v) RETURN count(n)")
WRITE_EDGES = ("UNWIND $rows AS r MATCH ()-[e:RELATES_TO {uuid: r.u}]->() "
               "SET e.fact_embedding = vecf32(r.v) RETURN count(e)")
SAMPLE_NODE_VEC = "UNWIND $u AS id MATCH (n:Entity {uuid: id}) RETURN n.uuid, n.name_embedding"
SAMPLE_EDGE_VEC = "UNWIND $u AS id MATCH ()-[e:RELATES_TO {uuid: id}]->() RETURN e.uuid, e.fact_embedding"


# ---------- 纯函数（有测试） ----------

def in_window(now: datetime, window=WINDOW) -> bool:
    (h1, m1), (h2, m2) = window
    cur = now.hour * 60 + now.minute
    start, end = h1 * 60 + m1, h2 * 60 + m2
    return (cur >= start or cur < end) if start > end else (start <= cur < end)


def text_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def plan_updates(current: dict[str, str], computed: dict[str, str]) -> tuple[list[str], list[str]]:
    """current/computed：uuid → 文本哈希。返回 (要补算的 uuid, 可直接写的 uuid)。
    检查点之后新增或文本变了的要补算；检查点里有、线上已删除的跳过（写回时 MATCH 不到也无害）。"""
    redo = sorted(u for u, h in current.items() if computed.get(u) != h)
    ready = sorted(u for u, h in current.items() if computed.get(u) == h)
    return redo, ready


def batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def model_slug(model: str) -> str:
    return model.replace("/", "--")


# ---------- IO ----------

FALKOR_KEYS = ("KG_HUB_FALKORDB_HOST", "KG_HUB_FALKORDB_PORT", "KG_HUB_FALKORDB_PASSWORD",
               "KG_HUB_FALKORDB_DATABASE")
_ENV_FILES = ("~/.claude-mem/.env",)


def falkordb_settings(env: dict | None = None, files: tuple[str, ...] = _ENV_FILES) -> dict[str, str]:
    """只取 FalkorDB 连接的四个键：环境变量优先，其次逐行读本机 env 文件里的这四个键。
    不整体加载那个文件——它里面还有网关凭据，本工具用不到（tests/test_model_gateway_client 的契约）。"""
    env = os.environ if env is None else env
    out = {k: env[k] for k in FALKOR_KEYS if env.get(k)}
    for f in files:
        try:
            lines = Path(f).expanduser().read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            key, sep, val = line.partition("=")
            if sep and key in FALKOR_KEYS and key not in out:
                val = val.strip().strip('"').strip("'")
                if val:
                    out[key] = val
    out.setdefault("KG_HUB_FALKORDB_HOST", "100.123.208.32")
    return out


def graph():
    from tools.eval_recall import _graph
    for k, v in falkordb_settings().items():
        os.environ.setdefault(k, v)
    return _graph()


def read_all(g, query: str, sleep: float = 0.2) -> list[tuple]:
    out, skip = [], 0
    while True:
        rows = g.ro_query(query, {"s": skip, "l": PAGE}).result_set
        out.extend(tuple(r) for r in rows)
        skip += PAGE
        if len(rows) < PAGE:
            return out
        time.sleep(sleep)


def graph_model(g) -> str:
    rows = g.ro_query(MARKER_READ).result_set
    return graph_model_from_rows([{"model": r[0]} for r in rows])


def http(method: str, url: str, body: dict | None, token: str | None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def embed(model: str, texts: list[str], cache_dir: str | None) -> "np.ndarray":
    import numpy as np
    from fastembed import TextEmbedding
    m = TextEmbedding(model_name=model, **({"cache_dir": cache_dir} if cache_dir else {}))
    v = np.asarray(list(m.embed(texts, batch_size=256)), dtype=np.float32)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-12)


def save_ckpt(path: Path, uuids: list[str], hashes: list[str], vecs) -> None:
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, uuids=np.array(uuids), hashes=np.array(hashes), vecs=vecs)
    tmp.replace(path)


def load_ckpt(path: Path):
    import numpy as np
    z = np.load(path, allow_pickle=False)
    return list(z["uuids"]), list(z["hashes"]), z["vecs"]


# ---------- 子命令 ----------

def cmd_plan(args) -> int:
    g = graph()
    nodes = g.ro_query("MATCH (n:Entity) RETURN count(n)").result_set[0][0]
    edges = g.ro_query("MATCH ()-[e:RELATES_TO]->() RETURN count(e)").result_set[0][0]
    print(f"实体 {nodes} · 边 {edges} · 图里向量来源：{graph_model(g)} · 目标：{args.model}")
    d = Path(args.dir).expanduser()
    for f in sorted(d.glob("*.npz")) if d.exists() else []:
        print(f"  {f.name}  {f.stat().st_size/2**20:.0f} MiB  {datetime.fromtimestamp(f.stat().st_mtime):%F %T}")
    return 0


def cmd_backup(args) -> int:
    import numpy as np
    g = graph(); d = Path(args.dir).expanduser()
    src = graph_model(g)
    for kind, q in (("nodes", READ_NODE_VECS), ("edges", READ_EDGE_VECS)):
        rows = [(u, v) for u, v in read_all(g, q) if v is not None]
        save_ckpt(d / f"backup-{kind}.npz", [u for u, _ in rows], [""] * len(rows),
                  np.asarray([v for _, v in rows], dtype=np.float32))
        print(f"备份 {kind} {len(rows)} 条 → {d / f'backup-{kind}.npz'}")
    (d / "backup-model.txt").write_text(src)
    print(f"备份时图里向量来源：{src}")
    return 0


def cmd_compute(args) -> int:
    g = graph(); d = Path(args.dir).expanduser()
    for kind, q in (("nodes", READ_NODES), ("edges", READ_EDGES)):
        rows = read_all(g, q)
        t = time.time()
        vecs = embed(args.model, [x or "" for _, x in rows], args.cache_dir)
        save_ckpt(d / f"{model_slug(args.model)}-{kind}.npz", [u for u, _ in rows],
                  [text_hash(x) for _, x in rows], vecs)
        print(f"{kind} {len(rows)} 条，用时 {time.time()-t:.0f}s → {model_slug(args.model)}-{kind}.npz", flush=True)
    return 0


def _drain(base: str, token: str | None, seconds: int) -> dict:
    code, d = http("POST", f"{base}/api/drain", {"seconds": seconds}, token)
    if code != 200:
        raise RuntimeError(f"排空接口返回 {code}")
    return d


def _wait_idle(base: str, token: str | None, limit_sec: int = 1800) -> None:
    t = time.time()
    while time.time() - t < limit_sec:
        code, d = http("GET", f"{base}/health", None, None)
        if code == 200 and int(d.get("active_extractions", 1)) == 0:
            return
        time.sleep(10)
    raise RuntimeError("等不到在飞写入归零")


def _write_vectors(g, kind: str, uuids, vecs, base, token, size: int, sleep: float) -> int:
    q = WRITE_NODES if kind == "nodes" else WRITE_EDGES
    done = 0
    for i, chunk in enumerate(batches(list(range(len(uuids))), size)):
        if i % 20 == 0:
            _drain(base, token, DRAIN_RENEW_SEC)
        rows = [{"u": uuids[j], "v": [float(x) for x in vecs[j]]} for j in chunk]
        for attempt in range(4):
            try:
                g.query(q, {"rows": rows})
                break
            except Exception as exc:  # noqa: BLE001 —— 链路抖动重试；连续失败就停，标记不动
                if attempt == 3:
                    raise RuntimeError(f"{kind} 第 {i} 批写入失败：{type(exc).__name__}") from exc
                time.sleep(2 ** attempt)
        done += len(chunk)
        if i % 50 == 0:
            print(f"  {kind} {done}/{len(uuids)}", flush=True)
        time.sleep(sleep)
    return done


def _guard_write(args, g, expect_from: str) -> None:
    if not args.allow_outside_window and not in_window(datetime.now(TZ)):
        raise SystemExit("不在 23:00–01:45 写入窗口内（--allow-outside-window 可越过，需有理由）")
    cur = graph_model(g)
    if cur != expect_from:
        raise SystemExit(f"图里向量来源是 {cur}，与预期的 {expect_from} 不符，停止")
    if not args.yes:
        raise SystemExit("写图需要 --yes")


def cmd_write(args) -> int:
    from tools.agent_extract import api_token
    import numpy as np
    g = graph(); d = Path(args.dir).expanduser(); base = args.url.rstrip("/"); token = api_token()
    if args.model not in PROFILES:
        raise SystemExit(f"{args.model} 未在 utils/embedding_profile.PROFILES 登记")
    _guard_write(args, g, expect_from=args.expect_from)
    try:
        print("进入排空态并等待在飞写入归零 …", flush=True)
        _drain(base, token, DRAIN_RENEW_SEC); _wait_idle(base, token)
        for kind, q in (("nodes", READ_NODES), ("edges", READ_EDGES)):
            uu, hh, vv = load_ckpt(d / f"{model_slug(args.model)}-{kind}.npz")
            texts = dict(read_all(g, q))
            cur = {u: text_hash(x) for u, x in texts.items()}
            redo, ready = plan_updates(cur, {str(u): h for u, h in zip(uu, hh)})
            pos = {str(u): i for i, u in enumerate(uu)}
            extra = embed(args.model, [texts[u] or "" for u in redo], args.cache_dir) if redo else np.zeros((0, vv.shape[1]), np.float32)
            uuids = ready + redo
            vecs = np.vstack([vv[[pos[u] for u in ready]], extra]) if ready else extra
            print(f"{kind}：检查点可直接写 {len(ready)}，补算 {len(redo)}", flush=True)
            _write_vectors(g, kind, uuids, vecs, base, token, args.batch, args.sleep)
        g.query(MARKER_WRITE, {"model": args.model, "updated_at": datetime.now(timezone.utc).isoformat(),
                               "updated_by": "tools/reembed.py write"})
        print(f"标记已更新为 {args.model}。现在按发布流程把 KG_HUB_EMBED_MODEL 切到它并重启服务。")
    finally:
        _drain(base, token, 0)
        print("已解除排空态")
    return 0


def cmd_restore(args) -> int:
    from tools.agent_extract import api_token
    g = graph(); d = Path(args.dir).expanduser(); base = args.url.rstrip("/"); token = api_token()
    back_model = (d / "backup-model.txt").read_text().strip()
    _guard_write(args, g, expect_from=args.expect_from)
    try:
        _drain(base, token, DRAIN_RENEW_SEC); _wait_idle(base, token)
        for kind in ("nodes", "edges"):
            uu, _, vv = load_ckpt(d / f"backup-{kind}.npz")
            _write_vectors(g, kind, uu, vv, base, token, args.batch, args.sleep)
        if back_model == DEFAULT_MODEL:
            g.query("MATCH (p:EmbeddingProfile {key: 'active'}) DELETE p")
        else:
            g.query(MARKER_WRITE, {"model": back_model, "updated_at": datetime.now(timezone.utc).isoformat(),
                                   "updated_by": "tools/reembed.py restore"})
        print(f"已写回备份向量，图里向量来源恢复为 {back_model}。把 KG_HUB_EMBED_MODEL 改回并重启服务。")
    finally:
        _drain(base, token, 0)
    return 0


def cmd_verify(args) -> int:
    import numpy as np
    import random
    g = graph(); d = Path(args.dir).expanduser(); bad = 0
    for kind, q in (("nodes", SAMPLE_NODE_VEC), ("edges", SAMPLE_EDGE_VEC)):
        uu, _, vv = load_ckpt(d / f"{model_slug(args.model)}-{kind}.npz")
        pos = {u: i for i, u in enumerate(uu)}
        sample = random.Random(0).sample(uu, min(args.n, len(uu)))
        rows = g.ro_query(q, {"u": sample}).result_set
        sims = [float(np.dot(np.asarray(v, np.float32), vv[pos[u]])) for u, v in rows if v is not None]
        low = sum(1 for s in sims if s < 0.999)
        bad += low
        print(f"{kind}：抽 {len(sample)} 读回 {len(sims)}，与检查点余弦 < 0.999 的 {low} 条（最小 {min(sims):.4f}）")
    print("图里向量来源：", graph_model(g))
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "backup", "compute", "write", "restore", "verify"])
    ap.add_argument("--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    ap.add_argument("--expect-from", default=DEFAULT_MODEL, help="写图前图里向量应当出自的模型")
    ap.add_argument("--dir", default=os.environ.get("KG_HUB_REEMBED_DIR", DEFAULT_DIR))
    ap.add_argument("--cache-dir", default=None, help="fastembed 模型缓存目录")
    ap.add_argument("--url", default=os.environ.get("KG_HUB_URL", DEFAULT_URL))
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--n", type=int, default=500, help="verify 抽样数")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--allow-outside-window", action="store_true")
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "backup": cmd_backup, "compute": cmd_compute, "write": cmd_write,
            "restore": cmd_restore, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
