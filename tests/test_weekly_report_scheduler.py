import asyncio
import importlib
import inspect
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import weekly_report_scheduler as S  # noqa: E402

BJ = timezone(timedelta(hours=8))


def bj(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=BJ).astimezone(timezone.utc)


SUN = bj(2026, 10, 18, 9)            # 一个周日 09:00（北京）
assert SUN.astimezone(BJ).weekday() == 6


class Slots(unittest.TestCase):
    def test_latest_slot_is_the_most_recent_sunday_0900(self):
        self.assertEqual(S.latest_slot(SUN), SUN)                         # 正好到点
        self.assertEqual(S.latest_slot(SUN + timedelta(days=2)), SUN)     # 周二
        self.assertEqual(S.latest_slot(SUN - timedelta(minutes=1)), SUN - timedelta(days=7))

    def test_due_rules(self):
        last_week = S.slot_id(SUN - timedelta(days=7))
        self.assertEqual(S.due_slot(SUN + timedelta(minutes=1), last_week), '2026-10-18')
        self.assertIsNone(S.due_slot(SUN + timedelta(minutes=1), '2026-10-18'))   # 已跑过
        # 发布压在周日上午、当晚才起来：24 小时内补跑
        self.assertEqual(S.due_slot(SUN + timedelta(hours=11), last_week), '2026-10-18')
        # 首次上线在工作日：不补跑，等下一个周日
        self.assertIsNone(S.due_slot(SUN + timedelta(hours=25), None))
        # 还没到本周的点：上一个时间点已跑过，就什么都不做
        self.assertIsNone(S.due_slot(SUN - timedelta(minutes=1), last_week))


