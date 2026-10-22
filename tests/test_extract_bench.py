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


class AdmissionChecksTest(unittest.TestCase):
    ROW = {"label": "x（全样本）", "items": 50, "missing": 0, "headline_verbatim_pct": 100.0, "second_other_lang_pct": 98.0,
           "hit_zh_pct": 96.0, "hit_en_pct": 100.0, "offschema_edge_pct": 0.0, "dangling_edge_pct": 0.0, "unknown_type": 0}

    def verdict(self, row=None, pairs=(), prompt="agent-v4+bench1", lint=0, blind=None):
        return xb.admission_checks(dict(self.ROW, **(row or {})), list(pairs), run_prompt=prompt,
                                   baseline_prompt="agent-v4", lint_problems=lint, blind=blind)

    def test_verdict_levels(self):
        ok_blind = {"judged": 10, "errors": 1}
        self.assertEqual(self.verdict(blind=ok_blind)[1], "pass")
        self.assertEqual(self.verdict()[1], "pending_blind")                        # 没盲评
        self.assertEqual(self.verdict(row={"items": 10}, blind=ok_blind)[1], "screen_pass")   # 10 条只算筛选
        for bad in ({"row": {"missing": 1}}, {"lint": 2}, {"row": {"headline_verbatim_pct": 94.0}},
                    {"row": {"second_other_lang_pct": 90.0}}, {"row": {"hit_zh_pct": 89.0}},
                    {"row": {"dangling_edge_pct": 0.5}}, {"row": {"unknown_type": 1}}, {"prompt": "agent-v3+bench1"},
                    {"blind": {"judged": 10, "errors": 2}}):
            with self.subTest(bad=bad):
                kw = {"blind": ok_blind, **bad}
                self.assertEqual(self.verdict(**kw)[1], "fail")

    def test_subset_must_not_drop_more_than_five_points(self):
        base = {"label": "haiku（基线）", "hit_zh_pct": 100.0, "hit_en_pct": 100.0}
        ok = ({"label": "x｜haiku 那部分", "hit_zh_pct": 95.0, "hit_en_pct": 100.0}, base)
        bad = ({"label": "x｜haiku 那部分", "hit_zh_pct": 94.9, "hit_en_pct": 100.0}, base)
        blind = {"judged": 10, "errors": 0}
        self.assertEqual(self.verdict(pairs=[ok], blind=blind)[1], "pass")
        checks, v = self.verdict(pairs=[bad], blind=blind)
        self.assertEqual(v, "fail")
        self.assertTrue(any("haiku 那部分" in c["check"] and not c["pass"] for c in checks))


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

    def test_blind_sheet_and_admit_writes_record(self):
        p, m = xb._bench(str(self.bench))
        titles = self.titles

        def factory(model, system):
            def call(q):
                obs = json.loads(q.split("观测：", 1)[1])
                body = [item([edge(titles[o["id"]]), edge(f"English {o['id']}")], id=o["id"]) for o in obs]
                return {"text": json.dumps({"items": body}, ensure_ascii=False), "error": None, "stop": "end_turn",
                        "credits": 1.0, "ms": 100}
            return call
        root = _Path(self.tmp.name)
        dirs = xb.run_models(p, m, ["甲模型", "乙模型"], executor="qodercli", out_root=root / "runs", tag="adm",
                             limit=None, chunk=10, parallel=1, call_factory=factory)
        runs = [xb.load_run(d) for d in dirs]
        sheet, key = xb.blind_sheet(p, m, runs, n=2)
        self.assertEqual(len(sheet), 4)
        self.assertNotIn("甲模型", json.dumps(sheet, ensure_ascii=False))           # 评审表里没有模型名
        self.assertEqual(sorted(set(key.values())), ["qodercli-乙模型", "qodercli-甲模型"])
        self.assertTrue(all(e["episode_body"] and e["title"] for e in sheet))     # 评审者能对照原文
        for e in sheet:
            e["verdict"] = "error" if key[e["code"]] == "qodercli-乙模型" else "ok"
        self.assertEqual(xb.blind_result(sheet, key, "qodercli-乙模型")["errors"], 2)
        with mock.patch.object(xb, "embed", fake_embed), mock.patch.object(xb, "BLIND_N", 2), \
                mock.patch.object(xb, "ADMISSION_MIN_ITEMS", 2), mock.patch.dict("os.environ", {"KG_HUB_BENCH_ROOT": str(root)}):
            xb._corpus_cache.clear()
            good = xb.admit(p, m, runs[0], sheet=sheet, key=key)
            bad = xb.admit(p, m, runs[1], sheet=sheet, key=key)
            pending = xb.admit(p, m, runs[0])
            self.assertEqual((good["verdict"], bad["verdict"], pending["verdict"]), ("pass", "fail", "pending_blind"))
            self.assertEqual(xb.latest_admission("甲模型")["verdict"], "pending_blind")   # 最新一条为准
            hist = json.loads((root / "admissions" / "甲模型.json").read_text())["history"]
            self.assertEqual([h["verdict"] for h in hist], ["pass", "pending_blind"])
            self.assertEqual(good["prompt_version"].split("+")[0], "agent-v4")
            self.assertIsNone(xb.latest_admission("没跑过的模型"))

    def test_register_manual_results_as_a_run(self):
        p, m = xb._bench(str(self.bench))
        d = _Path(self.tmp.name) / "manual"
        d.mkdir()
        (d / "results-b1.json").write_text(json.dumps({"items": [item([edge(self.titles[1]), edge("English 1")], id=1),
                                                                 item([edge("x"), edge("y")], id=999)]},
                                                      ensure_ascii=False))
        run = xb.register_run(p, m, d, model="GLM-5.3", executor="qoder", credits=4.0, seconds=60)
        self.assertEqual((run["ids"], run["ignored_ids"], run["label"]), ([1], [999], "qoder-GLM-5.3"))
        self.assertTrue(run["prompt_version"].endswith("+manual"))
        loaded = xb.load_run(d)                                                # blind / admit 用的入口能读
        self.assertEqual((loaded["cost"]["credits_per_obs"], set(loaded["results"])), (4.0, {1, 999}))
        empty = _Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit):
            xb.register_run(p, m, empty, model="M", executor="qoder")

    def test_run_models_writes_runs_and_scores_them_together(self):
        p, m = xb._bench(str(self.bench))
        titles = self.titles

        def factory(model, system):
            def call(q):
                obs = json.loads(q.split("观测：", 1)[1])
                good = model == "好模型"
                body = [item([edge(titles[o["id"]] if good else "改写"), edge(f"English {o['id']}" if good else "中文")],
                             id=o["id"]) for o in obs]
                return {"text": json.dumps({"items": body}, ensure_ascii=False), "error": None, "stop": "end_turn",
                        "credits": 2.0, "ms": 1000}
            return call
        out = _Path(self.tmp.name) / "runs"
        dirs = xb.run_models(p, m, ["好模型", "差模型"], executor="qodercli", out_root=out, tag="t", limit=1,
                             chunk=10, parallel=2, call_factory=factory)
        runs = [xb.load_run(d) for d in dirs]
        self.assertEqual([r["label"] for r in runs], ["qodercli-好模型", "qodercli-差模型"])
        self.assertEqual(runs[0]["ids"], [1])                     # limit 1：按基线模型轮流，先取 haiku 那批
        self.assertEqual((runs[0]["calls"], runs[0]["cost"]["credits_per_obs"]), (1, 2.0))
        self.assertTrue(runs[0]["prompt_version"].endswith("+bench1"))
        self.assertTrue(list((dirs[0] / "work").glob("b1.c0.a1.json")))
        self.assertEqual(xb.lint(p, m, runs[0]["results"], ids=runs[0]["ids"]), [])
        with self.assertRaises(SystemExit):                       # 同标签不覆盖
            xb.run_models(p, m, ["好模型"], executor="qodercli", out_root=out, tag="t", limit=1, chunk=10,
                          parallel=1, call_factory=factory)
        with mock.patch.object(xb, "embed", fake_embed):
            xb._corpus_cache.clear()
            rows = xb.score_many(p, m, [(r["label"], r["results"], r["cost"]) for r in runs], ids=[1])
        by = {r["label"]: r for r in rows}
        self.assertEqual(by["qodercli-好模型（全样本）"]["headline_verbatim_pct"], 100.0)
        self.assertEqual(by["qodercli-差模型（全样本）"]["headline_verbatim_pct"], 0.0)
        self.assertEqual(by["基线：三模型各自那部分"]["items"], 1)       # 基线也只评同一子集
        self.assertIn("qodercli-差模型｜haiku 那部分", by)



