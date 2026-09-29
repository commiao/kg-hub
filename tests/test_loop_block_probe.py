"""Loop stalls must be charged to the call site that blocked, and only to it."""
import asyncio
import pathlib
import subprocess
import time
import unittest

from utils.loop_block_probe import LoopBlockProbe

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _blocking_helper():
    time.sleep(0.6)


def _library_blocking_helper():
    subprocess.run(["sleep", "0.6"], check=True)


class LoopBlockProbeTests(unittest.IsolatedAsyncioTestCase):
    async def _probe(self):
        probe = LoopBlockProbe(threshold=0.1, report_seconds=3600)
        probe.start()
        self.addAsyncCleanup(probe.stop)
        await asyncio.sleep(0.15)
        return probe

    async def test_blocking_call_is_charged_to_its_site(self):
        probe = await self._probe()
        _blocking_helper()
        await asyncio.sleep(0.1)
        stalled, stalls, top = probe.snapshot()
        self.assertEqual(stalls, 1)
        self.assertGreater(stalled, 0.3)
        self.assertIn("_blocking_helper", top[0][0])

    async def test_idle_loop_is_never_charged(self):
        probe = await self._probe()
        await asyncio.sleep(0.6)
        self.assertEqual(probe.snapshot(), (0.0, 0, []))

    async def test_library_leaf_keeps_our_caller(self):
        probe = await self._probe()
        _library_blocking_helper()
        await asyncio.sleep(0.1)
        site = probe.snapshot()[2][0][0]
        self.assertTrue(site.startswith("test_loop_block_probe.py:"), site)
        self.assertIn("_library_blocking_helper <- ", site)
        self.assertIn(" -> ", site)

    async def test_shared_helper_is_charged_with_its_callers(self):
        probe = await self._probe()
        _blocking_helper()
        await asyncio.sleep(0.1)
        site = probe.snapshot()[2][0][0]
        self.assertIn("_blocking_helper <- ", site)
        self.assertIn("test_shared_helper_is_charged_with_its_callers", site)

    async def test_report_logs_and_resets_the_window(self):
        probe = await self._probe()
        _blocking_helper()
        await asyncio.sleep(0.1)
        with self.assertLogs("kg_hub.loop_block", "INFO") as logs:
            probe.report(60)
        self.assertIn("[loop:block] window=60s", logs.output[0])
        self.assertIn("_blocking_helper", logs.output[0])
        self.assertEqual(probe.snapshot(), (0.0, 0, []))

    def test_server_lifespan_starts_and_stops_the_probe(self):
        src = (ROOT / "kg_hub_server.py").read_text(encoding="utf-8")
        body = src.split("async def _application_lifespan(app):", 1)[1].split("\napp = ", 1)[0]
        self.assertIn("start_probe()", body)
        self.assertIn("await stop_probe()", body)


if __name__ == "__main__":
    unittest.main()