class Tick(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.status = Path(self.dir.name) / S.STATUS_NAME

    def tearDown(self):
        self.dir.cleanup()

    def tick(self, now, cmd, **kw):
        opts = dict(stop=S._Stop(), at='09:00', weekday=6, catchup_hours=24.0, timeout=30)
        opts.update(kw)
        return S.tick(now, self.status, cmd, **opts)

    def test_success_records_slot_and_does_not_rerun(self):
        ok = [sys.executable, '-c', 'pass']
        self.assertEqual(self.tick(SUN + timedelta(minutes=1), ok), '2026-10-18')
        st = S.read_status(self.status)
        self.assertEqual(st['last_slot'], '2026-10-18')
        self.assertEqual((st['last']['rc'], st['last']['result']), (0, 'done'))
        self.assertIsNone(self.tick(SUN + timedelta(minutes=2), ok))

    def test_failure_is_recorded_and_not_retried(self):
        bad = [sys.executable, '-c', 'raise SystemExit(1)']
        self.tick(SUN + timedelta(minutes=1), bad)
        st = S.read_status(self.status)
        self.assertEqual((st['last']['rc'], st['last']['result']), (1, 'failed'))
        self.assertEqual(st['last_slot'], '2026-10-18')
        self.assertIsNone(self.tick(SUN + timedelta(minutes=2), bad))

    def test_stopped_run_is_not_counted(self):
        # 被发布打断：新容器还在补跑窗口内，应当接着跑这个时间点。
        stop = S._Stop()
        stop.requested = True
        slow = [sys.executable, '-c', 'import time; time.sleep(30)']
        self.tick(SUN + timedelta(minutes=1), slow, stop=stop)
        st = S.read_status(self.status)
        self.assertEqual(st['last']['result'], 'stopped')
        self.assertNotIn('last_slot', st)
        self.assertEqual(self.tick(SUN + timedelta(hours=1), [sys.executable, '-c', 'pass']),
                         '2026-10-18')


class RunOnce(unittest.TestCase):
    def test_timeout_kills_the_child(self):
        t0 = time.monotonic()
        rc, result = S.run_once([sys.executable, '-c', 'import time; time.sleep(30)'],
                                timeout=0.5, stop=S._Stop(), poll=0.1, grace=5)
        self.assertEqual(result, 'timeout')
        self.assertNotEqual(rc, 0)
        self.assertLess(time.monotonic() - t0, 10)

    def test_stop_ignoring_child_is_killed_after_grace(self):
        stop = S._Stop()
        stop.requested = True
        code = ('import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                'print("ready", flush=True); time.sleep(30)')
        t0 = time.monotonic()
        rc, result = S.run_once([sys.executable, '-c', code], timeout=60, stop=stop,
                                poll=0.5, grace=0.5)
        self.assertEqual(result, 'stopped')
        self.assertLess(time.monotonic() - t0, 10)


class PathFixes(unittest.TestCase):
    def test_weekly_report_uses_its_own_interpreter(self):
        # 原先写死 <代码根>/spike-graphiti/.venv：发布产物和 NAS 镜像里都没有。
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KG_HUB_PYTHON', None)
            import tools.weekly_report as W
            W = importlib.reload(W)
            self.assertEqual(W.PYTHON, Path(sys.executable))
        with patch.dict(os.environ, {'KG_HUB_PYTHON': '/opt/py/bin/python'}):
            W = importlib.reload(W)
            self.assertEqual(W.PYTHON, Path('/opt/py/bin/python'))
        importlib.reload(W)

    def test_decisions_log_follows_env(self):
        import tools.decisions_summary as D
        with patch.dict(os.environ, {'KG_HUB_DECISIONS_LOG': '/refinery-state/ingest_decisions.jsonl'}):
            D = importlib.reload(D)
            self.assertEqual(D.DECISIONS_LOG, Path('/refinery-state/ingest_decisions.jsonl'))
        os.environ.pop('KG_HUB_DECISIONS_LOG', None)
        D = importlib.reload(D)
        self.assertEqual(D.DECISIONS_LOG, ROOT / 'data' / '.ingest_decisions.jsonl')


class Wiring(unittest.TestCase):
    def test_compose_service(self):
        import yaml
        svc = yaml.safe_load((ROOT / 'docker-compose.yml').read_text('utf-8'))['services']['weekly_report']
        self.assertEqual(svc['command'], ['python', '-m', 'tools.weekly_report_scheduler'])
        self.assertEqual(svc['container_name'], 'kg-hub-weekly-report')
        root = '${KG_HUB_DATA_ROOT:?set absolute kg-hub data root}'
        self.assertEqual(sorted(svc['volumes']), sorted([
            f'{root}/reports:/root/.kg-hub/reports',
            f'{root}/claude-mem:/root/.claude-mem:ro',
            f'{root}/refinery-state:/refinery-state:ro',
            f'{root}/models:/models:ro']))
        env = dict(e.split('=', 1) for e in svc['environment'])
        self.assertEqual(env['KG_HUB_FALKORDB_HOST'], 'falkordb')
        self.assertEqual(env['KG_HUB_URL'], 'http://kg_hub_server:8080')
        self.assertEqual(env['KG_HUB_DECISIONS_LOG'], '/refinery-state/ingest_decisions.jsonl')
        for key in ('KG_HUB_FALKORDB_PASSWORD', 'KG_HUB_API_TOKEN'):
            self.assertTrue(env[key].startswith('${'), key)          # 只引用变量，不写字面量

    def test_release_and_redeploy_manage_it(self):
        release = (ROOT / 'deploy/nas/release.sh').read_text('utf-8')
        services = release.split('SERVICES="${KG_HUB_SERVICES:-', 1)[1].split('}', 1)[0].split()
        self.assertIn('weekly_report', services)
        self.assertIn("mkdir -p '$DATA/reports'", release)          # 绑定源要预先存在
        redeploy = (ROOT / 'deploy/nas/redeploy.sh').read_text('utf-8')
        self.assertIn("weekly_report) container='kg-hub-weekly-report'", redeploy)
        loop = redeploy.split('  for service in ', 1)[1].split(';', 1)[0].split()
        self.assertIn('weekly_report', loop)
        self.assertEqual(loop[-1], 'watchdog')

    def test_mac_job_is_retired(self):
        self.assertFalse((ROOT / 'deploy/mac/agents/com.kg-hub.weekly-report.plist').exists())


try:
    import graphiti_core  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class SearchOnly(unittest.TestCase):
    """kg_eval 只检索：不该为了构造 Graphiti 去拿网关凭据，更不能发出模型调用。"""

    def test_generation_is_refused(self):
        from graphiti_client import SearchOnlyLLM
        with self.assertRaises(RuntimeError):
            asyncio.run(SearchOnlyLLM()._generate_response([]))

    def test_build_graphiti_skips_gateway_when_given_a_client(self):
        import graphiti_client as GC
        llm = GC.SearchOnlyLLM()
        with patch.object(GC, 'build_llm', side_effect=AssertionError('不应构造网关客户端')), \
             patch.object(GC, 'FalkorDriver') as D, patch.object(GC, 'FastembedEmbedder'), \
             patch.object(GC, 'Graphiti') as G:
            # build_graphiti 建客户端前先查图里向量的来源模型（T-0339）；无标记 = 缺省模型，放行
            D.return_value.execute_query = AsyncMock(return_value=([], None, None))
            G.return_value.build_indices_and_constraints = AsyncMock()
            asyncio.run(GC.build_graphiti(llm_client=llm))
        self.assertIs(G.call_args.kwargs['llm_client'], llm)
        self.assertIn('EmbeddingProfile', D.return_value.execute_query.call_args.args[0])

    def test_kg_eval_builds_search_only(self):
        import tools.kg_eval as K
        self.assertIn('build_graphiti(fresh=False, llm_client=SearchOnlyLLM())',
                      inspect.getsource(K.main_async))


if __name__ == '__main__':
    unittest.main()
