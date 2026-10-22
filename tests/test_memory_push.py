"""memory_push：记忆文件 → 确定性抽取 → /api/ingest_extracted 载荷。纯函数全钉住；
网络用注入的 post 替身。方案见 docs/MEMORY-INGEST-PLAN.md（T-0332）。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools import memory_push as mp
from utils.extracted_ingest import ExtractedEdge, ExtractedEntity, normalize_extraction

try:
    from schema import EDGE_TYPES, ENTITY_TYPES
except Exception:  # noqa: BLE001 — 本机缺 graphiti 时退回字面词表
    ENTITY_TYPES = {k: None for k in ("Person", "Project", "File", "Tool", "Concept", "Issue", "Fix",
                                      "Config", "Session", "Observation", "Capsule", "KnowledgeDoc", "Lesson")}
    EDGE_TYPES = {k: None for k in ("caused_by", "fixed_by", "verified_by", "references", "modified",
                                    "depends_on", "supersedes", "derived_from", "occurred_in", "belongs_to",
                                    "extracted_from", "relates_to", "documented_in", "diagnosed_by",
                                    "implemented_as")}

SAMPLE = """---
name: launchctl-bootout-is-async
description: bootout 返回 ≠ 作业已退干净；紧跟 bootstrap 报 error 5 且不重试
metadata:
  type: feedback
  modified: 2026-09-22T07:09:08.628Z
---

正文第一段。

**Why：** 单个切时看不见，批量切就现形。

