"""预抽取入库的纯逻辑与写图语句：归一纪律指标、消解复用、边复用、来源标记。

这条路不调模型，所以测试不需要 graphiti_core：driver 只要 execute_query，embedder 只要
create_batch。写图语句必须与 graphiti 0.29.0 的 FalkorDB save 一致（vecf32、SET n = $entity_data、
MENTIONS/RELATES_TO），否则检索和画像看不到这些数据。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import unittest
from datetime import datetime, timezone

from utils import extracted_ingest as xi

ENTITY_TYPES = {"Person", "Project", "File", "Tool", "Concept", "Issue", "Fix", "Config",
                "Session", "Observation", "Capsule", "KnowledgeDoc", "Lesson"}
EDGE_TYPES = {"caused_by", "fixed_by", "verified_by", "references", "modified", "depends_on",
              "supersedes", "derived_from", "occurred_in", "belongs_to", "extracted_from",
              "relates_to", "documented_in", "diagnosed_by", "implemented_as"}


def ents(*items):
    return [xi.ExtractedEntity(name=n, type=t, summary=s) for n, t, s in items]


def edges(*items):
    return [xi.ExtractedEdge(source=a, name=r, target=b, fact=f) for a, r, b, f in items]


class NormalizeTest(unittest.TestCase):
    def test_schema_mapping_and_discipline_stats(self):
        n = xi.normalize_extraction(
            ents(("kg-hub", "Project", "repo"), ("KG-HUB", "project", "dup by case"),
                 ("commit 94a9779", "tool", "a commit"), ("/Users/mac/x", "Widget", "path")),
            edges(("kg-hub", "BELONGS_TO", "commit 94a9779", "f1"),
                  ("kg-hub", "IS_ANCESTOR_OF", "/Users/mac/x", "f2"),
                  ("kg-hub", "is_ancestor_of", "/Users/mac/x", " f2 "),
                  ("kg-hub", "references", "ghost", "dangling"),
                  ("kg-hub", "relates_to", "kg-hub", "self")),
            ENTITY_TYPES, EDGE_TYPES)
        self.assertEqual([e.name for e in n.entities], ["kg-hub", "commit 94a9779", "/Users/mac/x"])
        self.assertEqual([e.type for e in n.entities], ["Project", "Tool", "Concept"])
        self.assertEqual([e.name for e in n.edges], ["belongs_to", "relates_to"])
        self.assertEqual(n.edges[1].raw_name, "IS_ANCESTOR_OF")
        s = n.stats
        self.assertEqual((s["dup_entity"], s["unknown_type"], s["dirty_name"]), (1, 1, 2))
        self.assertEqual((s["offschema_edge"], s["dup_edge"], s["dangling_edge"], s["self_edge"]), (2, 1, 1, 1))
        self.assertEqual((s["entities_out"], s["edges_out"]), (3, 2))

    def test_same_triple_different_facts_are_kept(self):
        # 同一对实体同一边名、不同 fact 是两条事实（memory-v3 段首句边）；逐字重复才去重
        n = xi.normalize_extraction(
            ents(("lesson-a", "Lesson", ""), ("kg-hub", "Project", "")),
            edges(("lesson-a", "relates_to", "kg-hub", "做法首句。"),
                  ("lesson-a", "relates_to", "kg-hub", "段首句一。"),
                  ("lesson-a", "RELATES_TO", "kg-hub", "段首句一。"),
                  ("lesson-a", "relates_to", "kg-hub", "段首句二。")),
            ENTITY_TYPES, EDGE_TYPES)
        self.assertEqual([e.fact for e in n.edges], ["做法首句。", "段首句一。", "段首句二。"])
        self.assertEqual(n.stats["dup_edge"], 1)

    def test_limits(self):
        with self.assertRaises(ValueError):
            xi.normalize_extraction(ents(*[(f"e{i}", "Tool", "") for i in range(61)]), [],
                                    ENTITY_TYPES, EDGE_TYPES)


class LexicalTest(unittest.TestCase):
    def test_vector_candidates_need_lexical_support(self):
        same = xi._lexically_same
        # 第 2 轮后的取舍：代码类标识符只认完全相等（分隔符有意义），宁可多一个节点也不并错近名
        self.assertFalse(same("qwen.token_plan", "qwen-token-plan"))
        self.assertTrue(same("qwen.token_plan", "QWEN.TOKEN_PLAN"))
        self.assertTrue(same("tests/test_effective_quota_deploy.py", "test_effective_quota_deploy.py"))
        self.assertTrue(same("model-gateway-private 网络", "model-gateway-private"))
        self.assertTrue(same("Writer Lock", "writer_lock"))
        # 2026-10-17 冒烟里的两次误并
        self.assertFalse(same("实际额度来源修正发布（T-0046）", "反思改进项落实专项 (REF-001~005)"))
        self.assertFalse(same("effective-quota 发布脚本不入仓导致本机测试 FileNotFoundError", "docs/effective-quota"))
        self.assertFalse(same("kg-hub", "kg-hub-server 镜像"))  # 短名不能靠包含吃掉长名的一半以下
        # 第 2 轮（opus）复盘：同类型近名误并
        self.assertFalse(same("tests/test_phase1m.py", "tests/test_phase1p.py", "File"))
        self.assertFalse(same("deploy-standard 准则 19–22", "deploy-standard 准则 26–28"))
        self.assertFalse(same("2026-09-19 网关付费请求全 503", "2026-09-18 早间网关全量 503"))
        self.assertFalse(same("kg-hub:deploy/nas/release.sh", "deploy/nas-release.sh", "File"))
        self.assertFalse(same("credvault:deploy/nas-release.sh", "deploy/nas-release.sh", "File"))  # 仓库:路径 是另一仓库的文件
        self.assertTrue(same("credvault/deploy/nas-release.sh", "deploy/nas-release.sh", "File"))   # 全路径 vs 子路径
        self.assertFalse(same("deploy/nas/release.sh", "deploy/nas-release.sh", "File"))
        self.assertFalse(same("max_tokens 路由上限裁剪", "max_tokens"))
        self.assertFalse(same("candidate_source_boundary", "same_candidate_source_boundary"))
        self.assertFalse(same("credvault dashboard 发布作业恢复", "credvault dashboard 安全删除凭证"))
        self.assertTrue(same("claude-mem 502", "claude-mem 502 错误"))   # 占比 ≥0.8 的包含仍算同物
        self.assertTrue(same("Writer Lock", "writer_lock"))

    def test_names_differing_by_one_contrast_word_are_different(self):
        """2026-10-22 三模型对比第 6 轮：边组 ↔ 节点组只差一组对立词，difflib 0.9 被放行。"""
        same = xi._lexically_same
        self.assertFalse(same("prepare 阶段边组相似度", "prepare 阶段节点组相似度", "Concept"))
        self.assertFalse(same("向量索引启用开关", "向量索引禁用开关", "Concept"))
        self.assertFalse(same("Edge group latency", "Node group latency", "Concept"))
        self.assertFalse(same("读路径超时", "写路径超时", "Issue"))
        # 不是对立词替换的近名照旧：只多修饰语、同义写法仍可复用
        self.assertTrue(same("claude-mem 502", "claude-mem 502 错误"))
        self.assertTrue(same("Writer Lock", "writer_lock"))

    def test_names_differing_only_in_number_expressions_are_different(self):
        """2026-10-23 战役第 1 轮 dry-run：「准则 8 → 准则 26–28」一个单数、一个范围，按段替换数字的守卫漏过。"""
        same = xi._lexically_same
        self.assertFalse(same("deploy-standard 准则 8", "deploy-standard 准则 26–28", "Concept"))
        self.assertFalse(same("credvault 准则 8 至 10", "credvault 准则 8", "Concept"))
        self.assertFalse(same("Python 3.9 运行时", "Python 3.13 运行时", "Tool"))
        self.assertFalse(same("灰度 5% 阶段", "灰度 50% 阶段", "Concept"))
        # 数字一样、只多修饰语或写法不同的照旧复用
        self.assertTrue(same("claude-mem 502", "claude-mem 502 错误", "Issue"))
        self.assertTrue(same("PR #49 修复", "PR #49 清理修复", "Fix"))
        self.assertTrue(same("deploy-standard 准则 26–28", "deploy-standard 准则 26-28", "Concept"))

    def test_names_differing_by_word_prefix_are_different(self):
        """2026-10-22 补交 b0024：`prevalidate 阶段相似度计数` 被并进同批先建的 `validate 阶段相似度计数`（包含占比 0.81）。"""
        same = xi._lexically_same
        self.assertFalse(same("prevalidate 阶段相似度计数", "validate 阶段相似度计数", "Concept"))
        self.assertFalse(same("pre-validate 阶段相似度计数", "validate 阶段相似度计数", "Concept"))
        self.assertFalse(same("reindex 耗时统计", "index 耗时统计", "Concept"))
        self.assertFalse(same("预校验阶段计数", "校验阶段计数", "Concept"))
        self.assertFalse(same("未提交的工作树改动", "提交的工作树改动", "Concept"))
        # 同次补交里判为正确的复用照旧：多修饰词、同一 PR 的两种叫法、单复数
        for a, b in (("model_inflight_avg 时间积分", "model_inflight_avg"),
                     ("flow-metrics-release 隔离工作树", "flow-metrics-release 工作树"),
                     ("reconciliation-projection 修复（pr #49）", "reconciliation projection 清理（PR #49）"),
                     ("http_request_started_at 见证字段", "http_request_started_at"),
                     ("workers 队列", "worker 队列")):
            with self.subTest(a=a):
                self.assertTrue(same(a, b, "Concept"))

    def test_campaign_1024_near_names_are_different(self):
        """2026-10-24 opus/fable 战役 dry-run：子代理逐批改名才没让这些近名并进图（另有一对发布物 hash 已并进）。"""
        same = xi._lexically_same
        cases = {
            # ① 只换动词词头的命令/方法名
            "verb": [("adopt-reviewed-routes 命令（nas-compose-control.sh）",
                      "prepare-reviewed-routes 命令（nas-compose-control.sh）"),
                     ("arm_with_outcome_guard（ProviderStateStore）", "arm_outcome_write_guard（ProviderStateStore）")],
            # ② 分支名只差末尾单词
            "branch": [("claude/t-0219-reconcile-readonly 分支（credvault）",
                        "claude/t-0219-reconcile-backoff 分支（credvault）")],
            # ③ 含字母的短 hash / 版本号
            "hash": [("a1b2c3d 发布工作树（credvault t-0219-release6，2026-10-09）",
                      "9f8e7d6 发布工作树（credvault t-0219-release8，2026-10-09）"),
                     ("fleet-ops Mac 发布物 b28fd06（mac-release.py，2026-10-10）",
                      "fleet-ops Mac 发布物 7a9b6c1（mac-release.py，2026-10-10）"),
                     ("rollback-witness-v5 schema", "v4 rollback-witness schema")],
            # ④ 只差数字（另一边多一个带数字的词）
            "number": [("md2 阵列（NAS RAID）", "md3 阵列（NAS RAID1）"),
                       ("credvault release4 工作树（2026-10-09）", "credvault release5 工作树（2026-10-09）")],
            # ⑤ 工作树名只差中间用途词
            "purpose": [("credvault t-0219-wal 工作树（2026-10-09）", "credvault t-0219-scenario 工作树（2026-10-09）"),
                        ("t-0219-plan2 工作树（credvault）", "t-0219-release2 工作树（credvault）")],
            # ⑥ 中文对立词（时间也不同、时间相同两种）
            "contrast": [("人工闭合 kg_hub.entity_extract 断路器（T-0195，21:02）",
                          "人工断开 kg_hub.entity_extract 断路器（T-0195，14:07）"),
                         ("人工闭合 kg_hub.entity_extract 断路器（T-0195）",
                          "人工断开 kg_hub.entity_extract 断路器（T-0195）")],
            # ⑦ 方法/常量被所属类或相邻标识符吸走
            "member": [("RequestQueue.cancel（llm_request_queue.py）", "RequestQueue（llm_request_queue.py）"),
                       ("claim()（RequestQueue，llm_request_queue.py）", "RequestQueue（llm_request_queue.py）"),
                       ("_cancel()（utils/gateway_queue.py）", "QueueCancelled（utils/gateway_queue.py）"),
                       ("_submit（utils/gateway_queue.py）", "SUBMIT_ATTEMPTS（utils/gateway_queue.py）"),
                       ("ReceiptBackoff（utils/gateway_queue.py）", "receipt_loop（utils/gateway_queue.py）"),
                       ("can_submit()（kg_refinery.py）", "pool_can_submit()（kg_refinery.py）"),
                       ("_cancel()", "QueueCancelled"), ("_submit", "SUBMIT_ATTEMPTS"),
                       ("can_submit()", "pool_can_submit()")],
        }
        for kind, pairs in cases.items():
            for a, b in pairs:
                with self.subTest(kind=kind, a=a):
                    self.assertFalse(same(a, b, "Concept"))
                    self.assertFalse(same(b, a, "Concept"))

    def test_campaign_1024_correct_reuses_still_match(self):
        """同一战役里正式提交、核对为正确的复用（每类各取一两条），守卫补强后不能回归。"""
        same = xi._lexically_same
        for a, b, t in (
                ("tests/test_gateway_queue.py", "test_gateway_queue.py", "File"),       # 同仓库全路径 → 短名
                ("ReadDependencies（graphiti_parallel）", "ReadDependencies (utils/graphiti_parallel.py)", "Concept"),
                ("summarize_attempts()", "summarize_attempts", "Concept"),            # 带不带 ()
                ("assert_container_binding()（nas-compose-control.sh）",
                 "assert_container_binding（nas-compose-control.sh）", "Concept"),
                ("reconcile_due（queuedispatcher）", "reconcile_due()（QueueDispatcher）", "Concept"),
                ("execute_queued_request（model-gateway）", "execute_queued_request（ModelGateway）", "Concept"),
                ("排队作业取消 cancel()（RequestQueue，llm_request_queue.py）",
                 "作业取消方法 cancel（RequestQueue，llm_request_queue.py）", "Concept"),
                ("_max_task_http_calls", "_MAX_TASK_HTTP_CALLS = 3", "Concept"),
                ("model_stage contextvar（kg-hub）", "_model_stage contextvar", "Concept"),
                ("performgracefulshutdown", "performGracefulShutdown function", "Concept"),
                ("avgsnapshotbytes 统计（createx 进度日志）", "avgSnapshotBytes（CreateX 进度统计）", "Concept"),
                ("witness-preflight-cancel 运维出口（model-gateway）",
                 "witness-preflight-cancel 命令（model_gateway.py CLI）", "Concept"),
                ("NAS 发布 a1b2c3d（credvault 网关 PR #58，2026-10-09 03:03）",
                 "NAS 发布 a1b2c3d（credvault PR #58，2026-10-09）", "Concept"),
                ("跨天重试复用幂等键致永久 425（2026-09-05）", "跨日复用幂等键导致永久425（2026-09-05）", "Issue"),
                ("--refinery-window-22-08（release.sh 选项）", "--refinery-window-22-08 标志（kg-hub release.sh）", "Concept"),
                ("对账任务列表端点（ingest_reconciliation）", "对账列表端点（GET /api/ingest/reconciliation）", "Concept"),
                # 补强守卫时差点误伤的：空格隔开的英文换说法、连字符写法、版本号带点
                ("gateway reconciliation endpoints", "gateway reconciliation API", "Concept"),
                ("model gateway reconciliation endpoints", "gateway reconciliation REST endpoints", "Concept"),
                ("kg_hub_default 网络", "kg-hub_default", "Concept"),
                ("上游 graphiticore v0.29.0 库", "graphiti-core 0.29.0", "Tool"),
                # 三条新守卫各自的放行豁免：单复数、数字相同只是贴的字母不同、版本号里的点不是成员访问
                ("claude-mem-workers 队列积压", "claude-mem-worker 队列积压", "Issue"),
                ("T-0195 断路器人工断开复盘记录", "T0195 断路器人工断开复盘", "Fix"),
                ("Python v3.13 运行时升级记录", "Python v3.13.0 运行时升级", "Tool"),
        ):
            with self.subTest(a=a):
                self.assertTrue(same(a, b, t))

    def test_repo_prefixed_names_only_match_same_repo(self):
        """2026-10-22 三模型对比第 1 轮：仓库:路径 被后缀规则并进不带前缀的同名文件。"""
        same = xi._lexically_same
        self.assertFalse(same("report-portal:tests/test_deployment_standard.py", "test_deployment_standard.py", "File"))
        self.assertFalse(same("task-hub:tests/test_release_marker.py", "tests/test_release_marker.py", "File"))
        self.assertFalse(same("credvault:deploy/nas-release.sh", "kg-hub:deploy/nas-release.sh", "File"))
        self.assertTrue(same("task-hub:tests/test_release_marker.py", "TASK-HUB:tests/test_release_marker.py", "File"))
        self.assertTrue(same("fleet-ops:bin/check-cjk-expansion.py", "fleet-ops:check-cjk-expansion.py", "File"))
        # 不带前缀的两边照旧按后缀规则；URL 与盘符不算仓库前缀
        self.assertTrue(same("bin/check-cjk-expansion.py", "check-cjk-expansion.py", "File"))
        self.assertIsNone(xi._repo_prefix("https://example.com/x.py"))
        self.assertIsNone(xi._repo_prefix("c:/work/x.py"))

    def test_path_suffix_only_for_file_like_non_generic_names(self):
        """2026-10-19 单次模式误并抽查（报告 §4.4）：23 对非精确复用里 7 对可疑/并错，其中 3 对由本规则挡住。"""
        same = xi._lexically_same
        # 挡住：复合标识符不是路径；泛用文件名不能只凭后缀认定
        self.assertFalse(same("edge_similarity_search/node_similarity_search", "node_similarity_search", "Concept"))
        self.assertFalse(same("push/config.go", "config.go", "File"))
        self.assertFalse(same("deploy/docker-compose.yml", "docker-compose.yml", "File"))
        self.assertFalse(same("svc/Dockerfile", "Dockerfile", "File"))
        # 保留：同次抽查判为正确的路径长短写法
        for long_, short in (("bin/check-name-vs-behaviour.py", "check-name-vs-behaviour.py"),
                             ("fleet-ops/lib/fleetops_gate.py", "fleetops_gate.py"),
                             ("tests/test_deployment_standard.py", "test_deployment_standard.py"),
                             ("src/main/java/com/liblibai/sd/web/ai/createx/CreateXIntervalPolicy.java",
                              "CreateXIntervalPolicy.java"),
                             ("libtv/api/handler/tv/mcp/tool_task.go", "api/handler/tv/mcp/tool_task.go"),
                             ("libtv/domain/task/service/node_audio_loudness_hook.go", "node_audio_loudness_hook.go")):
            with self.subTest(long_=long_):
                self.assertTrue(same(long_, short, "File"))
        # 泛用文件名带上目录后就够具体了，仍可做后缀合并
        self.assertTrue(same("libtv/api/router/proxy/task.go", "api/router/proxy/task.go", "File"))

    def test_dry_run_resolves_without_writing(self):
        n = xi.normalize_extraction(ents(("kg-hub", "Project", ""), ("Brand New Thing", "Concept", "")), [],
                                    ENTITY_TYPES, EDGE_TYPES)
        d = FakeDriver(existing_names={"kg-hub": "u-kghub"})
        r = asyncio.run(xi.write_extraction(d, FakeEmbedder(), n, name="x", episode_body="b", source_description="sd",
                                            reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                            extractor_model="m", group_id="kg_hub", dry_run=True))
        self.assertEqual((r.nodes_new, r.nodes_reused, r.episode_uuid), (1, 1, ""))
        self.assertEqual(r.resolved["kg-hub"]["uuid"], "u-kghub")
        self.assertIsNone(r.resolved["brand new thing"]["uuid"])
        writes = [c for c, _ in d.calls if c.startswith(("MERGE", "MATCH (source", "MATCH (e:Episodic"))]
        self.assertEqual(writes, [])


    def test_dry_run_sees_entities_created_earlier_in_the_batch(self):
        """2026-10-22 补交 b0024：dry-run 只查图，同批前面条目会新建的实体不在候选里，误并到正式提交才出现。"""
        def run(extraction, pending):
            return asyncio.run(xi.write_extraction(
                FakeDriver(), FakeEmbedder(), extraction, name="x", episode_body="b", source_description="sd",
                reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc), extractor_model="m",
                group_id="kg_hub", dry_run=True, pending=pending))
        n = xi.normalize_extraction(ents(("writer_lock", "Concept", ""), ("Brand New Thing", "Concept", "")), [],
                                    ENTITY_TYPES, EDGE_TYPES)
        r = run(n, [{"name": "Writer Lock", "type": "Concept"}])
        self.assertEqual(r.resolved["writer_lock"],
                         {"uuid": None, "reused": True, "matched_name": "Writer Lock", "matched_type": "Concept",
                          "match": "similar", "in_batch": True})
        self.assertFalse(r.resolved["brand new thing"]["reused"])
        # 精确同名也算（与 _FIND_EXACT 一样不看类型）；向量候选的类型守卫照旧：同批的 Tool 不能被 Concept 复用
        r = run(n, [{"name": "Writer Lock", "type": "Tool"}, {"name": "brand new thing", "type": "Issue"}])
        self.assertEqual(r.resolved["writer_lock"]["reused"], False)
        self.assertTrue(r.resolved["brand new thing"]["in_batch"])
        # 本条前面新建的实体也参与（正式提交时它已经写进图）；前缀守卫在这里同样生效
        n2 = xi.normalize_extraction(ents(("validate 阶段相似度计数", "Concept", ""),
                                          ("prevalidate 阶段相似度计数", "Concept", ""),
                                          ("validate 阶段相似度计数 统计", "Concept", "")), [], ENTITY_TYPES, EDGE_TYPES)
        r = run(n2, None)
        self.assertFalse(r.resolved["prevalidate 阶段相似度计数"]["reused"])
        self.assertEqual(r.resolved["validate 阶段相似度计数 统计"]["matched_name"], "validate 阶段相似度计数")
        self.assertEqual((r.nodes_new, r.nodes_reused), (2, 1))

    def test_dry_run_reports_matched_node_type_and_summary(self):
        """2026-10-25 opus2000：throttle 与 sd-server 的同名 Java 文件精确同名并到一起，dry-run 只给名字看不出来。
        复用要回显被复用节点的类型、怎么命中的（exact / similar）和 summary 开头。"""
        class Driver(FakeDriver):
            async def execute_query(self, cypher, **params):
                if cypher.startswith("MATCH (n:Entity) WHERE n.group_id = $g AND toLower(n.name)"):
                    self.calls.append((cypher, params))
                    if params["lname"] == "ratelimiter.java":
                        return [{"uuid": "u-rl", "name": "RateLimiter.java", "labels": ["Entity", "File"],
                                 "summary": "throttle 仓库的令牌桶限流器，\n按租户配额" + "长" * 200}], None, None
                    return [], None, None
                return await super().execute_query(cypher, **params)
        n = xi.normalize_extraction(ents(("RateLimiter.java", "Concept", ""), ("redis lock", "Tool", ""),
                                         ("Brand New Thing", "Concept", "")), [], ENTITY_TYPES, EDGE_TYPES)
        d = Driver(similar={"redis lock": ("u-lock", "Redis Lock")})
        r = asyncio.run(xi.write_extraction(d, FakeEmbedder(), n, name="x", episode_body="b", source_description="sd",
                                            reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                            extractor_model="m", group_id="kg_hub", dry_run=True))
        rl = r.resolved["ratelimiter.java"]
        self.assertEqual((rl["uuid"], rl["matched_name"], rl["matched_type"], rl["match"]),
                         ("u-rl", "RateLimiter.java", "File", "exact"))   # 精确同名不看类型：Concept 并进了 File
        self.assertTrue(rl["matched_summary"].startswith("throttle 仓库的令牌桶限流器， 按租户配额"))
        self.assertEqual(len(rl["matched_summary"]), xi.MATCHED_SUMMARY_MAX)
        lock = r.resolved["redis lock"]
        self.assertEqual((lock["matched_name"], lock["matched_type"], lock["match"]), ("Redis Lock", "Tool", "similar"))
        self.assertNotIn("matched_summary", lock)
        self.assertEqual(r.resolved["brand new thing"], {"uuid": None, "reused": False, "matched_name": None})

    def test_pending_is_ignored_outside_dry_run(self):
        n = xi.normalize_extraction(ents(("writer_lock", "Concept", "")), [], ENTITY_TYPES, EDGE_TYPES)
        r = asyncio.run(xi.write_extraction(
            FakeDriver(), FakeEmbedder(), n, name="x", episode_body="b", source_description="sd",
            reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc), extractor_model="m", group_id="kg_hub",
            episode_uuid="ep", pending=[{"name": "Writer Lock", "type": "Concept"}]))
        self.assertEqual((r.nodes_new, r.nodes_reused), (1, 0))


class FakeEmbedder:
    async def create_batch(self, texts):
        return [[float(len(t))] * 3 for t in texts]


class FakeDriver:
    """按语句类型回放：存在的实体名 / 已有边由测试预置。"""

    def __init__(self, existing_names=None, similar=None, existing_edges=None):
        self.existing = {k.lower(): v for k, v in (existing_names or {}).items()}
        self.similar = similar or {}            # name_key -> (uuid, name)
        self.existing_edges = existing_edges or {}  # (src,name,dst) -> uuid
        self.calls = []

    async def execute_query(self, cypher, **params):
        self.calls.append((cypher, params))
        if cypher.startswith("MATCH (n:Entity) WHERE n.group_id = $g AND toLower(n.name)"):
            u = self.existing.get(params["lname"])
            return ([{"uuid": u, "name": params["lname"], "labels": ["Entity"]}] if u else []), None, None
        if "vec.cosineDistance(n.name_embedding" in cypher:
            # 用向量长度反查哪个实体（FakeEmbedder 用名字长度编码）
            for key, (u, name) in self.similar.items():
                if params["v"][0] == float(len(name)):
                    return [{"uuid": u, "name": name, "labels": ["Entity", "Tool"], "score": 0.97}], None, None
            return [], None, None
        if cypher.startswith("MATCH (a:Entity {uuid: $src})-[e:RELATES_TO"):
            assert "fact" in params, "边复用必须带 fact 条件"
            hit = self.existing_edges.get((params["src"], params["name"], params["dst"]))
            # 预置值可为 uuid（任意 fact 都算同一条）或 (uuid, fact)（只有 fact 原文相同才命中）
            if isinstance(hit, tuple):
                hit = hit[0] if hit[1] == params["fact"] else None
            return ([{"uuid": hit, "episodes": ["old-ep"]}] if hit else []), None, None
        return [{"uuid": params.get("uuid") or "x"}], None, None


class WriteTest(unittest.TestCase):
    def _write(self, driver, extraction):
        return asyncio.run(xi.write_extraction(
            driver, FakeEmbedder(), extraction, name="claude-mem-obs-1", episode_body="body",
            source_description="claude-mem obs id=1", reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
            extractor_model="claude-fable-5-1", group_id="kg_hub", extractor_session="s1",
            prompt_version="v1", episode_uuid="ep-1"))

    def test_reuse_exact_similar_and_edges_with_provenance(self):
        n = xi.normalize_extraction(
            ents(("kg-hub", "Project", ""), ("FalkorDB", "Tool", "graph db"), ("Writer Lock", "Concept", "")),
            edges(("kg-hub", "depends_on", "FalkorDB", "kg-hub stores graph in FalkorDB"),
                  ("Writer Lock", "belongs_to", "kg-hub", "lock lives in kg-hub")),
            ENTITY_TYPES, EDGE_TYPES)
        d = FakeDriver(existing_names={"kg-hub": "u-kghub"},
                       similar={"falkordb": ("u-falkor", "falkordb")},
                       existing_edges={("u-kghub", "depends_on", "u-falkor"): "e-old"})
        r = self._write(d, n)
        self.assertEqual((r.nodes_new, r.nodes_reused, r.edges_new, r.edges_reused), (1, 2, 1, 1))
        self.assertEqual(r.resolved["kg-hub"], {"uuid": "u-kghub", "reused": True, "matched_name": "kg-hub",
                                                "matched_type": None, "match": "exact"})
        self.assertEqual(r.resolved["falkordb"]["uuid"], "u-falkor")
        saved_nodes = [p for c, p in d.calls if c.startswith("MERGE (n:Entity")]
        self.assertEqual(len(saved_nodes), 1)
        node = saved_nodes[0]["entity_data"]
        self.assertEqual((node["name"], node["extractor_model"], node["group_id"]),
                         ("Writer Lock", "claude-fable-5-1", "kg_hub"))
        self.assertIn("SET n:Concept:Entity", [c for c, _ in d.calls if c.startswith("MERGE (n:Entity")][0])
        ep = [p for c, p in d.calls if c.startswith("MERGE (e:Episodic")][0]
        self.assertEqual((ep["extractor_model"], ep["extractor_session"], ep["prompt_version"], ep["valid_at"]),
                         ("claude-fable-5-1", "s1", "v1", "2026-10-17T00:00:00+00:00"))
        mentions = [p for c, p in d.calls if "MERGE (episode)-[e:MENTIONS" in c]
        self.assertEqual({m["entity_uuid"] for m in mentions}, {"u-kghub", "u-falkor", r.resolved["writer lock"]["uuid"]})
        appended = [p for c, p in d.calls if c.startswith("MATCH ()-[e:RELATES_TO {uuid: $uuid}]->()")]
        self.assertEqual(appended, [{"uuid": "e-old", "ep": "ep-1"}])
        new_edge = [p for c, p in d.calls if c.startswith("MATCH (source:Entity")][0]["edge_data"]
        self.assertEqual((new_edge["name"], new_edge["raw_name"], new_edge["episodes"], new_edge["extractor_model"]),
                         ("belongs_to", "belongs_to", ["ep-1"], "claude-fable-5-1"))
        self.assertTrue(all("vecf32" in c for c, _ in d.calls if c.startswith("MERGE (n:Entity") or c.startswith("MATCH (source:Entity")))
        link = [p for c, p in d.calls if c.startswith("MATCH (e:Episodic {uuid: $uuid}) SET e.entity_edges")][0]
        self.assertEqual(set(link["entity_edges"]), {"e-old", new_edge["uuid"]})

    def test_same_triple_different_fact_makes_new_edge(self):
        """同 (源, 边名, 目标) 但 fact 不同 → 新边；fact 相同 → 复用并追加 episode。
        否则记忆改版的新 fact 永远写不进图（T-0332）。"""
        def extraction(fact):
            return xi.normalize_extraction(
                ents(("kg-hub", "Project", ""), ("Lesson A", "Lesson", "")),
                edges(("Lesson A", "belongs_to", "kg-hub", fact)), ENTITY_TYPES, EDGE_TYPES)
        preset = {("u-lesson", "belongs_to", "u-kghub"): ("e-old", "旧写法：slug：结论")}
        d = FakeDriver(existing_names={"kg-hub": "u-kghub", "Lesson A": "u-lesson"}, existing_edges=preset)
        r = self._write(d, extraction("结论"))
        self.assertEqual((r.edges_new, r.edges_reused), (1, 0))
        self.assertEqual([p for c, p in d.calls if c.startswith("MATCH ()-[e:RELATES_TO {uuid: $uuid}]->()")], [])
        find = [p for c, p in d.calls if c.startswith("MATCH (a:Entity {uuid: $src})-[e:RELATES_TO")][0]
        self.assertEqual(find["fact"], "结论")
        self.assertIn("e.fact = $fact", xi._FIND_EDGE)
        d2 = FakeDriver(existing_names={"kg-hub": "u-kghub", "Lesson A": "u-lesson"}, existing_edges=preset)
        r2 = self._write(d2, extraction("旧写法：slug：结论"))
        self.assertEqual((r2.edges_new, r2.edges_reused), (0, 1))
        self.assertEqual([p for c, p in d2.calls if c.startswith("MATCH ()-[e:RELATES_TO {uuid: $uuid}]->()")],
                         [{"uuid": "e-old", "ep": "ep-1"}])

    def test_type_guard_blocks_cross_type_vector_reuse(self):
        n = xi.normalize_extraction(ents(("connection-status 监控", "Tool", "探针")), [], ENTITY_TYPES, EDGE_TYPES)

        class D(FakeDriver):
            async def execute_query(self, cypher, **params):
                if "vec.cosineDistance(n.name_embedding" in cypher:
                    return [{"uuid": "u-issue", "name": "connection-status 监控误报", "labels": ["Entity", "Issue"], "score": 0.97}], None, None
                return await super().execute_query(cypher, **params)
        r = asyncio.run(xi.write_extraction(D(), FakeEmbedder(), n, name="x", episode_body="b",
                                            source_description="sd", reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                            extractor_model="m", group_id="kg_hub", episode_uuid="ep"))
        self.assertEqual((r.nodes_new, r.nodes_reused), (1, 0))   # Issue 候选不能当 Tool 复用
        r = asyncio.run(xi.write_extraction(D(), FakeEmbedder(), n, name="x", episode_body="b",
                                            source_description="sd", reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                            extractor_model="m", group_id="kg_hub", episode_uuid="ep", type_guard=False))
        self.assertEqual((r.nodes_new, r.nodes_reused), (0, 1))

    def test_check_names(self):
        class D:
            async def execute_query(self, cypher, **params):
                self.names = params["names"]
                return [{"name": "claude-mem-obs-1", "status": "ok"}], None, None
        d = D()
        got = asyncio.run(xi.check_names(d, ["claude-mem-obs-1", "claude-mem-obs-2"]))
        self.assertEqual(got, {"claude-mem-obs-1": "ok"})
        self.assertEqual(d.names, ["claude-mem-obs-1", "claude-mem-obs-2"])
        self.assertEqual(asyncio.run(xi.check_names(d, [])), {})

    def test_check_keys(self):
        class D:
            async def execute_query(self, cypher, **params):
                return [{"sd": "a", "sid": "1", "status": "ok", "episode_uuid": "u"}], None, None
        got = asyncio.run(xi.check_keys(D(), [{"source_description": "a", "source_obs_id": "1"},
                                              {"source_description": "a", "source_obs_id": "2"}]))
        self.assertEqual(got, {("a", "1"): {"status": "ok", "episode_uuid": "u"}})
        self.assertEqual(asyncio.run(xi.check_keys(D(), [])), {})



class ProvenanceFieldsTest(unittest.TestCase):
    """框架 §8-1：extractor_tool / run_id 随 episode、新实体、新边落图；不给时为 None（与旧内容一致）。"""

    def _write(self, **kw):
        n = xi.normalize_extraction(ents(("Brand New", "Concept", ""), ("Other New", "Tool", "")),
                                    edges(("Brand New", "references", "Other New", "f")), ENTITY_TYPES, EDGE_TYPES)
        d = FakeDriver()
        asyncio.run(xi.write_extraction(d, FakeEmbedder(), n, name="x", episode_body="b", source_description="sd",
                                        reference_time=datetime(2026, 10, 19, tzinfo=timezone.utc),
                                        extractor_model="m", group_id="kg_hub", episode_uuid="ep", **kw))
        ep = [p for c, p in d.calls if c.startswith("MERGE (e:Episodic")][0]
        node = [p for c, p in d.calls if c.startswith("MERGE (n:Entity")][0]["entity_data"]
        edge = [p for c, p in d.calls if c.startswith("MATCH (source:Entity")][0]["edge_data"]
        cy = [c for c, _ in d.calls if c.startswith("MERGE (e:Episodic")][0]
        return ep, node, edge, cy

    def test_tool_and_run_id_written_everywhere(self):
        ep, node, edge, cy = self._write(extractor_tool="codex", run_id="20261019-codex-s50")
        self.assertIn("extractor_tool: $extractor_tool, run_id: $run_id", cy)
        self.assertEqual((ep["extractor_tool"], ep["run_id"]), ("codex", "20261019-codex-s50"))
        self.assertEqual((node["extractor_tool"], node["run_id"]), ("codex", "20261019-codex-s50"))
        self.assertEqual((edge["extractor_tool"], edge["run_id"]), ("codex", "20261019-codex-s50"))
        self.assertEqual(edge["extractor_model"], "m")

    def test_defaults_to_none_for_compat(self):
        ep, node, edge, _ = self._write()
        self.assertEqual((ep["extractor_tool"], ep["run_id"]), (None, None))
        self.assertEqual((node["extractor_tool"], node["run_id"]), (None, None))
        self.assertEqual((edge["extractor_tool"], edge["run_id"]), (None, None))


if __name__ == "__main__":
    unittest.main()
