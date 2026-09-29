"""The writer generation proves whether any holder came between two readings."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import writer_lock as wl


class WriterGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.multiple(wl, LOCK_DIR=Path(temp.name),
                                 LOCK_FILE=Path(temp.name) / "writer.lock")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_async_holder_is_odd_and_release_is_even(self):
        self.assertEqual(wl.read_generation(), 0)
        async with wl.async_writer_lock(owner="a"):
            self.assertEqual(wl.read_generation(), 1)
        self.assertEqual(wl.read_generation(), 2)

    def test_sync_holder_advances_the_same_generation(self):
        with wl.writer_lock(owner="tool"):
            self.assertEqual(wl.read_generation(), 1)
        self.assertEqual(wl.read_generation(), 2)

    async def test_any_intervening_holder_breaks_the_single_step(self):
        before = wl.read_generation()
        with wl.writer_lock(owner="other"):
            pass
        async with wl.async_writer_lock(owner="me"):
            self.assertNotEqual(wl.read_generation(), before + 1)

    async def test_holder_that_died_while_holding_stays_detectable(self):
        (wl.LOCK_DIR / "writer.gen").write_text("7")
        async with wl.async_writer_lock(owner="next"):
            self.assertEqual(wl.read_generation(), 9)
        self.assertEqual(wl.read_generation(), 10)

    async def test_error_inside_the_lock_still_releases_the_generation(self):
        with self.assertRaises(RuntimeError):
            async with wl.async_writer_lock(owner="a"):
                raise RuntimeError("boom")
        self.assertEqual(wl.read_generation(), 2)


if __name__ == "__main__":
    unittest.main()
