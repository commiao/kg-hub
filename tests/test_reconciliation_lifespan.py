"""Use the installed Starlette lifecycle API without starting business work."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import kg_hub_server as server


class ReconciliationLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_lifespan_starts_and_stops_mailbox(self):
        with patch.object(server, "_start_reconciliation_mailbox", AsyncMock()) as start, \
                patch.object(server, "_stop_reconciliation_mailbox", AsyncMock()) as stop:
            async with server.app.router.lifespan_context(server.app):
                start.assert_awaited_once()
                stop.assert_not_awaited()
            stop.assert_awaited_once()

    async def test_failed_application_scope_still_stops_mailbox(self):
        with patch.object(server, "_start_reconciliation_mailbox", AsyncMock()), \
                patch.object(server, "_stop_reconciliation_mailbox", AsyncMock()) as stop:
            with self.assertRaises(RuntimeError):
                async with server.app.router.lifespan_context(server.app):
                    raise RuntimeError("fixture")
            stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
