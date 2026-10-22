"""tools/extract_bench：链路遵从判定、纪律聚合、top-k 命中、冻结→自检→评分端到端（假嵌入，不加载模型）。"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import json
import tempfile
import unittest
from unittest import mock

import numpy as np

from schema import EDGE_TYPES, ENTITY_TYPES
from tools import extract_bench as xb

TITLE = "边向量索引灰度第 4 次采样：零回退"


def item(edges, entities=None, **kw):
    ents = entities if entities is not None else [{"name": "A", "type": "Concept"}, {"name": "B", "type": "Tool"}]
    return {"id": 1, "entities": ents, "edges": edges, **kw}


def edge(fact, s="A", t="B", name="relates_to"):
    return {"source": s, "target": t, "name": name, "fact": fact}


class ItemMetricsTest(unittest.TestCase):
    def m(self, it, title=TITLE):
        return xb.item_metrics(it, title, ENTITY_TYPES, EDGE_TYPES)

    def test_headline_verbatim_and_translation(self):
        good = self.m(item([edge(TITLE + "（后被推翻：其实有回退）"), edge("Edge vector index canary sample 4: zero fallback"),
                            edge("A relates to B because the canary sampled the HNSW index path with zero fallbacks")]))
        self.assertTrue(good["headline_verbatim"])        # 原文后加推翻说明也算照抄
        self.assertTrue(good["second_other_lang"])
        self.assertEqual((good["rest_facts"], good["rest_en"]), (1, 1))
        # 改写过的标题、第二条仍是中文
        bad = self.m(item([edge("边向量索引第四次灰度采样没有回退"), edge("同上的中文复述")]))
        self.assertFalse(bad["headline_verbatim"])
        self.assertFalse(bad["second_other_lang"])
        # 英文标题：第二条要是中文
        en = self.m(item([edge("Index canary 4: zero fallback"), edge("索引灰度第 4 次：零回退")]), "Index canary 4: zero fallback")
        self.assertTrue(en["second_other_lang"])

    def test_discipline_counts_and_bad_edges(self):
        m = self.m(item([edge(TITLE), edge("x", t="不存在"), edge("y", name="BINDS"), {"source": "A", "fact": "z"}],
                        entities=[{"name": "A", "type": "Concept"}, {"name": "B", "type": "Gadget"}]))
        self.assertEqual((m["edges_in"], m["dangling_edge"], m["offschema_edge"], m["unknown_type"]), (4, 2, 1, 1))
        self.assertFalse(m["missing_edges_key"])
        self.assertTrue(self.m({"id": 1, "entities": [{"name": "A", "type": "Concept"}]})["missing_edges_key"])
        self.assertEqual(self.m(None), {"present": False, "nonempty": False})
        self.assertEqual(self.m(item([], entities=[])), {"present": True, "nonempty": False})

    def test_aggregate(self):
        ms = [self.m(item([edge(TITLE), edge("English translation of the title here")])),
              self.m(item([edge("改写"), edge("也是中文")])), self.m(None), self.m(item([], entities=[]))]
        r = xb.aggregate(ms)
        self.assertEqual((r["items"], r["missing"], r["empty"]), (4, 1, 1))
        self.assertEqual((r["headline_verbatim_pct"], r["second_other_lang_pct"]), (50.0, 50.0))
        self.assertEqual(r["edge_per_obs"], 2.0)


class HitTest(unittest.TestCase):
    def test_hit_at_k_counts_competitors_across_pools(self):
        q = np.array([1.0, 0.0])
        own = np.array([[0.9, 0.436]])                    # 自己最好的一条与问句相似度 0.9
        strong = np.array([[0.95, 0.31]] * 6)             # 6 条更像的干扰项
        weak = np.array([[0.1, 0.99]] * 50)
        self.assertTrue(xb.hit_at_k(q, own, [strong, weak], k=10))
        self.assertFalse(xb.hit_at_k(q, own, [strong, strong], k=10))   # 两个池子合计 12 条排在前面
        self.assertFalse(xb.hit_at_k(q, np.zeros((0, 2)), [weak]))      # 没有自己的 fact 不算命中


def fake_embed(texts, cache_dir=None):
    """按文本哈希出确定的单位向量；问句与它的标题 fact 共用前 6 个字，让它们靠得近。"""
    out = []
    for t in texts:
        rng = np.random.default_rng(abs(hash(t[:6])) % (2 ** 32))
        v = rng.normal(size=16)
        out.append(v / np.linalg.norm(v))
    return np.array(out, dtype=np.float32)


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = _Path(self.tmp.name)
        src, corpus = root / "exp", root / "corpus"
        src.mkdir(); corpus.mkdir()
        rounds = {"b1": ["haiku", "c8", 13], "b2": ["opus", "c8", 13], "b0": ["opus", "c7", 12]}
        (src / "rounds.json").write_text(json.dumps(rounds))
        titles = {1: "甲观测标题一", 2: "乙观测标题二", 3: "丙观测标题三"}
        for b, ids in (("b1", [1]), ("b2", [2]), ("b0", [3])):
            (src / f"batch-{b}.json").write_text(json.dumps({"batch_id": b, "lookahead": [], "items": [
                {"id": i, "name": f"obs-{i}", "source_description": "sd", "episode_body": f"[DISCOVERY] {titles[i]}\nbody"}
                for i in ids]}))
            (src / f"results-{b}.json").write_text(json.dumps({"items": [
                item([edge(titles[i]), edge(f"English title {i}")], id=i) for i in ids]}))
        (src / "titles-chunk-0.q.tsv").write_text("".join(f"obs:{i}\t{titles[i]}？\tq{i}\n" for i in titles))
        (corpus / "archived.json").write_text("[]")
        (corpus / "edges.jsonl").write_text(json.dumps({"f": "语料里的一句", "e": ["ep"]}) + "\n")
        np.save(corpus / "vec-minilm.npy", fake_embed(["语料里的一句"]))
        with mock.patch.object(xb, "embed", fake_embed):
            self.bench = xb.freeze(src, "c8", "bench-t", corpus, root / "bench")
        self.titles = titles

    def tearDown(self):
        self.tmp.cleanup()

    def test_freeze_keeps_only_the_chain_and_background_excludes_sample(self):
        m = json.loads((self.bench / "manifest.json").read_text())
        self.assertEqual([p["part"] for p in m["parts"]], ["b1", "b2"])
        self.assertEqual((m["items"], m["questions"]), (2, 2))
        self.assertEqual(json.loads((self.bench / "background.json").read_text()),
                         sorted(["丙观测标题三", "English title 3"]))      # 样本外批次的 fact 才进背景
        with self.assertRaises(SystemExit):                                  # 冻结样本不覆盖
            xb.freeze(_Path(self.tmp.name) / "exp", "c8", "bench-t", _Path(self.tmp.name) / "corpus",
                      _Path(self.tmp.name) / "bench")

    def test_lint_and_score_against_baseline(self):
        p, m = xb._bench(str(self.bench))
        cand = {1: item([edge("改写了标题"), edge("还是中文")], id=1)}        # 2 没交
        probs = xb.lint(p, m, cand)
        self.assertTrue(any("2: 没有结果" in x for x in probs))
        self.assertTrue(any("规则 21" in x for x in probs) and any("规则 22" in x for x in probs))
        self.assertEqual(xb.lint(p, m, xb.load_results([str(self.bench / "baseline-b1.json"),
                                                       str(self.bench / "baseline-b2.json")])), [])
        with mock.patch.object(xb, "embed", fake_embed):
            xb._corpus_cache.clear()
            rows = xb.score(p, m, cand, "qoder-x", tokens=4000, wall_s=10)
        by = {r["label"]: r for r in rows}
        self.assertEqual(by["qoder-x（全样本）"]["missing"], 1)
        self.assertEqual(by["qoder-x（全样本）"]["tokens_per_obs"], 2000)
        self.assertEqual(by["基线：三模型各自那部分"]["headline_verbatim_pct"], 100.0)
        self.assertEqual(by["haiku（基线）"]["hit_zh_pct"], 100.0)            # 问句与标题 fact 前 6 字相同
        self.assertEqual(by["qoder-x｜opus 那部分"]["hit_zh_pct"], 0.0)       # 没交的那条不命中
        self.assertIn("| qoder-x（全样本） |", xb.render(rows))


if __name__ == "__main__":
    unittest.main()
