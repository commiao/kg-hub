"""战役命名同步（tools/campaign_namesync.py）：锚点键规则、规范名选择、dry-run 分裂判据、改写结果、回放。

夹具是 2026-10-24 opus2000 战役里真实发生过的 4 类命名分裂的缩小版（名字照抄当时的写法）：
  1. 标识符对标识符（带说明）：reconcile_due ↔ reconcile_due（QueueDispatcher）
  2. 工作树三种写法：log-redundancy-verification 工作树（fleet-ops）/ fleet-ops 日志冗余核查工作树（…-0b8ec2）/
     日志冗余核查工作树（fleet-ops，0b8ec2）
  3. 同一仓库两个前缀：recruitment-intel-service:… ↔ zhaopin:…
  4. 服务端本来就会合并的全路径与短名：deploy/dashboard_publication_jobs.py ↔ dashboard_publication_jobs.py（不能误改）
2026-10-25 战役 021、022 批反馈的三处不足各有一组用例（ApplyDetailTest / ServerMergeTest / CrossRepoTest）：
  1. --apply 后输出要保留实际写回的每一条改名（022 批 graphiti_core 改了名却只剩计数）
  2. 服务端按包含占比会合并的写法（feature/course-benefit-freetimes（libtv））要标成「服务端会合并到 X」，
     而服务端不会合并的写法不能被标、也不能被误改
  3. sd-server 的 RedisCacheKeyConstants.java 与图里属 throttle 的同名裸节点：同名不同仓库要报出来
路径布局用假的家目录，不读本机 ~/work-ai。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import copy
import json
import os
import tempfile
import unittest
import unittest.mock
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

    def test_server_merges_follows_server_lexical_rule(self):
        self.assertTrue(ns.server_merges("dashboard_publication_jobs.py", "deploy/dashboard_publication_jobs.py"))
        self.assertTrue(ns.server_merges("check_refinery_halt()", "check_refinery_halt"))
        self.assertFalse(ns.server_merges("tests/test_x.py", "kg-hub:tests/test_x.py"))   # 带仓库前缀的服务端不并
        self.assertFalse(ns.server_merges("reconcile_due", "reconcile_due（QueueDispatcher）"))
        # 以前抄的两条规则之外：包含占比 ≥ CONTAIN_MIN_SHARE 的短括号也会被服务端并掉
        self.assertTrue(ns.server_merges("feature/course-benefit-freetimes（libtv）", "feature/course-benefit-freetimes", "Concept"))
        self.assertFalse(ns.server_merges("graphiti_core（Python 依赖包）", "graphiti_core", "Concept"))   # 占比不够
        self.assertFalse(ns.server_merges("config.py", "deploy/config.py", "File"))           # 泛名文件服务端不并

    def test_layout_from_disk_reads_repos_and_worktrees(self):
        with tempfile.TemporaryDirectory() as t, unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KG_HUB_NAMESYNC_ROOTS", None)
            (Path(t) / "work-ai" / "recruitment-intel-service" / ".claude" / "worktrees" / "fix-abc123").mkdir(parents=True)
            (Path(t) / "work-ai" / "kg-hub").mkdir()
            (Path(t) / "workspace" / "sd-server" / ".git").mkdir(parents=True)   # 公司仓库目录：带 .git 的才算
            (Path(t) / "workspace" / "tmp").mkdir()
            lay = ns.Layout.from_disk(home=t)
            self.assertEqual(lay.repos, {"recruitment-intel-service", "kg-hub", "sd-server"})
            self.assertEqual(lay.known_wts, {("zhaopin", "fix-abc123")})
            self.assertFalse(lay.clean(f"{t}/work-ai/kg-hub/x.py".lstrip("/")))
            self.assertEqual(lay.split_abs(f"{t}/workspace/sd-server/pom.xml"), ("sd-server", "pom.xml", None))
            os.environ["KG_HUB_NAMESYNC_ROOTS"] = ""
            self.assertNotIn("sd-server", ns.Layout.from_disk(home=t).repos)   # 给空串就不看 ~/workspace


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

        def lookup(ident, limit=1):
            asked.append(ident)
            return {"matched_node": ident, "labels": ["Issue"]}
        hints = ns.graph_hints(mine, lookup)
        self.assertEqual(asked, ["falkordb_slow"])
        self.assertEqual(hints[0]["names_in_batch"], ["falkordb_slow 告警"])


class ApplyDetailTest(unittest.TestCase):
    """缺陷 1：--apply 后输出要留下实际写回的每一条（item / from / to / kind / source），而不只是计数。"""

    def test_applied_rows_keep_each_written_rename(self):
        auto = [{"item": 7, "from": "graphiti_core", "to": "graphiti_core（Python 依赖包）", "kind": "id", "key": ["graphiti_core", ""],
                 "from_type": "Tool", "to_type": "Tool", "source": "图中节点（3 条观测挂在它上面，例 c/b#1）"},
                {"item": 7, "from": "graphiti_core", "to": "别的写法", "kind": "id", "source": "后到的同名计划"},   # 同条同旧名：先到的生效
                {"item": 8, "from": "a_b（说明）", "to": "a_b", "kind": "id", "source": "x"}]
        data = {"items": [{"id": 7, "entities": [ent("graphiti_core", "Tool")], "edges": []},
                          {"id": 8, "entities": [ent("a_b"), ent("a_b（说明）")],
                           "edges": [{"source": "a_b", "name": "relates_to", "target": "a_b（说明）", "fact": "x"}]}]}
        skipped = ns.apply_renames(data, auto)
        self.assertEqual(ns.applied_rows(auto, skipped), [
            {"item": 7, "from": "graphiti_core", "to": "graphiti_core（Python 依赖包）", "kind": "id",
             "source": "图中节点（3 条观测挂在它上面，例 c/b#1）"}])               # 8 因自环没改，不算写回
        self.assertEqual(data["items"][0]["entities"][0]["name"], "graphiti_core（Python 依赖包）")

    def test_single_model_apply_prints_applied_rows(self):
        from tools import agent_extract as ax
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            build(root / "campaigns")
            b, r = root / "b.json", root / "r.json"
            b.write_text(json.dumps({"batch_id": "b1", "items": [{"id": 11, "episode_body": ""}]}))
            r.write_text(json.dumps({"items": [{"id": 11, "entities": [ent("reconcile_due")], "edges": []}]}))
            out = []
            with unittest.mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(root)}), \
                    unittest.mock.patch.object(ns.Layout, "from_disk", classmethod(lambda cls, *a, **k: layout())), \
                    unittest.mock.patch("builtins.print", lambda *a, **k: out.append(a[0] if a else "")):
                code = ax.main(["namesync", "--batch", str(b), "--file", str(r), "--apply"])
            rep = json.loads(out[-1])
            self.assertEqual(code, 0)
            self.assertEqual([(a["item"], a["from"], a["to"], a["kind"]) for a in rep["applied"]],
                             [(11, "reconcile_due", "reconcile_due（QueueDispatcher）", "id")])
            self.assertEqual(rep["residual"], [])
            self.assertEqual(len(rep["auto"]), 1)                                # 计划本身也留着


def graph_campaign(root: Path, entities_by_item: dict, *, bodies: dict | None = None) -> None:
    """一个已提交的旧战役（就是「图里已有」）：{观测 id: [实体]}。"""
    write_campaign(root, OLD, [{"id": "m-001", "status": "submitted", "submitted_at": "2026-10-24T01:00:00+00:00",
                                "items": [{"id": i, "entities": es, "body": (bodies or {}).get(i, "")}
                                          for i, es in entities_by_item.items()]}])


def pending(root: Path, entities_by_item: dict, *, bodies: dict | None = None) -> None:
    write_campaign(root, CUR, [{"id": "c-001", "status": "claimed",
                                "items": [{"id": i, "entities": es, "body": (bodies or {}).get(i, "")}
                                          for i, es in entities_by_item.items()]}])


class ServerMergeTest(unittest.TestCase):
    """缺陷 2：服务端按 _lexically_same（含包含占比）会合并的写法，标成「服务端会合并到 X」，不当 review、不改名；
    服务端不会合并的写法不能被这样标，原有的 auto 也不能因此丢。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_case(self, graph: dict, mine: dict, lay=None):
        graph_campaign(self.root, graph)
        pending(self.root, mine)
        return ns.batch_report(self.root, CUR, "c-001", lay or layout())[0]

    def test_short_bracket_on_bare_name_is_reported_as_server_merge(self):
        bare = "feature/course-benefit-freetimes"
        for lay in (layout(), ns.Layout(workai=WA, home=HOME, repos={"kg-hub", "libtv"})):   # libtv 认不认得都一样
            with self.subTest(libtv_known="libtv" in lay.repos):
                self.root = Path(self.tmp.name) / str(len(lay.repos))
                rep = self.run_case({1: [ent(bare)], 2: [ent(bare)]}, {11: [ent(bare + "（libtv）")]}, lay)
                self.assertEqual([(s["item"], s["from"], s["to"], s["kind"]) for s in rep["server"]],
                                 [(11, bare + "（libtv）", bare, "server")])
                self.assertIn("服务端会合并到「feature/course-benefit-freetimes」", rep["server"][0]["source"])
                self.assertEqual((rep["auto"], rep["review"]), ([], []))     # 以前这里是一条 review

    def test_names_the_server_will_not_merge_are_not_marked(self):
        rep = self.run_case(
            {1: [ent("graphiti_core（Python 依赖包）", "Tool")], 2: [ent("graphiti_core（Python 依赖包）", "Tool")],
             3: [ent("kg-hub:tests/test_x.py", "File")],
             4: [ent("deploy/config.py", "File")],
             5: [ent("feature/other-branch")],
             6: [ent("validate 阶段相似度计数")]},
            {11: [ent("graphiti_core", "Tool")],                            # 包含占比不够：服务端不并 → 照旧 auto
             12: [ent("tests/test_x.py", "File")],                          # 仓库前缀不同：服务端不并
             13: [ent("config.py", "File")],                                # 泛名文件：服务端不并
             14: [ent("feature/other-branch（libtv）", "Issue")],           # 类型不同：服务端的类型守卫不让并
             15: [ent("prevalidate 阶段相似度计数")]})                       # 只差词头前缀：服务端不并
        self.assertEqual(rep["server"], [])
        self.assertEqual([(a["item"], a["from"], a["to"]) for a in rep["auto"]],
                         [(11, "graphiti_core", "graphiti_core（Python 依赖包）")])

    def test_same_key_server_merge_is_reported_not_renamed(self):
        rep = self.run_case({1: [ent("deploy/dashboard_publication_jobs.py", "File")]},
                            {11: [ent("dashboard_publication_jobs.py", "File")]})
        self.assertEqual(rep["auto"], [])
        self.assertEqual([(s["from"], s["to"]) for s in rep["server"]],
                         [("dashboard_publication_jobs.py", "deploy/dashboard_publication_jobs.py")])

    def test_prefixed_name_is_not_renamed_to_bare_graph_name(self):
        lay = ns.Layout(workai=WA, home=HOME, repos={"kg-hub", "sd-server"})
        rep = self.run_case({1: [ent("sd-server:pom.xml", "File")]}, {11: [ent("sd-server:pom.xml", "File")]}, lay)
        self.assertEqual(rep["auto"], [])
        graph_campaign(self.root / "b", {1: [ent("pom.xml（sd-server）", "File")]})
        pending(self.root / "b", {11: [ent("sd-server:pom.xml", "File")]})
        recs, meta = ns.campaign_records(self.root / "b", CUR, lay)
        mine = [r for r in recs if r.batch == "c-001"]
        earlier = ns.earlier_index([r for r in recs if r.batch != "c-001"], meta[(CUR, "c-001")]["rank"])
        self.assertEqual(ns.analyse(mine, earlier, lay)["auto"], [])        # 不往没前缀的写法上改
        self.assertEqual(ns.dryrun_splits(mine, earlier, {"11": {"sd-server:pom.xml"}}), [])   # 也不算分裂