**How to apply:** 任何 bootout/bootstrap 成对操作都要等到 `launchctl print` 查不到再装回去；批量执行前先想一想。
同形的另一例见 [[shell-cjk-expansion-utf8-not-c]]，以及 [[not-in-index]]。
"""


def _quiet(fn):
    def run(*a, **k):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **k)
    return run


def mem(text=SAMPLE, mtime="2026-10-01T00:00:00+00:00"):
    return mp.parse_memory(text, mtime)


class ParseTest(unittest.TestCase):
    def test_frontmatter_and_body(self):
        m = mem()
        self.assertEqual(m["name"], "launchctl-bootout-is-async")
        self.assertTrue(m["description"].startswith("bootout 返回"))
        self.assertEqual(m["type"], "feedback")
        self.assertEqual(m["reference_time"], "2026-09-22T07:09:08.628000+00:00")
        self.assertTrue(m["body"].startswith("正文第一段"))
        self.assertEqual(m["links"], ["shell-cjk-expansion-utf8-not-c", "not-in-index"])

    def test_how_to_first_sentence_only(self):
        m = mem()
        self.assertEqual(m["how_to"], "任何 bootout/bootstrap 成对操作都要等到 `launchctl print` 查不到再装回去；")

    def test_how_to_variants_and_next_line(self):
        body = "**How to apply**：\n- 先看分布。再定阈值。\n"
        self.assertEqual(mp.how_to_first_sentence(body), "先看分布。")
        self.assertEqual(mp.how_to_first_sentence("## How to apply\n\n定阈值前跑历史数据；问自己。"), "定阈值前跑历史数据；")
        self.assertEqual(mp.how_to_first_sentence("没有这一段"), "")

    def test_reference_time_fallbacks(self):
        self.assertEqual(mp._iso("2026-10-18"), "2026-10-18T00:00:00+00:00")
        self.assertIsNone(mp._iso("not a date"))
        m = mem(SAMPLE.replace("  modified: 2026-09-22T07:09:08.628Z\n", ""), "2026-10-01T00:00:00+00:00")
        self.assertEqual(m["reference_time"], "2026-10-01T00:00:00+00:00")

    def test_paragraph_leads(self):
        body = (
            "第一段**加粗**和 `code`，见 [[other-memory]] 与 [文档](docs/x.md)。第二句不要。\n续行\n\n"
            "```sh\n代码块不取。\n```\n"
            "| 表格 | 不取 |\n"
            "## 标题不取\n\n---\n\n"
            "- 列表一；后半句不要\n"
            "* 列表二\n"
            "1. 编号项。\n\n"
            "**Why：** 理由段不取。\n\n"
            "**How to apply:** 做法段不取。\n\n"
            "最后一段。\n"
        )
        self.assertEqual(mp.paragraph_leads(body), [
            "第一段加粗和 code，见 other-memory 与 文档。", "列表一；", "列表二", "编号项。", "最后一段。"])
        self.assertEqual(mp.paragraph_leads(body, limit=2), ["第一段加粗和 code，见 other-memory 与 文档。", "列表一；"])
        # 取的是「前 limit 段」，Why/How 段不占名额
        many = "\n\n".join(f"第{i}段。" for i in range(10))
        self.assertEqual(mp.paragraph_leads("**Why:** 理由。\n\n" + many), [f"第{i}段。" for i in range(mp.MAX_LEADS)])
        self.assertEqual(mem()["leads"], ["正文第一段。"])

    def test_no_frontmatter(self):
        m = mem("just text")
        self.assertEqual(m["name"], "")
        self.assertEqual(mp.classify_tier(m), ("C", "no-frontmatter"))


class TierTest(unittest.TestCase):
    def test_types_map_to_tiers_and_kinds(self):
        for typ, tier, kind in (("feedback", "A", "方法论"), ("reference", "A", "手册"), ("project", "B", "项目事实")):
            m = mem(SAMPLE.replace("type: feedback", f"type: {typ}"))
            self.assertEqual(mp.classify_tier(m), (tier, f"type={typ}"))
            self.assertEqual(mp.KIND_BY_TYPE[typ], kind)
        m = mem(SAMPLE.replace("type: feedback", "type: user"))
        self.assertEqual(mp.classify_tier(m)[0], "C")

    def test_skip_list_and_secret_scan_win(self):
        m = mem()
        self.assertEqual(mp.classify_tier(m, {"launchctl-bootout-is-async"}), ("C", "skip-list"))
        leaked = mem(SAMPLE + "\nexport KG_HUB_API_TOKEN=abcdefghijklmnop\n")
        self.assertEqual(mp.classify_tier(leaked), ("C", "secret-scan"))
        self.assertTrue(all(h.endswith("…") and len(h) <= 25 for h in leaked["secret_hits"]))

    def test_secret_scan_ignores_concept_mentions(self):
        text = SAMPLE + "\n记忆里 20 个文件提到 password / token / webhook，未逐个核过。token 过期要重新登录。\n"
        self.assertEqual(mp.scan_secrets(text), [])
        self.assertTrue(mp.scan_secrets("webhook: https://open.feishu.cn/open-apis/bot/v2/hook/abcdefghij1234567890xyz"))
        self.assertTrue(mp.scan_secrets("key sk-abcdefghijklmnop"))

    def test_project_from_bucket(self):
        self.assertEqual(mp.project_from_bucket("-Users-mac-work-ai-kg-hub"), "kg-hub")
        self.assertEqual(mp.project_from_bucket("-Users-mac-workspace-sd-server"), "sd-server")
        self.assertIsNone(mp.project_from_bucket("-Users-mac-work-ai"))
        self.assertIsNone(mp.project_from_bucket("something-else"))


class IdentityAndExtractionTest(unittest.TestCase):
    SHA = "0123456789abcdef" * 4

    def test_identity_format(self):
        name, sd, sid = mp.build_identity(mem(), "-Users-mac-work-ai-kg-hub", self.SHA)
        self.assertEqual(sid, f"{self.SHA}:{mp.PROMPT_VERSION}")
        import hashlib
        key8 = hashlib.sha256(sid.encode()).hexdigest()[:8]
        self.assertEqual(name, f"agent-memory-claude-code-launchctl-bootout-is-async@{key8}")
        self.assertEqual(sd, f"agent-memory/claude-code project=kg-hub type=feedback sha=01234567 v={mp.PROMPT_VERSION}")
        # 规则版本变 → 键与 name 都变（同一文件要重推并归档旧版）
        with unittest.mock.patch.object(mp, "PROMPT_VERSION", "memory-v99"):
            name2, _, sid2 = mp.build_identity(mem(), "-Users-mac-work-ai-kg-hub", self.SHA)
        self.assertNotEqual((name2, sid2), (name, sid))
        _, sd2, _ = mp.build_identity(mem(), "-Users-mac-work-ai", self.SHA)
        self.assertIn("project=general", sd2)

    def test_identity_is_derivable_by_origin(self):
        try:
            from utils.origin import derive_origin
        except Exception:  # noqa: BLE001
            self.skipTest("utils.origin 不可导入")
        name, sd, _ = mp.build_identity(mem(), "-Users-mac-work-ai-kg-hub", self.SHA)
        o = derive_origin(name, sd)
        self.assertEqual(o["origin_tool"], "claude-code")
        self.assertEqual(o["origin_project"], "kg-hub")

    def test_extraction_project_bucket(self):
        index = {"launchctl-bootout-is-async": "x", "shell-cjk-expansion-utf8-not-c": "准则 31 的 locale 条件曾被写反"}
        ents, edges = mp.build_extraction(mem(), "kg-hub", index)
        self.assertEqual([(e["name"], e["type"]) for e in ents],
                         [("launchctl-bootout-is-async", "Lesson"), ("kg-hub", "Project"),
                          ("shell-cjk-expansion-utf8-not-c", "Lesson")])
        self.assertEqual([e["name"] for e in edges], ["belongs_to", "relates_to", "relates_to", "relates_to"])
        # 主 fact = description / How-to 原文，不带 "slug：" 前缀（中文向量对不上英文前缀）
        self.assertEqual(edges[0]["fact"], mem()["description"])
        self.assertEqual(edges[1]["fact"], mem()["how_to"])
        self.assertFalse(edges[0]["fact"].startswith("launchctl-bootout-is-async"))
        self.assertEqual(edges[2]["target"], "shell-cjk-expansion-utf8-not-c")
        self.assertIn("准则 31", edges[2]["fact"])
        # 不在索引里的链接不建实体、不建边
        self.assertNotIn("not-in-index", [e["name"] for e in ents])
        # memory-v3：段首句挂在主目标上
        self.assertEqual((edges[3]["source"], edges[3]["target"], edges[3]["fact"]),
                         ("launchctl-bootout-is-async", "kg-hub", "正文第一段。"))

    def test_lead_edges_dedupe_and_cap(self):
        m = mem()
        m["leads"] = [m["description"], "甲。", m["how_to"], "甲。", "", "乙。"]
        _, edges = mp.build_extraction(m, "kg-hub", {})
        facts = [e["fact"] for e in edges]
        self.assertEqual(facts, [m["description"], m["how_to"], "甲。", "乙。"])
        long_body = "\n\n".join(f"第{i}段。" for i in range(20))
        _, edges = mp.build_extraction(mem(SAMPLE.replace("正文第一段。", long_body)), None, {})
        self.assertEqual(len(edges), 2 + mp.MAX_LEADS)

    def test_extraction_general_bucket_hangs_on_tool(self):
        ents, edges = mp.build_extraction(mem(), None, {})
        self.assertEqual(ents[1], {"name": "Claude Code", "type": "Tool", "summary": ""})
        self.assertEqual(edges[0]["name"], "derived_from")

    def test_payload_passes_server_normalization(self):
        index = {"shell-cjk-expansion-utf8-not-c": "d"}
        p = mp.build_payload(mem(), "-Users-mac-work-ai-kg-hub", self.SHA, index)
        for k in ("name", "episode_body", "source_description", "reference_time", "source_obs_id",
                  "extractor_model", "entities", "edges"):
            self.assertIn(k, p)
        self.assertEqual(p["kind"], "方法论")
        self.assertEqual(p["durability"], "evergreen")
        self.assertEqual(p["origin_tool"], "claude-code")
        self.assertFalse(p["dry_run"])
        import re
        self.assertRegex(p["extractor_model"], r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}\Z")
        ents = [ExtractedEntity(**e) for e in p["entities"]]
        edges = [ExtractedEdge(**e) for e in p["edges"]]
        n = normalize_extraction(ents, edges, set(ENTITY_TYPES), set(EDGE_TYPES))
        self.assertEqual(n.stats["entities_out"], 3)
        self.assertEqual(n.stats["edges_out"], 4)
        for k in ("unknown_type", "offschema_edge", "dangling_edge", "dup_entity", "dup_edge", "self_edge"):
            self.assertEqual(n.stats[k], 0, k)
        self.assertTrue(all(len(e["fact"]) <= mp.MAX_FACT for e in p["edges"]))

    def test_no_kind_when_type_unmapped(self):
        p = mp.build_payload(mem(SAMPLE.replace("type: feedback", "type: user")), "-Users-mac-work-ai", self.SHA, {})
        self.assertNotIn("kind", p)


class PlanTest(unittest.TestCase):
    def test_plan_changes(self):
        wm = {"a": {"key": "1"}, "b": {"key": "2"}, "gone": {"key": "9"}}
        plan = mp.plan_changes(wm, {"a": "1", "b": "3", "c": "4"})
        self.assertEqual(plan, {"new": ["c"], "changed": ["b"], "unchanged": ["a"], "deleted": ["gone"]})

    def test_legacy_v1_watermark_counts_as_changed(self):
        # v1 水位线只有 sha；v2 的 content_key 带版本 → 必须判 changed（重推 + 归档旧版）
        wm = {"a": {"sha": "abc", "episode_name": "agent-memory-claude-code-a@old"}}
        self.assertEqual(mp.plan_changes(wm, {"a": mp.content_key("abc")})["changed"], ["a"])
        self.assertEqual(mp.plan_changes(wm, {"a": "abc:memory-v1"})["unchanged"], ["a"])

    def test_v2_watermark_counts_as_changed(self):
        # v3 加了段首句边：v2 推过的同一文件也要重推并归档 v2 旧版
        self.assertEqual(mp.PROMPT_VERSION, "memory-v3")
        wm = {"a": {"key": "abc:memory-v2", "episode_name": "agent-memory-claude-code-a@v2"}}
        self.assertEqual(mp.plan_changes(wm, {"a": mp.content_key("abc")})["changed"], ["a"])

    def test_post_with_retry_waits_on_draining_only(self):
        calls, sleeps = [], []
        answers = iter([(503, {"code": "draining", "retry_after_seconds": 7}), (200, {"status": "ok"})])

        def post(m, u, b, t):
            calls.append(u)
            return next(answers)
        code, d = mp.post_with_retry("http://x/api/ingest_extracted", {}, "t", post=post, sleep=sleeps.append)
        self.assertEqual((code, d["status"], sleeps, len(calls)), (200, "ok", [7], 2))
        code, d = mp.post_with_retry("u", {}, "t", post=lambda *a: (409, {"code": "x"}), sleep=sleeps.append)
        self.assertEqual(code, 409)
        self.assertEqual(sleeps, [7])


class PushFlowTest(unittest.TestCase):
    def _items(self, root: Path, texts: dict[str, str]):
        for bucket, slug, text in [(b, s, t) for (b, s), t in texts.items()]:
            d = root / bucket / "memory"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{slug}.md").write_text(text)
        return mp.load_all(root, None)

    def test_push_then_change_archives_old_and_delete_archives(self):
        posted, archived = [], []

        def post(method, url, body, token):
            if url.endswith("/api/ingest_extracted/check"):
                return 200, {"items": [{"source_obs_id": i["source_obs_id"], "status": None} for i in body["items"]]}
            if url.endswith("/api/ingest_extracted"):
                posted.append(body)
                return 200, {"status": "ok", "episode_uuid": f"u{len(posted)}", "stats": {}}
            if url.endswith("/dashboard/archive_episode"):
                archived.append(body["name"])
                return 200, {"ok": True}
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as td:
            root, state = Path(td) / "projects", Path(td) / "state"
            _os.environ["KG_HUB_MEMORY_PUSH_DIR"] = str(state)
            try:
                items = self._items(root, {("-Users-mac-work-ai-kg-hub", "launchctl-bootout-is-async"): SAMPLE})
                sel = mp._select(items, mp.DEFAULT_SKIP, None, None)
                self.assertEqual(_quiet(mp.cmd_push)(items, sel, "http://x", "t", sleep=lambda s: None, post=post), 0)
                wm = json.loads((state / "watermark.json").read_text())
                first = wm["launchctl-bootout-is-async"]["episode_name"]
                self.assertTrue(first.startswith("agent-memory-claude-code-launchctl-bootout-is-async@"))
                self.assertEqual(len(posted), 1)
                self.assertEqual(posted[0]["source_obs_id"], mp.content_key(items[0]["sha"]))
                # 同内容再推：不 POST
                _quiet(mp.cmd_push)(items, sel, "http://x", "t", sleep=lambda s: None, post=post)
                self.assertEqual(len(posted), 1)
                # 改内容：推新 + 归档旧
                items = self._items(root, {("-Users-mac-work-ai-kg-hub", "launchctl-bootout-is-async"): SAMPLE + "\n补一行。\n"})
                sel = mp._select(items, mp.DEFAULT_SKIP, None, None)
                _quiet(mp.cmd_push)(items, sel, "http://x", "t", sleep=lambda s: None, post=post)
                self.assertEqual(len(posted), 2)
                self.assertEqual(archived, [first])
                second = json.loads((state / "watermark.json").read_text())["launchctl-bootout-is-async"]["episode_name"]
                self.assertNotEqual(first, second)
                # 删文件：归档新版、水位线移除
                (root / "-Users-mac-work-ai-kg-hub" / "memory" / "launchctl-bootout-is-async.md").unlink()
                items = mp.load_all(root, None)
                _quiet(mp.cmd_push)(items, mp._select(items, mp.DEFAULT_SKIP, None, None), "http://x", "t",
                            sleep=lambda s: None, post=post)
                self.assertEqual(archived, [first, second])
                self.assertEqual(json.loads((state / "watermark.json").read_text()), {})
                recs = [json.loads(l) for l in (state / "log.jsonl").read_text().splitlines()]
                self.assertEqual(sum(1 for r in recs if r.get("status") == "ok"), 2)
                self.assertEqual(sum(1 for r in recs if r.get("archived")), 2)
            finally:
                _os.environ.pop("KG_HUB_MEMORY_PUSH_DIR", None)

    def test_only_tier_a_is_selected_and_already_ok_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root, state = Path(td) / "projects", Path(td) / "state"
            _os.environ["KG_HUB_MEMORY_PUSH_DIR"] = str(state)
            try:
                items = self._items(root, {
                    ("-Users-mac-work-ai-kg-hub", "launchctl-bootout-is-async"): SAMPLE,
                    ("-Users-mac-work-ai-kg-hub", "proj-fact"): SAMPLE.replace("name: launchctl-bootout-is-async", "name: proj-fact").replace("type: feedback", "type: project"),
                    ("-Users-mac-work-ai-kg-hub", "MEMORY"): "# index",
                })
                self.assertEqual([it["mem"]["name"] for it in items], ["launchctl-bootout-is-async", "proj-fact"])
                sel = mp._select(items, mp.DEFAULT_SKIP, None, None)
                self.assertEqual([it["mem"]["name"] for it in sel], ["launchctl-bootout-is-async"])
                posted = []

                def post(method, url, body, token):
                    if url.endswith("/check"):
                        return 200, {"items": [{"source_obs_id": i["source_obs_id"], "status": "ok"} for i in body["items"]]}
                    posted.append(url)
                    return 200, {"status": "ok"}
                _quiet(mp.cmd_push)(items, sel, "http://x", "t", sleep=lambda s: None, post=post)
                self.assertEqual(posted, [])   # 服务端已有同键 → 不再 POST
                wm = json.loads((state / "watermark.json").read_text())
                self.assertIn("launchctl-bootout-is-async", wm)
            finally:
                _os.environ.pop("KG_HUB_MEMORY_PUSH_DIR", None)


if __name__ == "__main__":
    unittest.main()