class RunnerPureTest(unittest.TestCase):
    def test_select_ids_round_robin_across_baseline_models(self):
        rows = [(1, "t", "b1", "haiku"), (2, "t", "b1", "haiku"), (3, "t", "b2", "opus"), (4, "t", "b3", "sonnet"),
                (5, "t", "b3", "sonnet")]
        self.assertEqual(xb.select_ids(rows, 4), [1, 3, 4, 2])
        self.assertEqual(xb.select_ids(rows, None), [1, 2, 3, 4, 5])
        self.assertEqual(xb.select_ids(rows, 99), [1, 3, 4, 2, 5])

    def test_parse_items_tolerates_fences_and_filters_ids(self):
        txt = '好的：\n```json\n{"items": [{"id": "1", "entities": []}, {"id": 9, "entities": []}]}\n```'
        self.assertEqual(xb.parse_items(txt, [1, 2]), {1: {"id": 1, "entities": []}})
        self.assertEqual(set(xb.parse_items('[{"id": 2, "entities": []}]', [2])), {2})
        self.assertEqual(set(xb.parse_items('{"id": 2, "entities": [], "edges": []}', [2])), {2})
        for bad in ("没有 JSON", '{"foo": 1}'):
            with self.assertRaises(ValueError):
                xb.parse_items(bad, [1])

    def test_system_prompt_carries_rules_and_bench_addendum(self):
        system, version = xb.bench_system_prompt()
        self.assertRegex(version, r"^agent-v\d+\+bench1$")
        self.assertIn("21. **第一条边 = 观测标题原文**", system)
        self.assertTrue(system.endswith(xb.BENCH_ADDENDUM))

    def test_qodercli_adapter_command_and_output(self):
        seen = {}

        class R:
            returncode, stderr = 0, ""
            stdout = json.dumps({"result": '{"items": []}', "is_error": False, "stop_reason": "end_turn",
                                 "total_credits": 3.5, "duration_ms": 1200})

        def fake_run(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return R()
        with mock.patch("subprocess.run", fake_run):
            res = xb.qodercli_call("GLM-5.3", "SYS", "Q", cwd=_Path("/tmp"))
        cmd = seen["cmd"]
        self.assertEqual(cmd[:4], ["qodercli", "-p", "-m", "GLM-5.3"])
        # 不加载用户级设置（claude-mem 钩子在那里）、不给工具、系统提示整体替换
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "local")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "SYS")
        self.assertEqual(cmd[-2:], ["--", "Q"])
        self.assertEqual(seen["kw"]["cwd"], "/tmp")
        self.assertEqual((res["text"], res["error"], res["credits"], res["ms"]), ('{"items": []}', False, 3.5, 1200))
        R.stdout = "not json"
        with mock.patch("subprocess.run", fake_run):
            self.assertTrue(xb.qodercli_call("m", "S", "Q", cwd=_Path("/tmp"))["error"])

    def test_run_chunk_splits_and_retries_missing(self):
        calls = []

        def call(q):
            ids = [o["id"] for o in json.loads(q.split("观测：", 1)[1])]
            calls.append(ids)
            if len(ids) > 2:                                      # 大组：漏掉 3、4
                body = [{"id": i, "entities": []} for i in ids if i not in (3, 4)]
            elif ids == [4]:                                      # 4 永远解析不了
                return {"text": "坏掉的回复", "error": None, "stop": "end_turn", "credits": 1.0, "ms": 10}
            else:
                body = [{"id": i, "entities": []} for i in ids]
            return {"text": json.dumps({"items": body}), "error": None, "stop": "end_turn", "credits": 1.0, "ms": 10}
        items = [{"id": i, "episode_body": "x"} for i in (1, 2, 3, 4, 5)]
        logs = []
        got, st = xb.run_chunk(call, items, [], lambda w, r, e: logs.append((w, e)))
        self.assertEqual(sorted(got), [1, 2, 3, 5])
        self.assertEqual(st["failed"], [4])
        self.assertEqual(calls[:3], [[1, 2, 3, 4, 5], [3], [4]])
        self.assertEqual(st["calls"], len(calls))
        self.assertEqual(st["credits"], float(len(calls)))
        self.assertTrue(any(e for _, e in logs))                   # 解析失败写进日志


if __name__ == "__main__":
    unittest.main()