SD_FILE = "src/main/java/com/liblibai/sd/common/constant/RedisCacheKeyConstants.java"
TH_FILE = "throttle-common/src/main/java/com/liblibai/throttle/common/constant/RedisCacheKeyConstants.java"


class CrossRepoTest(unittest.TestCase):
    """缺陷 3：本批文件按正文解析到仓库 A，图里同名裸节点在已提交记录里只解析到仓库 B → 报「同名不同仓库」。
    公司仓库在 ~/workspace，观测正文只有「Project:」与「Files read:」两行；相对路径要在本机该仓库里真有才算数。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.home = t / "home"
        for repo, rel in (("sd-server", SD_FILE), ("throttle", TH_FILE)):
            f = self.home / "workspace" / repo / rel
            f.parent.mkdir(parents=True)
            f.write_text("class X {}")
            (self.home / "workspace" / repo / ".git").mkdir()
        (self.home / "work-ai" / "fleet-ops").mkdir(parents=True)
        self.root = t / "campaigns"
        self.lay = ns.Layout.from_disk(home=str(self.home), workai=str(self.home / "work-ai"))

    def report(self, mine_body: str, *, name="RedisCacheKeyConstants.java", graph_body=None):
        graph_body = graph_body if graph_body is not None else f"Files read: {TH_FILE}\nProject: throttle"
        graph_campaign(self.root, {1: [ent("RedisCacheKeyConstants.java", "File")]}, bodies={1: graph_body})
        pending(self.root, {11: [ent(name, "File")]}, bodies={11: mine_body})
        return ns.batch_report(self.root, CUR, "c-001", self.lay)[0]

    def test_same_bare_name_in_another_repo_is_reported_with_prefix_suggestion(self):
        rep = self.report(f"读了 RedisCacheKeyConstants.java\nFiles read: {SD_FILE}\nProject: sd-server")
        self.assertEqual([(r["item"], r["from"], r["to"], r["kind"]) for r in rep["review"]],
                         [(11, "RedisCacheKeyConstants.java", f"sd-server:{SD_FILE}", "xrepo")])
        self.assertIn("同名不同仓库", rep["review"][0]["source"])
        self.assertIn("throttle", rep["review"][0]["source"])
        self.assertEqual(rep["auto"], [])                                    # 只报告，不自动改

    def test_absolute_path_under_workspace_counts_too(self):
        rep = self.report(f"cd {self.home}/workspace/sd-server && vim {self.home}/workspace/sd-server/{SD_FILE}")
        self.assertEqual([r["kind"] for r in rep["review"]], ["xrepo"])

    def test_same_repo_or_unresolved_repo_is_not_reported(self):
        same = self.report(f"Files read: {TH_FILE}\nProject: throttle")
        self.assertEqual(same["review"], [])
        self.tmp.cleanup()
        self.setUp()
        # Project 是会话标签：fleet-ops 会话读的相对路径在 fleet-ops 里并不存在 → 不算 fleet-ops 的文件
        wrong = self.report(f"Files read: {SD_FILE}\nProject: fleet-ops")
        self.assertEqual(wrong["review"], [])

    def test_location_pseudo_repos_are_not_reported(self):
        rep = self.report("", name=f"~/.config/RedisCacheKeyConstants.java",
                          graph_body=f"Files read: {TH_FILE}\nProject: throttle")
        self.assertNotIn("xrepo", [r["kind"] for r in rep["review"]])

    def test_graph_hint_when_the_bare_node_only_lives_in_the_old_graph(self):
        """不在任何战役里的旧图节点：--graph 问一次图，邻居里提到别的仓库就提示。"""
        pending(self.root, {11: [ent("RedisCacheKeyConstants.java", "File")]},
                bodies={11: f"Files read: {SD_FILE}\nProject: sd-server"})
        recs, _ = ns.campaign_records(self.root, CUR, self.lay)
        asked = []

        def lookup(name, limit=1):
            asked.append((name, limit))
            return {"matched_node": name, "labels": ["File", "Entity"], "neighbors": [
                {"name": "throttle 限流键常量", "fact": "throttle-common 的 RedisCacheKeyConstants.java 定义限流键", "direction": "in"}]}
        hints = ns.graph_hints(recs, lookup, self.lay)
        self.assertEqual([(h["name"], h["repo"], h["neighbors_mention"], h["suggest"]) for h in hints],
                         [("RedisCacheKeyConstants.java", "sd-server", ["throttle"], f"sd-server:{SD_FILE}")])
        self.assertEqual(asked, [("RedisCacheKeyConstants.java", ns.GRAPH_HINT_LIMIT)])
        self.assertEqual(ns.graph_hints(recs, lambda n, limit=1: {"matched_node": n, "neighbors": [
            {"name": "sd-server 常量", "fact": "sd-server 的键"}]}, self.lay), [])   # 只提到本仓库：不提示


if __name__ == "__main__":
    unittest.main()
