"""积压消化链路看板：卡点判定、积压消化口径、图与页面渲染。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flow_dashboard as F  # noqa: E402
from utils import ingest_timing  # noqa: E402
from utils import flow_metrics  # noqa: E402

NOW = datetime(2026, 9, 28, 4, 30, tzinfo=timezone.utc)
SERVER = (ROOT / "kg_hub_server.py").read_text("utf-8")


def status(**overrides) -> dict:
    base = {"heartbeat_at": NOW.isoformat(), "ts": NOW.isoformat(),
            "backlog_window_open": True, "backlog_remaining": 5000,
            "boundary_id": 20000, "per_cycle": 70, "live_per_cycle": 30}
    base.update(overrides)
    return base


def hourly(hours: int, backlog_ingested: int, backlog_rejected: int = 0,
           deferred: int = 0) -> dict:
    out = {}
    for i in range(hours):
        key = (NOW - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        out[key] = {"backlog": {"ingested": backlog_ingested, "rejected": backlog_rejected,
                                "deferred": deferred}}
    return out


def build(**kw) -> dict:
    args = {"status": status(), "snapshots": [], "gateway_node": None,
            "keys": {"by_status": {"ok": 10}, "pending": 0, "pending_oldest_s": None,
                     "errors_24h": {}, "duration_p50": 30.0, "duration_p90": 60.0,
                     "duration_samples": 10},
            "timing": {"samples": 0}, "active": 1, "graph_daily": [], "now": NOW}
    args.update(kw)
    return F.build_flow(**args)


def titles(flow: dict) -> list[str]:
    return [b["title"] for b in flow["bottlenecks"]]


class BottleneckTests(unittest.TestCase):
    def test_outside_window_is_a_planned_stop_not_a_red_failure(self):
        flow = build(status=status(backlog_window_open=False, idle_outside_window=True))
        self.assertEqual(flow["primary"]["title"], "工作窗口外，积压暂停")
        self.assertTrue(flow["primary"]["deliberate"])
        refinery = next(s for s in flow["stages"] if s["id"] == "refinery")
        self.assertNotEqual(refinery["state"], "red")

    def test_missing_heartbeat_outranks_a_planned_pause(self):
        flow = build(status=status(heartbeat_at=(NOW - timedelta(hours=1)).isoformat(),
                                   idle_outside_window=True))
        self.assertEqual(flow["primary"]["title"], "refinery 无心跳")
        refinery = next(s for s in flow["stages"] if s["id"] == "refinery")
        self.assertEqual(refinery["state"], "red")

    def test_lock_queue_dominating_points_at_kghub(self):
        flow = build(timing={"samples": 20, "wait_share": 0.7, "wait_p50": 300.0,
                             "extract_p50": 90.0, "extract_p90": 200.0})
        self.assertEqual(flow["primary"]["stage"], "kghub")
        self.assertEqual(flow["primary"]["title"], "写锁排队是主要耗时")
        self.assertNotIn("单条抽取耗时高", titles(flow))

    def test_parallel_mode_queue_is_slot_wait_not_writer_lock(self):
        flow = build(timing={"samples": 20, "wait_share": 0.7, "wait_p50": 300.0,
                             "extract_p50": 90.0, "extract_p90": 200.0, "parallel": True})
        self.assertEqual(flow["primary"]["title"], "并发槽位排队是主要耗时")
        self.assertNotIn("写锁排队是主要耗时", titles(flow))
        self.assertEqual(flow["efficiency"]["queue_label"], "槽位排队")
        kghub = next(s for s in flow["stages"] if s["id"] == "kghub")
        self.assertIn("槽位排队 P50", kghub["detail"])

    def test_among_slow_items_the_cause_outranks_error_count_and_eta(self):
        keys = {"by_status": {"ok": 10}, "pending": 0, "pending_oldest_s": None,
                "errors_24h": {"upstream_error": 30}, "duration_p50": 30.0,
                "duration_p90": 60.0, "duration_samples": 10}
        flow = build(keys=keys, timing={"samples": 20, "wait_share": 0.6, "wait_p50": 200.0,
                                        "extract_p50": 90.0, "extract_p90": 200.0})
        self.assertIn("近 24h 抽取失败偏多", titles(flow))
        self.assertEqual(flow["primary"]["title"], "写锁排队是主要耗时")

    def test_every_slow_title_has_an_impact_rank(self):
        source = (ROOT / "flow_dashboard.py").read_text("utf-8")
        import re
        slow = set(re.findall(r'add\("\w+", "slow", "([^"]+)"', source))
        self.assertTrue(slow)
        self.assertEqual(slow - set(F.SLOW_IMPACT_ORDER), set())

    def test_slow_extraction_without_queueing_is_a_different_diagnosis(self):
        flow = build(timing={"samples": 20, "wait_share": 0.05, "wait_p50": 1.0,
                             "extract_p50": 180.0, "extract_p90": 360.0})
        self.assertIn("单条抽取耗时高", titles(flow))
        self.assertNotIn("写锁排队是主要耗时", titles(flow))

    def test_too_few_timing_samples_fall_back_to_key_durations(self):
        keys = {"by_status": {}, "pending": 0, "pending_oldest_s": None, "errors_24h": {},
                "duration_p50": 200.0, "duration_p90": 400.0, "duration_samples": 50}
        flow = build(keys=keys, timing={"samples": 2, "wait_share": 0.9})
        self.assertIn("单条入图耗时高", titles(flow))
        self.assertNotIn("写锁排队是主要耗时", titles(flow))

    def test_stuck_pending_key_turns_kghub_red(self):
        keys = {"by_status": {"pending": 1}, "pending": 1, "pending_oldest_s": 3600,
                "errors_24h": {}, "duration_p50": None, "duration_p90": None,
                "duration_samples": 0}
        flow = build(keys=keys)
        kghub = next(s for s in flow["stages"] if s["id"] == "kghub")
        self.assertEqual(kghub["state"], "red")
        self.assertEqual(flow["primary"]["title"], "有抽取任务卡在 pending")

    def test_recovery_pause_is_attributed_to_the_gateway(self):
        flow = build(status=status(recovery_reason="provider_circuit_open",
                                   recovery_retry_at="2026-09-28T04:35:00+00:00"))
        self.assertEqual(flow["primary"]["stage"], "gateway")
        self.assertIn("provider_circuit_open", flow["primary"]["title"])

    def test_zero_digestion_over_a_full_day_is_a_stop(self):
        st = status(budget_today={"day": "2026-09-28", "hourly": hourly(24, 0, deferred=3)})
        flow = build(status=st)
        self.assertIn("近 24h 积压零消化", titles(flow))

    def test_high_deferred_share_names_its_reasons(self):
        st = status(budget_today={"day": "2026-09-28", "hourly": hourly(24, 1, deferred=2),
                                  "lines": {"backlog": {"result_counts": {"409": 30, "upstream_error": 5}}}})
        flow = build(status=st)
        item = next(b for b in flow["bottlenecks"] if b["title"] == "推迟/重试占比高")
        self.assertIn("409 30", item["evidence"])

    def test_red_capture_stage_is_reported_without_blaming_the_backlog(self):
        snap = {"_host": "mac", "nodes": [{"id": "worker", "layer": "worker",
                                           "label": "claude-mem", "state": "red",
                                           "detail": "health 不可达"}]}
        flow = build(snapshots=[snap])
        item = next(b for b in flow["bottlenecks"] if b["stage"] == "claude_mem")
        self.assertEqual(item["level"], "stop")
        self.assertIn("不直接影响历史积压", item["action"])


class DigestTests(unittest.TestCase):
    def test_remaining_history_records_real_samples_and_skips_stale_status(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"KG_HUB_INGEST_BACKUP_PATH": str(Path(tmp) / "ingest-backup.jsonl")}):
            first = F.backlog_remaining_history(status(backlog_remaining=5000), NOW)
            self.assertEqual([r["remaining"] for r in first], [5000])
            self.assertEqual(first[0]["at_beijing"], "2026-09-28 12:30:00 北京时间")
            same_bucket = F.backlog_remaining_history(
                status(backlog_remaining=4998), NOW + timedelta(seconds=30))
            self.assertEqual([r["remaining"] for r in same_bucket], [4998])
            later = F.backlog_remaining_history(
                status(backlog_remaining=4995), NOW + timedelta(minutes=2))
            self.assertEqual([r["remaining"] for r in later], [4998, 4995])
            stale = F.backlog_remaining_history(
                status(backlog_remaining=4000), NOW + timedelta(minutes=20))
            self.assertEqual([r["remaining"] for r in stale], [4998, 4995])

    def test_eta_uses_a_full_day_of_terminal_observations(self):
        st = status(backlog_remaining=4800,
                    budget_today={"day": "2026-09-28", "hourly": hourly(24, 8, 2)})
        digest = F.backlog_digest(st, None, NOW)
        self.assertEqual(digest["last24"]["backlog_terminal"], 240)
        self.assertEqual(digest["accept_rate"], 80.0)
        self.assertEqual(digest["eta_days"], 20.0)
        self.assertEqual(digest["last24"]["active_hours"], 24)

    def test_short_samples_do_not_produce_a_24h_extrapolation(self):
        st = status(budget_today={"day": "2026-09-28", "hourly": hourly(3, 10)})
        digest = F.backlog_digest(st, None, NOW)
        self.assertIsNone(digest["eta_days"])
        self.assertLess(digest["last24"]["coverage_h"], 20)

    def test_graph_fallback_uses_seven_complete_days_of_backlog_lane(self):
        rows = [{"bucket": (NOW - timedelta(days=i)).strftime("%Y-%m-%d"),
                 "lane": "积压线", "count": 70} for i in range(0, 8)]
        digest = F.backlog_digest(status(backlog_remaining=700), rows, NOW)
        self.assertEqual(digest["backlog_ingested_per_day_7d"], 70.0)
        self.assertEqual(digest["graph_eta_days"], 10.0)

    def test_calls_ratio_uses_todays_graph_episodes_not_refinery_memory(self):
        node = {"metrics": {"keys": {"kg_hub.entity_extract": {"today": 2000}}}}
        today = {"daily": [{"day": "2026-09-27", "积压线": 500},
                           {"day": "2026-09-28", "积压线": 60, "live 线": 40}]}
        self.assertEqual(F.calls_per_observation(node, today, NOW), 20.0)
        yesterday_only = {"daily": [{"day": "2026-09-27", "积压线": 500}]}
        self.assertIsNone(F.calls_per_observation(node, yesterday_only, NOW))

    def test_calls_ratio_survives_a_refinery_restart(self):
        node = {"metrics": {"keys": {"kg_hub.entity_extract": {"today": 3183}}}}
        budget = {"day": NOW.strftime("%Y-%m-%d"), "terminal_total": 2, "hourly": {}}
        rows = [{"bucket": NOW.strftime("%Y-%m-%d"), "lane": "live 线", "count": 152}]
        flow = build(status=status(budget_today=budget), graph_daily=rows,
                     gateway_node={"state": "green", **node})
        self.assertEqual(flow["efficiency"]["calls_per_observation"], 20.9)

    def test_calls_ratio_withheld_when_too_few_episodes_today(self):
        node = {"metrics": {"keys": {"kg_hub.entity_extract": {"today": 90}}}}
        few = {"daily": [{"day": "2026-09-28", "live 线": 3}]}
        self.assertIsNone(F.calls_per_observation(node, few, NOW))


class HourlyGapTests(unittest.TestCase):
    def test_idle_hours_between_records_show_as_zero_rows(self):
        first = (NOW - timedelta(hours=5)).strftime("%Y-%m-%dT%H")
        last = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
        budget = {"day": NOW.strftime("%Y-%m-%d"), "hourly": {
            first: {"backlog": {"ingested": 3}}, last: {"backlog": {"ingested": 4}}}}
        digest = F.backlog_digest(status(budget_today=budget), None, NOW)
        keys = [h["hour"] for h in digest["hourly"]]
        self.assertEqual(len(keys), 6)
        self.assertEqual(keys[0], first)
        self.assertEqual(keys[-1], NOW.strftime("%Y-%m-%dT%H"))
        self.assertEqual(digest["last24"]["active_hours"], 2)
        self.assertEqual(digest["last24"]["backlog_ingested"], 7)


class StageTests(unittest.TestCase):
    def test_stale_snapshot_is_not_treated_as_live_state(self):
        snap = {"_host": "mac", "_snapshot_stale": True,
                "nodes": [{"id": "sync", "layer": "transport", "state": "green",
                           "metrics": {"local_max_obs_id": 120, "nas_max_obs_id": 100}}]}
        stages = F.probe_stages([snap])
        self.assertEqual(stages["sync"]["state"], "grey")
        self.assertEqual(stages["sync"]["metrics"]["lag_rows"], 20)
        self.assertEqual(stages["tools"]["sub"], "无探针快照")
        self.assertEqual(stages["sync"]["sub"], "落差 20 条")

    def test_nas_ahead_of_one_device_reads_as_synced(self):
        snap = {"_host": "mac", "nodes": [{"id": "sync", "layer": "transport", "state": "green",
                "metrics": {"local_max_obs_id": 31772, "nas_max_obs_id": 32593,
                            "lag_rows": -821}}]}
        stages = F.probe_stages([snap])
        self.assertEqual(stages["sync"]["sub"], "已同步 · NAS 汇总领先本机 821 条")

    def test_worker_queue_depth_is_surfaced(self):
        snap = {"_host": "mac", "nodes": [{"id": "worker", "layer": "worker", "state": "green",
                                           "metrics": {"queue_depth": 42}}]}
        self.assertEqual(F.probe_stages([snap])["claude_mem"]["sub"], "内存队列 42")

    def test_migrated_host_uses_current_worker_not_retired_probe(self):
        snap = {"_host": "mac", "nodes": [
            {"id": "worker", "state": "red", "detail": "old :37701 unavailable"},
            {"id": "sqlite", "state": "amber", "detail": "old database stale"}],
            "claude_mem_queue": {"current": [{"worker": "current", "at": NOW.timestamp(),
                "depth": 0, "pid": "29149", "held": 0, "error": None}]}}
        stage = F.probe_stages([snap], NOW)["claude_mem"]
        self.assertEqual(stage["state"], "green")
        self.assertNotIn("old", stage["detail"])
        current = snap["claude_mem_queue"]["current"][0]
        current.update(depth=None, error="队列接口不可用")
        self.assertEqual(F.probe_stages([snap], NOW)["claude_mem"]["state"], "red")
        current.update(depth=0, error=None, at=NOW.timestamp()-1201)
        self.assertEqual(F.probe_stages([snap], NOW)["claude_mem"]["state"], "grey")
        current.update(at=NOW.timestamp(), held_error="待核验账本不可读")
        self.assertEqual(F.probe_stages([snap], NOW)["claude_mem"]["state"], "amber")


class DiagramTests(unittest.TestCase):
    def test_topology_marks_the_choke_and_escapes_labels(self):
        flow = build(timing={"samples": 20, "wait_share": 0.8, "wait_p50": 100.0,
                             "extract_p50": 20.0})
        text = flow["diagrams"]["topology"]
        self.assertIn("class s_kghub choke", text)
        stages = [{"id": "tools", "label": 'a"b[c]#d', "sub": "x<y>", "state": "green",
                   "metrics": {}}]
        rendered = F.topology_mermaid(stages, None, {})
        node_line = rendered.splitlines()[1]
        self.assertNotIn('b[c', node_line)
        self.assertEqual(node_line.count('"'), 2)
        self.assertNotIn("#", node_line)

    def test_no_node_id_is_a_mermaid_keyword(self):
        import re
        reserved = {"graph", "flowchart", "subgraph", "end", "class", "classDef",
                    "style", "linkStyle", "click", "direction", "default"}
        for kind, text in build()["diagrams"].items():
            ids = set(re.findall(r"^\s*([A-Za-z_][\w]*)\s*[\[\(\{]", text, re.M))
            ids |= set(re.findall(r"^\s*([A-Za-z_]\w*)\s*(?:-->|<-->|-\.->|==>)", text, re.M))
            ids |= set(re.findall(r"(?:-->|<-->|-\.->|==>)\s*(?:\|[^|]*\|\s*)?([A-Za-z_]\w*)", text))
            self.assertEqual(ids & reserved, set(), kind)

    def test_all_five_diagram_kinds_are_present(self):
        flow = build()
        self.assertEqual(set(flow["diagrams"]),
                         {"topology", "usecase", "flow", "arch", "app_arch"})
        for text in flow["diagrams"].values():
            self.assertTrue(text.startswith("flowchart"))


class RenderTests(unittest.TestCase):
    def test_page_embeds_data_without_breaking_out_of_script(self):
        flow = build(status=status(last_error="</script><img src=x>"))
        with patch.object(F, "collect_flow", AsyncMock(return_value=flow)):
            response = asyncio.run(F.dashboard_flow(__import__("starlette.requests", fromlist=["Request"]).Request({"type":"http","query_string":b""})))
        html = response.body.decode()
        self.assertNotIn("</script><img", html)
        payload = html.split("const D=", 1)[1].split(";\nconst $", 1)[0]
        self.assertEqual(json.loads(payload)["primary"]["title"], "refinery 本轮异常")

    def test_json_endpoint_returns_the_same_structure(self):
        flow = build()
        with patch.object(F, "collect_flow", AsyncMock(return_value=flow)):
            response = asyncio.run(F.dashboard_flow_json(__import__("starlette.requests", fromlist=["Request"]).Request({"type":"http","query_string":b""})))
        self.assertEqual(json.loads(response.body)["generated_at"], flow["generated_at"])

    def test_beijing_labels_keep_utc_source_buckets_and_cross_midnight(self):
        at = datetime(2026, 9, 28, 17, 15, tzinfo=timezone.utc)
        hour = "2026-09-28T17"
        flow = build(now=at,
                     status=status(heartbeat_at=at.isoformat(), ts=at.isoformat(),
                                   budget_today={"hourly": {hour: {"backlog": {"ingested": 2}}}}),
                     graph_daily=[{"bucket": "2026-09-28", "lane": "积压线", "count": 2}],
                     key_trends=[{"hour": hour, "ingested": 2}])
        self.assertEqual(flow["generated_at_beijing"], "2026-09-29 01:15:00 北京时间")
        self.assertEqual(flow["key_trends"][0]["hour"], hour)
        self.assertEqual(flow["key_trends"][0]["hour_beijing"], "2026-09-29 01:00")
        self.assertEqual(flow["backlog"]["hourly"][0]["hour_beijing"], "2026-09-29 01:00")
        self.assertEqual(flow["backlog"]["daily"][0]["day_beijing_start"], "2026-09-28 08:00")
        refinery = next(s for s in flow["stages"] if s["id"] == "refinery")
        self.assertIn("2026-09-29 01:15:00 北京时间", refinery["detail"])
        graph = next(s for s in flow["stages"] if s["id"] == "graph")
        self.assertIn("2026-09-28 08:00 至次日 08:00", graph["detail"])
        self.assertEqual(F._beijing_hour("2026-12-31T17"), "2027-01-01 01:00")

    def test_page_uses_beijing_labels_for_charts_and_hover(self):
        html = F._HTML
        self.assertIn("row.hour_beijing", html)
        self.assertIn("r.hour_beijing+' 北京时间'", html)
        self.assertIn("r.label.slice(5)", html)
        self.assertIn("D.generated_at_beijing", html)
        self.assertIn("data-backlog-chart", html)


class TimeRangeTests(unittest.TestCase):
    def test_beijing_midnight_and_yesterday_are_half_open(self):
        now = datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc)
        today = F.time_range("today", now)
        yesterday = F.time_range("yesterday", now)
        self.assertEqual(today["start"], "2026-10-06T16:00:00+00:00")
        self.assertEqual(yesterday["start"], "2026-10-05T16:00:00+00:00")
        self.assertEqual(yesterday["end"], today["start"])
        for key, hours in {"month":720,"week":168,"day":24,"6h":6,"3h":3,"1h":1}.items():
            w = F.time_range(key, now)
            self.assertEqual((F._parse_ts(w["end"])-F._parse_ts(w["start"])).total_seconds(), hours*3600)

    def test_selected_partial_hour_filters_events_and_inflight_denominator(self):
        start = NOW - timedelta(hours=1)
        commits = [{"at":(start-timedelta(seconds=1)).timestamp(), "conflict":False,
                    "lock_wait_s":900,"commit_s":900,"prevalidated_conflict":False,"validate_skipped":False},
                   {"at":start.timestamp(),"conflict":False,"lock_wait_s":2,"commit_s":4,
                    "prevalidated_conflict":False,"validate_skipped":False}]
        rows = F.key_metric_trends(now=NOW, window_start=start, commits=commits,
            attempts=[((start-timedelta(minutes=5)).isoformat(), (start+timedelta(minutes=15)).isoformat(), "completed"),
                      (NOW.isoformat(), NOW.isoformat(), "completed")], outcomes=[])
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["model_calls"] for r in rows), 0)
        self.assertEqual(rows[0]["model_inflight_avg"], .5)
        self.assertEqual(rows[0]["commit_p50"], 4)

    def test_yesterday_has_24_hours_without_today_bucket(self):
        w=F.time_range("yesterday", NOW)
        rows=F.key_metric_trends(now=F._parse_ts(w["end"]),window_start=F._parse_ts(w["start"]),
                                commits=[],attempts=None,outcomes=None)
        self.assertEqual(len(rows),24)
        self.assertEqual(F._beijing_hour(rows[0]["hour"]),"2026-09-27 00:00")
        self.assertEqual(F._beijing_hour(rows[-1]["hour"]),"2026-09-27 23:00")
        self.assertTrue(all(r["model_calls"] is None for r in rows))

    def test_default_and_invalid_range_endpoints(self):
        from starlette.requests import Request
        with patch.object(F,"collect_flow",AsyncMock(return_value={})) as collect:
            asyncio.run(F.dashboard_flow_json(Request({"type":"http","query_string":b""})))
            collect.assert_awaited_once_with("today")
        with patch.object(F,"collect_flow",AsyncMock()) as collect:
            r=asyncio.run(F.dashboard_flow_json(Request({"type":"http","query_string":b"range=bad"})))
            self.assertEqual(r.status_code,400)
            collect.assert_not_awaited()

    def test_queries_bound_both_ends(self):
        driver=type("Driver",(),{"execute_query":AsyncMock(return_value=([],None,None))})()
        start=NOW-timedelta(hours=1)
        asyncio.run(F._hourly_outcomes(driver,NOW,start))
        args,kw=driver.execute_query.call_args
        self.assertIn("k.updated_at < $until",args[0])
        self.assertEqual(kw,{"since":start.isoformat(),"until":NOW.isoformat()})
        asyncio.run(F._graph_period(driver,100,start,NOW))
        self.assertEqual(driver.execute_query.call_args.kwargs["until"],NOW.isoformat())

    def test_all_presets_render_with_one_axis_and_preserve_url(self):
        import shutil, subprocess
        if not shutil.which("node"):
            self.skipTest("node is needed for dashboard JavaScript smoke test")
        for key in F.RANGE_LABELS:
            w=F.time_range(key,NOW)
            rows=F.key_metric_trends(now=F._parse_ts(w["end"]),window_start=F._parse_ts(w["start"]),
                                    commits=[],attempts=[],outcomes=[])
            data=build(key_trends=rows)
            data.update(time_range=w,range_options=F.RANGE_LABELS,
                        period=F.selected_period({},w,NOW,rows,[],[]))
            script=F._HTML.split("<script>")[1].split("</script>")[0].replace("__DATA__",json.dumps(data))
            harness = """const elements={};
