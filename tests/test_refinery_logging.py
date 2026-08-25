"""Regression coverage for refinery logging under a blocked stderr sink."""
from __future__ import annotations

import json
import logging
import queue
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

import kg_refinery as refinery


class RefineryLoggingTests(unittest.TestCase):
    def test_full_log_queue_drops_without_blocking_the_caller(self):
        """A blocked listener must not hold the asyncio heartbeat producer."""
        log_queue: queue.Queue = queue.Queue(maxsize=1)
        handler = refinery.DroppingQueueHandler(log_queue)
        record = logging.LogRecord("refinery", logging.INFO, __file__, 0, "first", (), None)
        handler.emit(record)

        start = time.monotonic()
        handler.emit(record)

        self.assertLess(time.monotonic() - start, 0.1)
        self.assertEqual(log_queue.qsize(), 1)
        self.assertEqual(handler.dropped, 1)
        self.assertIsNotNone(handler.last_drop_at)

    def test_queue_and_format_errors_drop_without_handle_error_fallback(self):
        handler = refinery.DroppingQueueHandler(queue.Queue(maxsize=1))
        record = logging.LogRecord("refinery", logging.INFO, __file__, 0, "first", (), None)
        with patch.object(handler, "prepare", side_effect=ValueError("bad format")), \
                patch.object(handler, "handleError") as handle_error:
            handler.emit(record)
        self.assertEqual(handler.dropped, 1)
        handle_error.assert_not_called()

        broken_queue = Mock()
        broken_queue.put_nowait.side_effect = RuntimeError("queue unavailable")
        broken = refinery.DroppingQueueHandler(broken_queue)
        with patch.object(broken, "handleError") as handle_error:
            broken.emit(record)
        self.assertEqual(broken.dropped, 1)
        handle_error.assert_not_called()
        self.assertFalse(logging.raiseExceptions)

    def test_listener_stops_cleanly_when_the_sink_is_available(self):
        listener = refinery.NonBlockingQueueListener(queue.Queue(), logging.NullHandler())
        listener.start()
        listener.stop(timeout=0.2)
        self.assertIsNone(listener._thread)

    def test_status_persists_cumulative_log_drops_without_double_counting(self):
        record = logging.LogRecord("refinery", logging.INFO, __file__, 0, "first", (), None)
        first = refinery.DroppingQueueHandler(queue.Queue(maxsize=1))
        first.emit(record)
        first.emit(record)
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            status = state_dir / "status.json"
            with patch.object(refinery, "STATE_DIR", state_dir), \
                    patch.object(refinery, "STATUS", status), \
                    patch.object(refinery, "REFINERY_LOG_HANDLER", first):
                refinery.write_status(test="first")
                one_drop = json.loads(status.read_text())
                refinery.write_status(heartbeat_only=True)
                unchanged = json.loads(status.read_text())

            second = refinery.DroppingQueueHandler(queue.Queue(maxsize=1))
            second.emit(record)
            second.emit(record)
            with patch.object(refinery, "STATE_DIR", state_dir), \
                    patch.object(refinery, "STATUS", status), \
                    patch.object(refinery, "REFINERY_LOG_HANDLER", second):
                refinery.write_status(test="second")
                two_drops = json.loads(status.read_text())

        self.assertEqual(one_drop["log_dropped_total"], 1)
        self.assertIsInstance(one_drop["log_last_drop_at"], str)
        self.assertEqual(unchanged["log_dropped_total"], 1)
        self.assertEqual(two_drops["log_dropped_total"], 2)

    def test_refinery_logger_uses_a_bounded_queue_handler(self):
        self.assertFalse(refinery.log.propagate)
        self.assertIs(refinery.log.handlers[0], refinery.REFINERY_LOG_HANDLER)
        self.assertIsInstance(refinery.REFINERY_LOG_HANDLER, refinery.DroppingQueueHandler)
        self.assertEqual(refinery.REFINERY_LOG_HANDLER.queue.maxsize,
                         refinery.REFINERY_LOG_QUEUE_SIZE)
        self.assertIsInstance(refinery.REFINERY_LOG_LISTENER,
                              logging.handlers.QueueListener)


if __name__ == "__main__":
    unittest.main()
