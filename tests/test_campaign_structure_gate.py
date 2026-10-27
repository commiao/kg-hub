"""战役结构门槛（tools/campaign_structure_gate.py）：模板化批次被拒、达标批次通过、未启用战役行为不变、
structure_note 例外与模板句、阈值配置与启用 / 关闭审计。

战役流程沿用 test_campaign_prepare 的假取数、假 dry-run / 提交，不碰 NAS 与服务端。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests import test_campaign_prepare as base
from tools import campaign_prepare as cp
from tools import campaign_structure_gate as sg
from tools import extract_campaign as ec

CFG = {**sg.DEFAULTS, "from_chain": "c0"}


def ent(name, typ="Tool"):
    return {"name": name, "type": typ, "summary": f"{name} summary that is long enough to stand on its own."}


def edge(s, t, fact, name="relates_to"):
    return {"source": s, "name": name, "target": t, "fact": fact}


def two_node(i):
    """两节点模板：恰 2 实体、所有边同一对端点（075 批的形状）。"""
    a, b = f"topic_{i}", f"detail_{i}"
    return {"id": i, "entities": [ent(a), ent(b)],
            "edges": [edge(a, b, f"title {i}"), edge(a, b, f"标题 {i}"), edge(a, b, f"fact {i}"), edge(a, b, f"more {i}")]}


def rich(i):
    """4 实体、4 个不同端点对、具体关系。"""
    a, b, c, d = f"worker_{i}.py", f"queue_{i}", f"config_key_{i}", "kg-hub"
    return {"id": i, "entities": [ent(a), ent(b), ent(c), ent(d, "Project")],
            "edges": [edge(a, b, f"title {i}"), edge(a, b, f"标题 {i}"), edge(a, c, "reads", "depends_on"),
                      edge(b, c, "uses", "depends_on"), edge(a, d, "lives in", "belongs_to")]}


def review(notes: dict[int, str]) -> dict:
    return {"items": [{"id": i, "note": "逐条核对过", sg.NOTE_FIELD: n} for i, n in notes.items()]}


class EvaluateTest(unittest.TestCase):
    def test_template_batch_is_refused_with_per_item_list(self):
        res = {"items": [two_node(i) for i in range(10)]}
        ev = sg.evaluate(CFG, res)
        self.assertFalse(ev["ok"])
        self.assertEqual(ev["metrics"]["two_entity_pct"], 100.0)
        self.assertEqual(ev["metrics"]["pairs_per_obs"], 1.0)
        self.assertEqual(len(ev["violations"]), 2)
        self.assertEqual([t["id"] for t in ev["thin"]], list(range(10)))
        lines = sg.refusal_lines("m-001", ev)
        self.assertIn("恰 2 实体占 100.0%", lines[0])
        self.assertTrue(any(line.strip().startswith("m-001 3:") for line in lines))

    def test_rich_batch_passes_and_empty_items_stay_out_of_denominator(self):
        res = {"items": [rich(i) for i in range(8)] + [{"id": 99, "entities": [], "edges": []}]}
        ev = sg.evaluate(CFG, res)
        self.assertTrue(ev["ok"], ev["violations"])
        self.assertEqual((ev["nonempty"], ev["counted"], ev["thin"]), (8, 8, []))
        self.assertEqual(ev["metrics"]["pairs_per_obs"], 4.0)

    def test_thresholds_are_boundaries(self):
        # 3 条恰 2 实体 / 10 条 = 30%：等于上限放行，再多一条就拒
        res = {"items": [two_node(i) for i in range(3)] + [rich(i) for i in range(3, 10)]}
        self.assertNotIn("恰 2", " ".join(sg.evaluate(CFG, res)["violations"]))
        res["items"][3] = two_node(3)
        self.assertIn("恰 2", " ".join(sg.evaluate(CFG, res)["violations"]))
        # 端点对：给个下限 4.0，平均 4.0 放行；4.01 拒
        ok = sg.evaluate({**CFG, "max_two_entity_pct": None, "min_pairs_per_obs": 4.0}, {"items": [rich(1)]})
        self.assertTrue(ok["ok"])
        self.assertFalse(sg.evaluate({**CFG, "max_two_entity_pct": None, "min_pairs_per_obs": 4.01},
                                     {"items": [rich(1)]})["ok"])

    def test_relates_to_warns_by_default_and_rejects_only_when_configured(self):
        res = {"items": [two_node(i) for i in range(2)] + [rich(i) for i in range(2, 10)]}
        for it in res["items"]:
            for g in it["edges"][2:]:
                g["name"] = "relates_to"
        ev = sg.evaluate(CFG, res)
        self.assertTrue(ev["ok"])
        self.assertTrue(any("relates_to" in w for w in ev["warnings"]))
        ev = sg.evaluate({**CFG, "max_relates_to_pct": 90}, res)
        self.assertFalse(ev["ok"])
        self.assertIn("relates_to", ev["violations"][0])

    def test_specific_note_exempts_thin_item(self):
        res = {"items": [two_node(0)] + [rich(i) for i in range(1, 10)]}
        res["items"][0]["edges"] = res["items"][0]["edges"][:2]   # 1 对端点
        cfg = {**CFG, "max_two_entity_pct": 5}
        self.assertFalse(sg.evaluate(cfg, res)["ok"])
        ev = sg.evaluate(cfg, res, review({0: "正文只讲 topic_0 的一次改名，没有点名文件或配置，第二个对象 detail_0 已是全部证据"}))
        self.assertTrue(ev["ok"], ev["violations"])
        self.assertEqual([x["id"] for x in ev["exempt"]], [0])
        self.assertEqual((ev["nonempty"], ev["counted"]), (10, 9))

    def test_template_notes_do_not_count(self):
        res = {"items": [two_node(i) for i in range(3)] + [rich(i) for i in range(3, 20)]}
        cfg = {**CFG, "max_two_entity_pct": 5}
        # 同一句模板，只把本条实体名与编号换掉：全部不算
        tpl = {i: f"第 {50000 + i} 条：topic_{i} 与 detail_{i} 之外正文没有支持第三个实体的证据" for i in range(3)}
        ev = sg.evaluate(cfg, res, review(tpl))
        self.assertFalse(ev["ok"])
        self.assertEqual(ev["exempt"], [])
        self.assertEqual(len(ev["invalid_exempt"]), 3)
        self.assertTrue(all("模板" in x["why"] for x in ev["invalid_exempt"]))
        self.assertEqual(len(ev["thin"]), 3)
        # 不点名实体、太短的也不算；结构达标的条目上的 note 不管
        bad = review({0: "原文证据有限，只能这样抽，没有别的可抽了", 1: "只讲 topic_1", 5: "达标条目上的说明不计入例外"})
        ev = sg.evaluate(cfg, res, bad)
        self.assertEqual([x["id"] for x in ev["invalid_exempt"]], [0, 1])
        self.assertEqual(ev["exempt"], [])

    def test_exemption_cap(self):
        res = {"items": [two_node(i) for i in range(4)] + [rich(i) for i in range(4, 10)]}
        notes = {0: "topic_0 只是被引用的背景主题，正文只描述它和 detail_0 的单一关系",
                 1: "detail_1 是唯一被修改的对象；topic_1 只是归属，原文没有其他文件或命令",
                 2: "原文只说 topic_2 已废弃并由 detail_2 替代，没有提到其他组件、路径或配置项"}
        ev = sg.evaluate(CFG, res, review(notes))      # 10 条非空 × 20% = 2 条上限
        self.assertEqual(len(ev["exempt"]), 3)
        self.assertFalse(ev["ok"])
        self.assertIn("例外 3 条，超过上限 2 条", ev["violations"][-1])
        del notes[2]
        self.assertTrue(sg.evaluate(CFG, res, review(notes))["ok"])

    def test_config_validation_and_chain_applicability(self):
        with self.assertRaises(SystemExit):
            sg.validate_config({"max_two_entity_pct": 130})
        with self.assertRaises(SystemExit):
            sg.validate_config({"min_pairs_per_obs": -1})
        with self.assertRaises(SystemExit):
            sg.validate_config({"max_two_entity_pct": None, "min_pairs_per_obs": None, "max_relates_to_pct": None})
        with self.assertRaises(SystemExit):
            sg.validate_config({"max_exempt_pct": None})
        self.assertEqual(sg.validate_config({}), {k: (None if v is None else float(v)) for k, v in sg.DEFAULTS.items()})
        c = {"structure_gate": {"enabled": True, "from_chain": "c3", **sg.DEFAULTS}}
        self.assertIsNone(sg.config_for(c, {"chain": "c2"}))
        self.assertIsNotNone(sg.config_for(c, {"chain": "c3"}))
        self.assertIsNotNone(sg.config_for(c, {"chain": "c12"}))
        self.assertIsNone(sg.config_for({"structure_gate": {**c["structure_gate"], "enabled": False}}, {"chain": "c9"}))
        self.assertIsNone(sg.config_for({}, {"chain": "c9"}))


class CampaignGateTest(unittest.TestCase):
    """接进战役命令后的行为。夹具借用 PrepareTest 的方法（不继承，免得它的用例在这里重跑一遍）。"""
    setUp, tearDown = base.PrepareTest.setUp, base.PrepareTest.tearDown
    write_results, gate, post = base.PrepareTest.write_results, base.PrepareTest.gate, base.PrepareTest.post
    prepare, sheet, review_sheet = base.PrepareTest.prepare, base.PrepareTest.sheet, base.PrepareTest.review_sheet

    def thin(self):
        """四条里三条非空、每条只有 2 个不同端点对（基础夹具），再把它们改成两节点模板。"""
        items = []
        for n, i in enumerate(self.ids):
            it = base.result_item(i, empty=(n == len(self.ids) - 1))
            if it["entities"]:
                it["entities"] = [e for e in it["entities"] if e["name"] != "kg-hub"]
                it["edges"] = [g for g in it["edges"] if g["target"] != "kg-hub"]
            items.append(it)
        self.write_results(items)

    def rich(self):
        items = []
        for n, i in enumerate(self.ids):
            it = base.result_item(i, empty=(n == len(self.ids) - 1))
            if it["entities"]:
                it["edges"].append({"source": f"reconcile_queue_{i}", "name": "belongs_to", "target": "kg-hub",
                                    "fact": f"reconcile_queue_{i} lives in the kg-hub repository"})
            items.append(it)
        self.write_results(items)

    def enable(self, **kw):
        return ec.cmd_structure_gate("p1", enable=True, by="test-coordinator", reason="c13 回潮", **kw)

    def submit(self):
        return ec.cmd_submit("p1", self.bid, dry_run=False, url="http://x", token="t", post=self.post,
                             gate=self.gate, layout=self.layout)

    def lint_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = ec.main(["lint", "--id", "p1", "--batch", self.bid])
        return code, buf.getvalue()

    def ready(self):
        self.prepare()
        self.review_sheet()
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"]), (cp.EXIT_READY, "ready"), rep.get("next"))
        return rep

    def test_campaign_without_gate_behaves_as_before(self):
        self.thin()
        self.assertIsNone(ec.load(self.d).get("structure_gate"))
        self.assertEqual(self.lint_cli()[0], 0)
        rep = self.ready()
        self.assertNotIn("structure_gate", rep)
        out = self.submit()
        self.assertEqual(out["by_status"], {"ok": 3, "empty": 1})
        self.assertNotIn("structure_gate", ec.find_batch(ec.load(self.d), self.bid))
        self.assertIsNone(ec.cmd_status("p1")["structure_gate"])

    def test_template_batch_refused_in_lint_prepare_and_submit(self):
        self.thin()
        self.prepare()
        self.review_sheet()
        self.enable()
        code, text = self.lint_cli()
        self.assertEqual(code, 1)
        self.assertIn("结构门槛没过", text)
        self.assertIn(f"{self.bid} {self.ids[0]}: 2 个实体、1 个不同端点对", text)
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"]), (cp.EXIT_ACTION, "structure_gate"))
        self.assertTrue(rep["quality_check"]["ok"])                 # 原有判定照过，门槛只多一条拒绝
        self.assertEqual([t["id"] for t in rep["structure_gate"]["thin"]], self.ids[:3])
        self.assertEqual((self.gated, self.posts), ([], []))        # 没排闸门、没 dry-run

    def test_gate_enabled_after_preview_still_blocks_formal_submit(self):
        self.thin()
        self.ready()
        self.enable()
        with self.assertRaises(SystemExit) as cm:
            self.submit()
        self.assertIn("结构门槛没过", str(cm.exception))
        self.assertEqual(self.gated, ["dryrun"])                    # 正式提交没进闸门
        self.assertEqual(ec.find_batch(ec.load(self.d), self.bid)["status"], "dry_run")
        ec.cmd_structure_gate("p1", enable=False, by="test-coordinator", reason="用户决定先放行本批")
        self.assertEqual(self.submit()["by_status"], {"ok": 3, "empty": 1})

    def test_rich_batch_passes_and_submission_records_gate(self):
        self.rich()
        self.enable()
        self.assertEqual(self.lint_cli()[0], 0)
        rep = self.ready()
        self.assertTrue(rep["structure_gate"]["ok"])
        self.submit()
        rec = ec.find_batch(ec.load(self.d), self.bid)["structure_gate"]
        self.assertEqual((rec["version"], rec["metrics"]["pairs_per_obs"], rec["counted"]), (sg.GATE_VERSION, 3.0, 3))
        self.assertEqual(rec["config"]["max_two_entity_pct"], 30.0)

    def test_note_exemption_and_template_note_through_prepare(self):
        self.thin()
        self.enable(thresholds={"max_two_entity_pct": 100.0, "min_pairs_per_obs": 1.0, "max_exempt_pct": 100.0})
        self.ready()   # 宽阈值下能到 ready，质量表已通过
        self.enable(thresholds={"max_two_entity_pct": 0.0, "min_pairs_per_obs": 2.5})   # 收紧：只改给出的项
        g = ec.load(self.d)["structure_gate"]
        self.assertEqual((g["max_two_entity_pct"], g["max_exempt_pct"]), (0.0, 100.0))
        sheet = self.sheet()
        for row in sheet["items"]:
            i = row["id"]
            row[sg.NOTE_FIELD] = f"worker_{i} 与 reconcile_queue_{i} 之外正文没有支持第三个实体的证据"
        cp.sheet_path(self.d, self.bid).write_text(json.dumps(sheet, ensure_ascii=False))
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"]), (cp.EXIT_ACTION, "structure_gate"))
        self.assertEqual(len(rep["structure_gate"]["invalid_exempt"]), 3)   # 模板句全部不算
        notes = ["worker_{i} 只在调用供应商前重试三次，正文没有点名文件或配置",
                 "reconcile_queue_{i} 是唯一下游，原文没有命令、路由或第三个组件",
                 "原文只把 worker_{i} 交给对账，归属仓库只在录制元数据里，不能当证据"]
        for row, text in zip(sheet["items"], notes):
            row[sg.NOTE_FIELD] = text.format(i=row["id"])
        cp.sheet_path(self.d, self.bid).write_text(json.dumps(sheet, ensure_ascii=False))
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"]), (cp.EXIT_READY, "ready"), rep.get("next"))
        self.assertEqual(rep["structure_gate"]["exempt"], self.ids[:3])
        self.assertEqual(self.submit()["by_status"], {"ok": 3, "empty": 1})

    def test_from_chain_spares_batches_claimed_before_it(self):
        self.thin()
        out = self.enable(from_chain="c1")
        self.assertEqual(out["open_batches_gated"], [])
        self.assertEqual(self.lint_cli()[0], 0)
        self.ready()
        self.submit()                                              # c0 批次不受影响
        with self.assertRaises(SystemExit):
            self.enable(from_chain="c3")                           # 最多写下一版
        chain = os.path.join(self.tmp.name, "c1.md")
        _Path(chain).write_text("# c1\n结构约定")
        ec.cmd_chain("p1", chain)
        o = ec.cmd_claim("p1", "M", url="http://x", token="t", fetch=base.fake_fetch, check=lambda idents: {})
        self.assertEqual(ec.find_batch(ec.load(self.d), o["batch"])["chain"], "c1")
        c = ec.load(self.d)
        self.assertIsNotNone(sg.config_for(c, ec.find_batch(c, o["batch"])))   # c1 起领的批次受门槛约束
        self.assertIsNone(sg.config_for(c, ec.find_batch(c, self.bid)))

    def test_audit_log_and_worker_refusal(self):
        self.enable()
        self.enable(thresholds={"max_relates_to_pct": 95.0})
        with self.assertRaises(SystemExit):
            ec.cmd_structure_gate("p1", enable=True, by=" ", reason="x")
        with mock.patch.dict(os.environ, {"KG_HUB_CAMPAIGN_SLOT": "1"}), self.assertRaises(SystemExit):
            ec.cmd_structure_gate("p1", enable=False, by="worker", reason="想关掉")
        with mock.patch.dict(os.environ, {"KG_HUB_REWORK_BATCHES": "x"}), self.assertRaises(SystemExit):
            ec.main(["structure-gate", "--id", "p1", "--disable", "--by", "w", "--reason", "r"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ec.main(["structure-gate", "--id", "p1", "--enable", "--by", "coordinator",
                                      "--reason", "relates_to 只提醒", "--max-relates-to-pct", "off"]), 0)
        ec.cmd_structure_gate("p1", enable=False, by="coordinator", reason="收尾")
        with self.assertRaises(SystemExit):
            ec.cmd_structure_gate("p1", enable=False, by="coordinator", reason="重复关闭")
        log = ec.load(self.d)["structure_gate_log"]
        self.assertEqual([x["action"] for x in log], ["enable", "update", "update", "disable"])
        self.assertEqual([x["by"] for x in log], ["test-coordinator", "test-coordinator", "coordinator", "coordinator"])
        self.assertEqual((log[1]["gate"]["max_relates_to_pct"], log[2]["gate"]["max_relates_to_pct"]), (95.0, None))
        self.assertEqual(log[0]["gate"]["max_two_entity_pct"], 30.0)
        self.assertTrue(all(x["at"] and x["reason"] and x["via"] == "structure-gate" for x in log))
        self.assertFalse(ec.load(self.d)["structure_gate"]["enabled"])
        self.assertEqual(len(ec.cmd_status("p1")["structure_gate_log"]), 4)

    def test_start_defaults_on_via_cli_and_records_decline(self):
        c = ec.cmd_start("p2", ["M"], executor="claude-code", quota=4, batch=4, after_id=0, lookup=base.PASS,
                         structure_gate=dict(sg.DEFAULTS))
        self.assertEqual((c["structure_gate"]["enabled"], c["structure_gate"]["from_chain"]), (True, "c0"))
        self.assertEqual((c["structure_gate_log"][0]["action"], c["structure_gate_log"][0]["via"]), ("enable", "start"))
        c = ec.cmd_start("p3", ["M"], executor="claude-code", quota=4, batch=4, after_id=0, lookup=base.PASS,
                         no_structure_gate="对照组不加门槛")
        self.assertNotIn("structure_gate", c)
        self.assertEqual(c["structure_gate_log"][0]["action"], "declined")
        self.assertNotIn("structure_gate_log", ec.load(self.d))    # 函数缺省：与门槛出现前相同
        self.assertIn("结构门槛", ec.brief(ec.load(ec.campaign_dir("p2")), "M"))
        self.assertNotIn("结构门槛", ec.brief(ec.load(self.d), "M"))
        calls = []
        with mock.patch.object(ec.ax, "runtime_preflight", lambda *a, **k: {"ok": True}), \
                mock.patch.object(ec.ax, "api_token", lambda *a, **k: "t"), \
                mock.patch.object(ec, "cmd_start", lambda *a, **k: calls.append(k) or {}), \
                contextlib.redirect_stdout(io.StringIO()):
            ec.main(["start", "--id", "p4", "--models", "M", "--executor", "claude-code"])
            ec.main(["start", "--id", "p5", "--models", "M", "--executor", "claude-code", "--no-structure-gate", "对照"])
        self.assertEqual((calls[0]["structure_gate"], calls[0]["no_structure_gate"]), (sg.DEFAULTS, None))
        self.assertEqual((calls[1]["structure_gate"], calls[1]["no_structure_gate"]), (None, "对照"))


if __name__ == "__main__":
    unittest.main()