const element=()=>({innerHTML:'',textContent:'',dataset:{},classList:{toggle(){}},append(){},addEventListener(){}});
const document={getElementById:id=>elements[id]??=(element()),createElement:element,querySelectorAll:()=>[]};
const window={};
"""
            script += """
if(!elements['time-ranges'].innerHTML.includes('href="?range='+range.key+'" aria-current="true"'))throw Error('selection');
if((elements['time-ranges'].innerHTML.match(/<a /g)||[]).length!==8)throw Error('options');
if(!elements.keytrends.innerHTML.includes(axisLabel(chartStart)))throw Error('axis start');
if(!elements.keytrends.innerHTML.includes(axisLabel(chartEnd)))throw Error('axis end');
"""
            result=subprocess.run(["node","-e",harness+script],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_refinery_partial_leading_hour_is_not_counted(self):
        w=F.time_range("1h",NOW)
        st=status(budget_today={"hourly":{"2026-09-28T03":{"backlog":{"ingested":99}},
                                          "2026-09-28T04":{"backlog":{"ingested":2}}}})
        period=F.selected_period(st,w,NOW,[],[],None)
        self.assertEqual(period["backlog"]["ingested"],2)
        self.assertEqual(period["coverage_h"],.5)


class WiringTests(unittest.TestCase):
    def test_routes_and_portal_entry_are_registered(self):
        self.assertIn('Route("/dashboard/flow", dashboard_flow', SERVER)
        self.assertIn('Route("/dashboard/flow.json", dashboard_flow_json', SERVER)
        portal = SERVER.split("PORTAL_REPORTS =", 1)[1].split("]\n", 1)[0]
        self.assertIn('"url": "/dashboard/flow"', portal)

    def test_retired_pipeline_page_redirects_to_flow(self):
        portal = SERVER.split("PORTAL_REPORTS =", 1)[1].split("]\n", 1)[0]
        self.assertNotIn("/dashboard/pipeline", portal)
        route = SERVER.split('Route("/dashboard/pipeline",', 1)[1].split("methods=", 1)[0]
        self.assertIn('RedirectResponse("/dashboard/flow", status_code=301)', route)

    def test_every_extraction_exit_records_timing(self):
        body = SERVER.split("async def _do_extract_inner", 1)[1].split("\nasync def ", 1)[0]
        for call in ('_record_ingest_timing(started, lock_acquired, extract_finished, "ok")',
                     '_record_ingest_timing(started, lock_acquired, extract_finished, "error")',
                     '_record_ingest_timing(started, None, None, "lock_timeout")'):
            self.assertIn(call, body)


class TimingTests(unittest.TestCase):
    def setUp(self):
        ingest_timing.reset()

    def test_summary_splits_queue_and_extraction_time(self):
        for waited, extract in ((10, 90), (30, 70), (60, 140)):
            ingest_timing.record(waited_s=waited, extract_s=extract, outcome="ok", at=1000)
        ingest_timing.record(waited_s=900, extract_s=None, outcome="lock_timeout", at=1000)
        s = ingest_timing.summary(window_s=3600, now=1100)
        self.assertEqual(s["samples"], 4)
        self.assertEqual(s["extract_p50"], 90.0)
        self.assertEqual(s["wait_share"], round(1000 / 1300, 3))
        self.assertEqual(s["outcomes"], {"ok": 3, "lock_timeout": 1})

    def test_old_samples_leave_the_window(self):
        ingest_timing.record(waited_s=5, extract_s=5, outcome="ok", at=0)
        self.assertEqual(ingest_timing.summary(window_s=60, now=1000)["samples"], 0)
        self.assertIsNone(ingest_timing.summary(window_s=60, now=1000)["wait_share"])


class KeyTrendTests(unittest.TestCase):
    def test_commit_samples_survive_process_memory_reset(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"KG_HUB_INGEST_BACKUP_PATH": str(Path(tmp) / "ingest-backup.jsonl")}), \
                patch.object(flow_metrics, "_samples", deque(maxlen=4000)):
            at = NOW.timestamp()
            flow_metrics.record(conflict=False, lock_wait_s=4.5, commit_s=7.2,
                                validate_skipped=True, at=at)
            flow_metrics.record(conflict=True, prevalidated_conflict=True, at=at + 1)
            flow_metrics._samples.clear()  # model a server restart
            rows = flow_metrics.recent(since=at - 1)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["commit_s"], 7.2)
            self.assertTrue(rows[0]["validate_skipped"])
            self.assertTrue(rows[1]["prevalidated_conflict"])
            self.assertEqual(len(flow_metrics.recent(since=at + 1)), 1)

    def test_parallel_commit_samples_remain_unique_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"KG_HUB_INGEST_BACKUP_PATH": str(Path(tmp) / "ingest-backup.jsonl")}), \
                patch.object(flow_metrics, "_samples", deque(maxlen=4000)):
            at = NOW.timestamp()
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda i: flow_metrics.record(conflict=bool(i % 2), at=at + i),
                              range(36)))
            self.assertEqual(len(flow_metrics.recent(since=at)), 36)
            flow_metrics._samples.clear()
            self.assertEqual(len(flow_metrics.recent(since=at)), 36)
            self.assertIsNone(flow_metrics.storage_error())

    def test_archived_hourly_commit_values_fill_only_missing_hours(self):
        now = NOW
        archived = [{"hour": "2026-09-28T03", "lock_wait_avg": 12.0,
                     "commit_avg": 8.0, "commit_attempts": 5, "conflicts": 1,
                     "conflict_rate": 20.0}]
        rows = F.key_metric_trends(now=now, commits=[], attempts=None, outcomes=None,
                                   archived_commits=archived)
        self.assertEqual(rows[-2]["commit_attempts"], 5)
        self.assertEqual(rows[-2]["lock_wait_avg"], 12.0)
        self.assertIsNone(rows[-3]["commit_attempts"])
        live = [{"at": (now - timedelta(hours=1)).timestamp(), "conflict": False,
                 "lock_wait_s": 3, "commit_s": 4, "validate_skipped": False,
                 "prevalidated_conflict": False}]
        rows = F.key_metric_trends(now=now, commits=live, attempts=None, outcomes=None,
                                   archived_commits=archived)
        self.assertEqual(rows[-2]["commit_attempts"], 1)
        self.assertEqual(rows[-2]["lock_wait_avg"], 3.0)

    def test_archived_snapshot_reader_ignores_old_and_empty_hours(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"KG_HUB_INGEST_BACKUP_PATH": str(Path(tmp) / "ingest-backup.jsonl")}):
            seed = Path(tmp) / "flow-commit-hourly-seed.json"
            seed.write_text(json.dumps([
                {"hour": "2026-09-28T03", "commit_attempts": 3, "commit_avg": 5.0},
                {"hour": "2026-09-27T00", "commit_attempts": 2},
                {"hour": "2026-09-28T02", "commit_attempts": None},
            ]), encoding="utf-8")
            rows = flow_metrics.archived_hourly(since=(NOW - timedelta(hours=2)).timestamp())
            self.assertEqual(rows, [{"hour": "2026-09-28T03", "commit_attempts": 3,
                                     "commit_avg": 5.0}])

    def test_hourly_metrics_keep_sample_gaps_and_count_prevalidation(self):
        at = NOW.timestamp()
        commits = [
            {"at": at, "conflict": True, "lock_wait_s": 0,
             "commit_s": None, "validate_skipped": False,
             "prevalidated_conflict": True},
            {"at": at, "conflict": False, "lock_wait_s": 4,
             "commit_s": 10, "validate_skipped": True,
             "prevalidated_conflict": False},
        ]
        attempts = [
            ("2026-09-28T04:10:00+00:00", "2026-09-28T04:11:00+00:00", "completed"),
            ("2026-09-28T04:20:00+00:00", "2026-09-28T04:22:00+00:00", "completed"),
        ]
        rows = F.key_metric_trends(now=NOW, commits=commits, attempts=attempts,
                                   outcomes=[{"hour": "2026-09-28T04", "status": "ok", "count": 1},
                                             {"hour": "2026-09-28T04", "status": "error", "count": 2}])
        current = rows[-1]
        self.assertEqual((current["ingested"], current["errors"]), (1, 2))
        self.assertEqual((current["commit_attempts"], current["conflicts"],
                          current["prevalidated_conflicts"], current["validate_skipped"]),
                         (2, 1, 1, 1))
        self.assertEqual((current["lock_wait_avg"], current["commit_avg"],
                          current["call_duration_avg"], current["calls_per_ingested"]),
                         (4.0, 10.0, 90.0, 2.0))
        self.assertAlmostEqual(current["model_inflight_avg"], .1, places=2)
        self.assertIsNone(rows[-2]["commit_attempts"])
        self.assertEqual(rows[-2]["ingested"], 0)

    def test_missing_sources_are_unknown_not_zero(self):
        rows = F.key_metric_trends(now=NOW, commits=[], attempts=None, outcomes=None)
        self.assertIsNone(rows[-1]["ingested"])
        self.assertIsNone(rows[-1]["model_calls"])
        self.assertIsNone(rows[-1]["commit_attempts"])

    def test_late_reconciliation_does_not_count_as_hours_in_flight(self):
        rows = F.key_metric_trends(
            now=NOW, commits=[], outcomes=[],
            attempts=[("2026-09-28T03:10:00+00:00",
                       "2026-09-28T04:20:00+00:00", "unknown")])
        self.assertEqual(rows[-2]["model_inflight_avg"], .25)
        self.assertEqual(rows[-1]["model_inflight_avg"], 0)

    def test_commit_sample_retains_only_numbers_and_flags(self):
        flow_metrics.record(conflict=True, prevalidated_conflict=True, at=NOW.timestamp())
        sample = flow_metrics.recent(since=NOW.timestamp())[0]
        self.assertEqual(sample["prevalidated_conflict"], True)
        self.assertNotIn("sid", sample)


class ServerModuleIdentityTests(unittest.TestCase):
    """`python kg_hub_server.py` 下,看板读到的必须是正在跑的那份在飞计数。"""

    ALIAS = 'if __name__ == "__main__":\n    sys.modules.setdefault("kg_hub_server", sys.modules[__name__])'

    def test_main_module_registers_itself_before_any_definition(self):
        self.assertIn(self.ALIAS, SERVER)
        self.assertLess(SERVER.index(self.ALIAS), SERVER.index("\ndef "),
                        "别名必须在模块顶部登记,延迟 import 才拿得到同一份")

    def test_lazy_import_resolves_to_the_running_main_module(self):
        import subprocess
        probe = (
            "import sys, types\n"
            f"src = open({str(ROOT / 'kg_hub_server.py')!r}, encoding='utf-8').read()\n"
            f"start = src.index({self.ALIAS!r})\n"
            "block = src[start:src.index('\\n\\n', start)]\n"
            "main = types.ModuleType('__main__'); main.active_extractions = lambda: 7\n"
            "sys.modules['__main__'] = main\n"
            "exec(block, {'__name__': '__main__', 'sys': sys})\n"
            "from kg_hub_server import active_extractions\n"
            "print(active_extractions())\n")
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                             text=True, timeout=30, cwd="/")
        self.assertEqual(out.stdout.strip(), "7", out.stderr)


if __name__ == "__main__":
    unittest.main()
