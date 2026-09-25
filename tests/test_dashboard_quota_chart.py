"""Regression tests for the kg-hub -> NAS gateway usage bridge."""
from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "kg_hub_server.py").read_text("utf-8")


class GatewayUsageBridgeTests(unittest.TestCase):
    def test_portal_has_no_second_model_usage_module(self):
        portal = SERVER.split("PORTAL_REPORTS =", 1)[1].split("]\n", 1)[0]
        self.assertNotIn("模型用量与成本", portal)
        self.assertNotIn("/dashboard/gateway_usage", portal)

    def test_usage_is_data_only_api_for_the_nas_dashboard(self):
        self.assertIn('Route("/api/gateway-usage", gateway_usage_snapshot', SERVER)
        self.assertNotIn('Route("/dashboard/gateway_usage",', SERVER)
        handler = SERVER.split("async def gateway_usage_snapshot", 1)[1].split(
            "\nasync def ", 1
        )[0]
        self.assertIn("return JSONResponse", handler)
        self.assertNotIn("HTMLResponse(_DASH_GATEWAY_USAGE_HTML", handler)

    def test_api_keeps_historical_calls_and_observations_distinct(self):
        handler = SERVER.split("async def gateway_usage_snapshot", 1)[1].split(
            "\nasync def ", 1
        )[0]
        for field in ('"limits"', '"budget_hourly"', '"budget_day"'):
            self.assertIn(field, handler)
        self.assertIn("effective_limits", handler)
        self.assertIn('budget = rstatus.get("budget_today")', handler)


if __name__ == "__main__":
    unittest.main()
