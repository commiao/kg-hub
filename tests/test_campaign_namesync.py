"""战役命名同步（tools/campaign_namesync.py）：锚点键规则、规范名选择、dry-run 分裂判据、改写结果、回放。

夹具是 2026-10-24 opus2000 战役里真实发生过的 4 类命名分裂的缩小版（名字照抄当时的写法）：
  1. 标识符对标识符（带说明）：reconcile_due ↔ reconcile_due（QueueDispatcher）
  2. 工作树三种写法：log-redundancy-verification 工作树（fleet-ops）/ fleet-ops 日志冗余核查工作树（…-0b8ec2）/
     日志冗余核查工作树（fleet-ops，0b8ec2）
  3. 同一仓库两个前缀：recruitment-intel-service:… ↔ zhaopin:…
  4. 服务端本来就会合并的全路径与短名：deploy/dashboard_publication_jobs.py ↔ dashboard_publication_jobs.py（不能误改）
路径布局用假的家目录，不读本机 ~/work-ai。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools import campaign_namesync as ns

HOME = "/Users/tester"
WA = HOME + "/work-ai"
WT_DIR = f"{WA}/fleet-ops/.claude/worktrees/log-redundancy-verification-0b8ec2"
OLD, CUR = "20261024-old", "20261025-cur"


def layout():
    return ns.Layout(workai=WA, home=HOME, repos={"kg-hub", "fleet-ops", "zhaopin", "credvault"})


def ent(name, typ="Concept"):
    return {"name": name, "type": typ, "summary": "夹具实体，摘要写够三十个字以免被别的自检拦下来。"}


def write_campaign(root: Path, cid: str, batches: list[dict], *, log: list[dict] = ()) -> Path:
    """batches: [{"id", "status", "submitted_at"?, "items": [{"id", "body", "entities", "edges"?}]}]"""
    d = root / cid
    (d / "batches").mkdir(parents=True)
    (d / "results").mkdir()
    camp = {"tool": "extract_campaign", "id": cid, "batches": []}
    for b in batches:
        camp["batches"].append({k: b[k] for k in ("id", "status", "submitted_at") if k in b})
        (d / "batches" / f"{b['id']}.json").write_text(json.dumps(
            {"batch_id": b["id"], "items": [{"id": it["id"], "episode_body": it.get("body", "")} for it in b["items"]]},
            ensure_ascii=False))
        (d / "results" / f"{b['id']}.json").write_text(json.dumps(
            {"items": [{"id": it["id"], "entities": it["entities"], "edges": it.get("edges", [])} for it in b["items"]]},
            ensure_ascii=False))
    (d / "campaign.json").write_text(json.dumps(camp, ensure_ascii=False))
    (d / "log.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in log))
    return d


CUR_ITEMS = [
    {"id": 11, "body": f"改了 {WA}/kg-hub/utils/queue_dispatcher.py",
     "entities": [ent("reconcile_due"), ent("QueueDispatcher")],
     "edges": [{"source": "reconcile_due", "name": "relates_to", "target": "QueueDispatcher", "fact": "标题 reconcile_due"},
               {"source": "reconcile_due", "name": "relates_to", "target": "QueueDispatcher", "fact": "title reconcile_due"},
               {"source": "reconcile_due", "name": "part_of", "target": "QueueDispatcher",
                "fact": "reconcile_due 是 QueueDispatcher 的对账入口"}]},
    {"id": 12, "body": "在日志冗余核查工作树里跑了对账（正文没给路径）",
     "entities": [ent("日志冗余核查工作树（fleet-ops，0b8ec2）", "Tool")]},
    {"id": 13, "body": "",
     "entities": [ent("recruitment-intel-service:deploy/check_source_drift.py", "File")]},
    {"id": 14, "body": f"读了 {WA}/credvault/deploy/dashboard_publication_jobs.py",
     "entities": [ent("dashboard_publication_jobs.py", "File")]},
    {"id": 15, "body": "", "entities": [ent("fleet-ops 日志冗余核查工作树（log-redundancy-verification-0b8ec2）", "Tool")]},
]


def build(root: Path) -> None:
    """旧战役已提交（就是「图里已有」）；本战役 c-001 已 dry-run，c-002 / c-003 并行待提交。"""
    write_campaign(root, OLD, [{
        "id": "m-001", "status": "submitted", "submitted_at": "2026-10-24T01:00:00+00:00", "items": [
            {"id": 1, "entities": [ent("reconcile_due（QueueDispatcher）")]},
            # 服务端把这种写法并进了上面那个节点：log.jsonl 的 reuse_mismatch 记着，挂在图里那个名字上
            {"id": 2, "entities": [ent("reconcile_due()（QueueDispatcher）")]},
            {"id": 3, "body": f"cd {WT_DIR} && ls", "entities": [ent("log-redundancy-verification 工作树（fleet-ops）", "Tool")]},
            {"id": 4, "body": f"cd {WT_DIR}", "entities": [ent("fleet-ops 日志冗余核查工作树（log-redundancy-verification-0b8ec2）", "Tool")]},
            {"id": 5, "body": f"cd {WT_DIR}", "entities": [ent("fleet-ops 日志冗余核查工作树（log-redundancy-verification-0b8ec2）", "Tool")]},
            {"id": 6, "entities": [ent("zhaopin:deploy/check_source_drift.py", "File")]},
            {"id": 7, "body": f"{WA}/credvault/deploy/dashboard_publication_jobs.py",
             "entities": [ent("deploy/dashboard_publication_jobs.py", "File")]},
        ]}], log=[{"batch": "m-001", "id": 2, "status": "ok",
                   "reuse_mismatch": ["reconcile_due()（QueueDispatcher） → reconcile_due（QueueDispatcher）"]}])
    d = write_campaign(root, CUR, [
        {"id": "c-001", "status": "dry_run", "items": copy.deepcopy(CUR_ITEMS)},
        {"id": "c-002", "status": "claimed", "items": [{"id": 21, "entities": [ent("held_by_others（ClaimRegistry）")]}]},
        {"id": "c-003", "status": "claimed", "items": [
            {"id": 31, "entities": [ent("held_by_others")]},
            {"id": 32, "entities": [ent("foo_bar")]},
            {"id": 33, "entities": [ent("foo_bar（另一处说明）")]},
            {"id": 34, "entities": [ent("reconcile_due", "Tool")]},   # 同类别不同类型：只报告
            {"id": 35, "entities": [ent("reconcile_due", "Issue")]},  # 类型类别不同：不算同一个
        ]},
    ])
    (d / "dryrun-c-001.json").write_text(json.dumps({"batch_id": "c-001", "ids": [11, 12, 13, 14, 15], "new_entities": {
        "11": ["reconcile_due", "QueueDispatcher"], "12": ["日志冗余核查工作树（fleet-ops，0b8ec2）"],
        "13": ["recruitment-intel-service:deploy/check_source_drift.py"], "14": [], "15": []}}, ensure_ascii=False))


def new_entities(root: Path, bid: str):
    p = root / CUR / f"dryrun-{bid}.json"
    if not p.exists():
        return None
    return {k: set(v) for k, v in json.loads(p.read_text())["new_entities"].items()}


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.lay = layout()
        self.lay.known_wts.add(("fleet-ops", "log-redundancy-verification-0b8ec2"))

    def keys(self, name, typ="Concept", body=""):
        return [(k, key, auto) for k, key, auto in ns.keys_for(name, typ, ns.Body(body, self.lay), self.lay)]

    def test_file_key_is_repo_plus_relative_path_with_aliases(self):
        want = [("file", ("zhaopin", "deploy/check_source_drift.py"), True)]
        self.assertEqual(self.keys("zhaopin:deploy/check_source_drift.py", "File"), want)
        self.assertEqual(self.keys("recruitment-intel-service:deploy/check_source_drift.py", "File"), want)
        self.assertEqual(self.keys("deploy/check_source_drift.py（zhaopin）", "File"), want)
        self.assertEqual(self.keys("check_source_drift.py", "File", f"见 {WA}/recruitment-intel-service/deploy/check_source_drift.py"),
                         want)                                   # 正文绝对路径补全仓库与完整相对路径
        self.assertEqual(self.keys("check_source_drift.py", "File"),
                         [("file", ("?", "check_source_drift.py"), False)])   # 推不出仓库：不自动改

    def test_identifier_key_ignores_explanation_but_respects_other_repo(self):
        for name in ("reconcile_due", "reconcile_due()", "reconcile_due（QueueDispatcher）", "reconcile_due()（QueueDispatcher）"):
            self.assertEqual(self.keys(name), [("id", ("reconcile_due", ""), True)], name)
        self.assertEqual(self.keys("reconcile_due（credvault）"), [("id", ("reconcile_due", "credvault"), True)])
        self.assertNotIn("id", [k for k, *_ in self.keys("reconcile_due（credvault，kg-hub）")])
        self.assertEqual(self.keys("QueueDispatcher"), [("id", ("QueueDispatcher", ""), True)])   # 驼峰也算

    def test_three_worktree_spellings_share_one_key(self):
        key = [("wt", ("fleet-ops", "log-redundancy-verification-0b8ec2"), True)]
        self.assertEqual(self.keys("log-redundancy-verification 工作树（fleet-ops）", "Tool", f"cd {WT_DIR}"), key)
        self.assertEqual(self.keys("fleet-ops 日志冗余核查工作树（log-redundancy-verification-0b8ec2）", "Tool"), key)
        self.assertEqual(self.keys("日志冗余核查工作树（fleet-ops，0b8ec2）", "Tool"), key)   # 只有 hash 后缀，靠已知工作树

    def test_report_only_kinds(self):
        self.assertIn(("head", ("gateway_step_mappings", ""), False), self.keys("gateway_step_mappings 表（ModelAttemptJournal）"))
        self.assertIn(("pr", ("kg-hub", "114"), False), self.keys("对账状态未变不写账本（kg-hub#114）", "Fix"))
        self.assertIn(("core", ("thing", "sshwrap"), False), self.keys("sshwrap（SSH 主连接复用包装脚本）"))

    def test_server_merges_only_bare_path_suffix_or_parens(self):
        self.assertTrue(ns.server_merges("dashboard_publication_jobs.py", "deploy/dashboard_publication_jobs.py"))
        self.assertTrue(ns.server_merges("check_refinery_halt()", "check_refinery_halt"))
        self.assertFalse(ns.server_merges("tests/test_x.py", "kg-hub:tests/test_x.py"))   # 带仓库前缀的服务端不并
        self.assertFalse(ns.server_merges("reconcile_due", "reconcile_due（QueueDispatcher）"))

    def test_layout_from_disk_reads_repos_and_worktrees(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "work-ai" / "recruitment-intel-service" / ".claude" / "worktrees" / "fix-abc123").mkdir(parents=True)
            (Path(t) / "work-ai" / "kg-hub").mkdir()
            lay = ns.Layout.from_disk(home=t)
            self.assertEqual(lay.repos, {"recruitment-intel-service", "kg-hub"})
            self.assertEqual(lay.known_wts, {("zhaopin", "fix-abc123")})
            self.assertFalse(lay.clean(f"{t}/work-ai/kg-hub/x.py".lstrip("/")))


class ReplayFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, bid, ne=None):
        return ns.batch_report(self.root, CUR, bid, layout(), ne)[0]

    def test_four_split_kinds_canonical_names_and_dryrun_criterion(self):
        rep = self.report("c-001", new_entities(self.root, "c-001"))
        moves = {a["item"]: (a["from"], a["to"], a["kind"]) for a in rep["auto"]}
        self.assertEqual(moves, {
            11: ("reconcile_due", "reconcile_due（QueueDispatcher）", "id"),     # 1 条直接写 + 1 条服务端并进来 = 挂得最多
            12: ("日志冗余核查工作树（fleet-ops，0b8ec2）", "fleet-ops 日志冗余核查工作树（log-redundancy-verification-0b8ec2）", "wt"),
            13: ("recruitment-intel-service:deploy/check_source_drift.py", "zhaopin:deploy/check_source_drift.py", "file"),
        })                       # 14 服务端会并（不能误改）、15 已是图中名、QueueDispatcher 没有先例：都不动
        self.assertEqual(sorted((s["item"], s["kind"]) for s in rep["splits"]), [(11, "id"), (12, "wt"), (13, "file")])
        self.assertEqual(self.report("c-001")["splits"], [])               # 没给 dry-run 就不判分裂

    def test_pending_batches_first_come_and_in_batch_unification(self):
        rep = self.report("c-003")
        moves = {(a["item"], a["from"]): (a["to"], a["source"]) for a in rep["auto"]}
        self.assertEqual(moves[(31, "held_by_others")][0], "held_by_others（ClaimRegistry）")
        self.assertIn("待提交批次", moves[(31, "held_by_others")][1])            # 更早的待提交批次先到先得
        self.assertEqual(moves[(33, "foo_bar（另一处说明）")][0], "foo_bar")      # 本批首次出现的写法
        self.assertNotIn(34, [a["item"] for a in rep["auto"]])
        self.assertNotIn(35, [a["item"] for a in rep["auto"] + rep["review"]])
        self.assertIn((34, "id-type"), [(r["item"], r["kind"]) for r in rep["review"]])   # 同名不同类型只报告
        self.assertEqual(self.report("c-002")["auto"], [])                     # 先到的那批不改

    def test_apply_rewrites_entities_edges_and_later_facts_only(self):
        rep = self.report("c-001")
        data = json.loads((self.root / CUR / "results" / "c-001.json").read_text())
        self.assertEqual(ns.apply_renames(data, rep["auto"]), [])
        by = {it["id"]: it for it in data["items"]}
        self.assertEqual([e["name"] for e in by[11]["entities"]], ["reconcile_due（QueueDispatcher）", "QueueDispatcher"])
        self.assertEqual({e["source"] for e in by[11]["edges"]}, {"reconcile_due（QueueDispatcher）"})
        self.assertEqual([e["fact"] for e in by[11]["edges"][:2]], ["标题 reconcile_due", "title reconcile_due"])  # 照抄的不动
        self.assertEqual(by[11]["edges"][2]["fact"], "reconcile_due（QueueDispatcher） 是 QueueDispatcher 的对账入口")
        self.assertEqual(by[14]["entities"][0]["name"], "dashboard_publication_jobs.py")
        (self.root / CUR / "results" / "c-001.json").write_text(json.dumps(data, ensure_ascii=False))
        self.assertEqual(self.report("c-001")["auto"], [])                     # 改完再查是干净的

    def test_apply_skips_rename_that_would_make_a_self_loop(self):
        data = {"items": [{"id": 1, "entities": [ent("a_b"), ent("a_b（说明）")],
                           "edges": [{"source": "a_b", "name": "relates_to", "target": "a_b（说明）", "fact": "x"}]}]}
        auto = [{"item": 1, "from": "a_b（说明）", "to": "a_b"}]
        self.assertEqual(ns.apply_renames(data, auto)[0]["why"], "改名后成自环")
        self.assertEqual([e["name"] for e in data["items"][0]["entities"]], ["a_b", "a_b（说明）"])

    def test_replay_counts_splits_per_batch(self):
        rows = {r["batch"]: r for r in ns.replay(self.root, CUR, layout(), lambda bid: new_entities(self.root, bid))}
        self.assertEqual([r["batch"] for r in ns.replay(self.root, CUR, layout(), lambda bid: None)],
                         ["c-001", "c-002", "c-003"])
        self.assertEqual((rows["c-001"]["splits"], rows["c-001"]["auto"]), (3, 3))
        self.assertFalse(rows["c-003"]["dryrun"])

    def test_graph_hints_ask_only_for_identifier_heads(self):
        mine = ns.records_for(CUR, "x", [{"id": 1, "entities": [ent("falkordb_slow 告警"), ent("plain")]}], {}, (1,), layout())
        asked = []

        def lookup(ident):
            asked.append(ident)
            return {"matched_node": ident, "labels": ["Issue"]}
        hints = ns.graph_hints(mine, lookup)
        self.assertEqual(asked, ["falkordb_slow"])
        self.assertEqual(hints[0]["names_in_batch"], ["falkordb_slow 告警"])


if __name__ == "__main__":
    unittest.main()
