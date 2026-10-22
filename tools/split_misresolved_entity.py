"""把被误并进别的节点的实体拆回来（2026-10-22 补交 b0024）。

背景：会话自抽取正式提交时，同一批前面条目先建了 `validate 阶段相似度计数`，后面 5 条观测里的
`prevalidate 阶段相似度计数` 按名字近似被并了进去——它的边、提及都挂在了 validate 节点上，图里没有
prevalidate 节点。入图侧已补前缀守卫与 dry-run 同批模拟；本工具处理已经写进图里的那次。

认边的依据是执行器结果文件：误并实体在哪几条观测里出现、作哪条边的哪一端、fact 原文是什么。
图里同一个误并节点上、episodes 含该观测、fact 原文相同的那条边，就是要搬的边。

模式：
  --plan RESULTS --batch BATCH --wrong NAME --entity NAME
                    本机：从结果文件生成 spec（JSON 打到 stdout）。纯读文件。
  --spec SPEC [--manifest OUT]
                    容器内 dry-run，只读（ro_query）：按 spec 在图里认出要搬的边与要改的提及，
                    打印报告；带 --manifest 时冻结成清单，apply/restore 只认清单。
  --apply MANIFEST  持 writer lock：新建实体节点（名字向量现算）；清单里的边按原 uuid、原属性改接到新节点；
                    只在误并实体出现、误并节点本身不在的观测里，把提及从误并节点改指新节点。每条边都要求
                    当前端点与 fact 仍等于清单记录，否则跳过。可重跑。
  --restore MANIFEST
                    反向：边接回误并节点、提及恢复原 uuid、删除新节点（新节点上还有清单以外的边时不删）。

为什么重建边而不是改端点：FalkorDB 的关系不能换端点，只能删了按原属性再建；uuid 不变，episode 的
entity_edges 列表不用动。

在 kg-hub-server 容器内跑（FalkorDB 只在 NAS 本地可达），需 PYTHONPATH=/app。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOOL = "split_misresolved_entity"
TIMEOUT_MS = 60_000

FIND_NODE = ("MATCH (n:Entity) WHERE toLower(n.name) = $lname "
             "RETURN n.uuid, n.name, labels(n), n.group_id, n.extractor_model, n.extraction_mode, "
             "n.extractor_tool, n.run_id")
NODE_EDGES = ("MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity) WHERE a.uuid = $u OR b.uuid = $u "
              "RETURN e.uuid, a.uuid, b.uuid, e.fact, e.episodes")
EPISODES = "MATCH (ep:Episodic) WHERE ep.uuid IN $l RETURN ep.uuid, ep.name"
EPISODES_BY_NAME = "MATCH (ep:Episodic) WHERE ep.name IN $l RETURN ep.uuid, ep.name"
MENTIONS = ("MATCH (ep:Episodic)-[m:MENTIONS]->(n:Entity {uuid: $u}) WHERE ep.uuid IN $l "
            "RETURN ep.uuid, m.uuid, m.group_id, m.created_at")
EDGE_NOW = ("MATCH (a:Entity)-[e:RELATES_TO {uuid: $u}]->(b:Entity) "
            "RETURN a.uuid, b.uuid, e.fact, properties(e)")
SAVE_NODE = ("MERGE (n:Entity {uuid: $d.uuid}) SET n:{labels} SET n = $d "
             "SET n.name_embedding = vecf32($d.name_embedding) RETURN n.uuid")
# 端点用 uuid 锁定：只在当前两端仍是清单记录的那两个节点时才删旧建新
MOVE_EDGE = ("MATCH (a:Entity {uuid: $src})-[e:RELATES_TO {uuid: $u}]->(b:Entity {uuid: $dst}) "
             "MATCH (s:Entity {uuid: $new_src}) MATCH (t:Entity {uuid: $new_dst}) "
             "WITH e, s, t DELETE e "
             "CREATE (s)-[n:RELATES_TO]->(t) SET n = $props "
             "SET n.fact_embedding = vecf32($props.fact_embedding) RETURN count(n)")
ADD_MENTION = ("MATCH (ep:Episodic {uuid: $ep}) MATCH (n:Entity {uuid: $n}) "
               "MERGE (ep)-[m:MENTIONS {uuid: $m}]->(n) SET m.group_id = $g, m.created_at = $c RETURN count(m)")
DROP_MENTION = ("MATCH (ep:Episodic {uuid: $ep})-[m:MENTIONS]->(n:Entity {uuid: $n}) "
                "DELETE m RETURN count(m)")
NODE_DEGREE = "MATCH (n:Entity {uuid: $u}) OPTIONAL MATCH (n)-[r]-() RETURN count(r)"
DELETE_NODE = "MATCH (n:Entity {uuid: $u}) WHERE NOT (n)--() DELETE n RETURN count(n)"


def _key(name: str) -> str:
    return " ".join((name or "").split()).lower()


# ---------- 纯函数（有测试） ----------

def plan_from_results(results: dict, batch: dict, wrong: str, entity: str) -> dict:
    """结果文件 → spec：误并实体出现在哪些观测、作哪些边的哪一端。"""
    names = {int(i["id"]): i["name"] for i in batch["items"]}
    ek, wk = _key(entity), _key(wrong)
    spec = {"tool": TOOL, "wrong_name": wrong, "entity": None, "episodes": [], "edges": []}
    for it in results.get("items", []):
        ents = {_key(e["name"]): e for e in it.get("entities", [])}
        if ek not in ents:
            continue
        if spec["entity"] is None:
            e = ents[ek]
            spec["entity"] = {"name": " ".join(e["name"].split()), "type": e["type"], "summary": e.get("summary", "")}
        ep = names[int(it["id"])]
        spec["episodes"].append({"episode": ep, "keep_wrong_mention": wk in ents})
        for ed in it.get("edges", []):
            s, t = _key(ed["source"]), _key(ed["target"])
            if ek in (s, t):
                spec["edges"].append({"episode": ep, "fact": " ".join(ed["fact"].split()),
                                      "end": "source" if s == ek else "target"})
    if spec["entity"] is None:
        raise ValueError(f"结果文件里没有实体 {entity!r}")
    return spec


def select_moves(spec: dict, wrong_uuid: str, graph_edges: list, ep_names: dict) -> dict:
    """graph_edges: 误并节点上的边 (uuid, src, dst, fact, episodes)；ep_names: episode uuid → name。

    返回 {"moves": [...], "unmatched": [spec 边], "shared": [边 uuid]}：shared 是 episodes 不止一条的边
    （被别的观测复用过），搬了会把那条观测的事实一起搬走，不动，报出来人工看。"""
    moves, unmatched, shared, used = [], [], [], set()
    for want in spec["edges"]:
        hits = [g for g in graph_edges
                if g[3] == want["fact"] and want["episode"] in {ep_names.get(x) for x in (g[4] or [])}
                and (g[1] if want["end"] == "source" else g[2]) == wrong_uuid and g[0] not in used]
        if not hits:
            unmatched.append(want)
            continue
        g = hits[0]
        if len(g[4] or []) > 1:
            shared.append(g[0])
            continue
        used.add(g[0])
        moves.append({"uuid": g[0], "end": want["end"], "src": g[1], "dst": g[2], "fact": g[3],
                      "episode": want["episode"]})
    return {"moves": moves, "unmatched": unmatched, "shared": shared}


def moved_endpoints(move: dict, wrong_uuid: str, new_uuid: str, *, back: bool = False) -> tuple:
    """(当前 src, 当前 dst, 目标 src, 目标 dst)。apply：误并端 → 新节点；restore 反过来。"""
    src, dst = move["src"], move["dst"]
    if move["end"] == "source":
        moved = (new_uuid, dst) if not back else (src, dst)
        current = (src, dst) if not back else (new_uuid, dst)
    else:
        moved = (src, new_uuid) if not back else (src, dst)
        current = (src, dst) if not back else (src, new_uuid)
    assert (src if move["end"] == "source" else dst) == wrong_uuid
    return (*current, *moved)


# ---------- 读写图 ----------

def _graph():
    from falkordb import FalkorDB
    return FalkorDB(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "falkordb"), port=6379,
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    ).select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))


def _ro(graph, q, **p):
    return graph.ro_query(q, p, timeout=TIMEOUT_MS).result_set


def _rw(graph, q, **p):
    return graph.query(q, p, timeout=TIMEOUT_MS).result_set


def dry_run(graph, spec: dict) -> dict:
    wrong = _ro(graph, FIND_NODE, lname=_key(spec["wrong_name"]))
    if len(wrong) != 1:
        raise SystemExit(f"误并节点 {spec['wrong_name']!r} 应恰好一个，图里有 {len(wrong)} 个")
    w = wrong[0]
    if _ro(graph, FIND_NODE, lname=_key(spec["entity"]["name"])):
        raise SystemExit(f"图里已有 {spec['entity']['name']!r}：先人工确认，不自动合并")
    edges = [tuple(r) for r in _ro(graph, NODE_EDGES, u=w[0])]
    ep_uuids = sorted({x for r in edges for x in (r[4] or [])})
    ep_names = {u: n for u, n in _ro(graph, EPISODES, l=ep_uuids)}
    sel = select_moves(spec, w[0], edges, ep_names)
    by_name = {n: u for u, n in _ro(graph, EPISODES_BY_NAME, l=[e["episode"] for e in spec["episodes"]])}
    old_mentions = {r[0]: {"uuid": r[1], "group_id": r[2], "created_at": r[3]}
                    for r in _ro(graph, MENTIONS, u=w[0], l=list(by_name.values()))}
    mentions = [{"episode": e["episode"], "episode_uuid": by_name.get(e["episode"]),
                 "keep_wrong_mention": e["keep_wrong_mention"],
                 "wrong_mention": old_mentions.get(by_name.get(e["episode"]))} for e in spec["episodes"]]
    return {"tool": TOOL, "created_at": datetime.now(timezone.utc).isoformat(),
            "graph": os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"),
            "wrong": {"uuid": w[0], "name": w[1], "labels": w[2], "group_id": w[3]},
            "entity": {**spec["entity"], "uuid": str(uuidlib.uuid4()),
                       "provenance": {"extractor_model": w[4], "extraction_mode": w[5],
                                      "extractor_tool": w[6], "run_id": w[7]}},
            "edges": sel["moves"], "mentions": mentions,
            "unmatched": sel["unmatched"], "shared": sel["shared"],
            "missing_episodes": [m["episode"] for m in mentions if not m["episode_uuid"]]}


def _embed(name: str) -> list[float]:
    from fastembed import TextEmbedding
    from utils.embedding_profile import active_profile
    kwargs = {"model_name": active_profile().model}
    if os.environ.get("FASTEMBED_CACHE_PATH"):
        kwargs["cache_dir"] = os.environ["FASTEMBED_CACHE_PATH"]
    return [float(x) for x in next(iter(TextEmbedding(**kwargs).embed([name])))]


def _move_edges(graph, m: dict, *, back: bool) -> dict:
    wrong, new = m["wrong"]["uuid"], m["entity"]["uuid"]
    out = {"moved": 0, "already": 0, "skipped": []}
    for mv in m["edges"]:
        cs, cd, ts, td = moved_endpoints(mv, wrong, new, back=back)
        now = _ro(graph, EDGE_NOW, u=mv["uuid"])
        if now and (now[0][0], now[0][1], now[0][2]) == (ts, td, mv["fact"]):
            out["already"] += 1     # 重跑：上次已经搬过
            continue
        if not now or (now[0][0], now[0][1], now[0][2]) != (cs, cd, mv["fact"]):
            out["skipped"].append(mv["uuid"])
            continue
        props = dict(now[0][3])
        n = _rw(graph, MOVE_EDGE, src=cs, dst=cd, u=mv["uuid"], new_src=ts, new_dst=td, props=props)
        out["moved"] += n[0][0] if n else 0
    return out


def _locked(fn):
    from utils.writer_lock import writer_lock
    with writer_lock(owner=TOOL, timeout_seconds=120):
        return fn()


def apply(graph, m: dict) -> dict:
    def run():
        e, w = m["entity"], m["wrong"]
        now_iso = datetime.now(timezone.utc).isoformat()
        if not _ro(graph, "MATCH (n:Entity {uuid: $u}) RETURN n.uuid", u=e["uuid"]):
            d = {"uuid": e["uuid"], "name": e["name"], "name_embedding": _embed(e["name"]),
                 "group_id": w["group_id"], "summary": e["summary"], "created_at": now_iso,
                 **e["provenance"]}
            _rw(graph, SAVE_NODE.replace("{labels}", f"{e['type']}:Entity"), d=d)
        res = _move_edges(graph, m, back=False)
        res["mentions_added"] = res["mentions_dropped"] = 0
        for mt in m["mentions"]:
            if not mt["episode_uuid"]:
                continue
            res["mentions_added"] += _rw(graph, ADD_MENTION, ep=mt["episode_uuid"], n=e["uuid"],
                                         m=str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"{e['uuid']}:{mt['episode_uuid']}")),
                                         g=w["group_id"], c=now_iso)[0][0]
            if not mt["keep_wrong_mention"] and mt["wrong_mention"]:
                res["mentions_dropped"] += _rw(graph, DROP_MENTION, ep=mt["episode_uuid"], n=w["uuid"])[0][0]
        return res
    return _locked(run)


def restore(graph, m: dict) -> dict:
    def run():
        e, w = m["entity"], m["wrong"]
        res = _move_edges(graph, m, back=True)
        res["mentions_restored"] = 0
        for mt in m["mentions"]:
            if not mt["episode_uuid"]:
                continue
            _rw(graph, DROP_MENTION, ep=mt["episode_uuid"], n=e["uuid"])
            old = mt["wrong_mention"]
            if not mt["keep_wrong_mention"] and old:
                res["mentions_restored"] += _rw(graph, ADD_MENTION, ep=mt["episode_uuid"], n=w["uuid"],
                                                m=old["uuid"], g=old["group_id"], c=old["created_at"])[0][0]
        degree = _ro(graph, NODE_DEGREE, u=e["uuid"])
        res["node_left_edges"] = degree[0][0] if degree else 0
        res["node_deleted"] = _rw(graph, DELETE_NODE, u=e["uuid"])[0][0] if res["node_left_edges"] == 0 else 0
        return res
    return _locked(run)


def _read_manifest(path: str) -> dict:
    m = json.loads(Path(path).read_text())
    if m.get("tool") != TOOL:
        raise ValueError(f"{path} 不是 {TOOL} 的清单")
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把误并进别的节点的实体拆回来")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", metavar="RESULTS")
    mode.add_argument("--spec", metavar="SPEC")
    mode.add_argument("--apply", metavar="MANIFEST")
    mode.add_argument("--restore", metavar="MANIFEST")
    ap.add_argument("--batch", help="--plan：批次文件（观测 id → episode 名）")
    ap.add_argument("--wrong", help="--plan：被并进去的节点名")
    ap.add_argument("--entity", help="--plan：被误并的实体名")
    ap.add_argument("--manifest", help="--spec：冻结清单的输出路径")
    a = ap.parse_args(argv)
    if a.plan:
        spec = plan_from_results(json.loads(Path(a.plan).read_text()), json.loads(Path(a.batch).read_text()),
                                 a.wrong, a.entity)
        print(json.dumps(spec, ensure_ascii=False, indent=1))
        return 0
    graph = _graph()
    if a.spec:
        m = dry_run(graph, json.loads(Path(a.spec).read_text()))
        if a.manifest:
            Path(a.manifest).write_text(json.dumps(m, ensure_ascii=False, indent=1))
        print(json.dumps(m, ensure_ascii=False, indent=1))
        return 0
    m = _read_manifest(a.apply or a.restore)
    res = (apply if a.apply else restore)(graph, m)
    print(json.dumps({"mode": "apply" if a.apply else "restore", **res}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
