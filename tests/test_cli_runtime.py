import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from utils import cli_runtime as rt


class RuntimeTest(unittest.TestCase):
    def test_proxy_precedence_redaction_and_no_global_mutation(self):
        source = {"HTTPS_PROXY": "http://alice:SECRET@proxy.example:8123/private?token=SECRET"}
        before = source.copy()
        env, route = rt.cli_environment(environ=source, platform="darwin", discover=lambda: self.fail("must not discover"))
        self.assertEqual(env, before)
        self.assertEqual(source, before)
        self.assertEqual(route["proxies"]["HTTPS_PROXY"], "http://proxy.example:8123")
        self.assertNotIn("SECRET", json.dumps(route))
        direct, _ = rt.cli_environment(environ=source, proxy_mode="none")
        self.assertNotIn("HTTPS_PROXY", direct)
        self.assertEqual(source, before)

    def test_empty_scutil_is_unverified_and_linux_does_not_discover(self):
        empty = mock.Mock(stdout="<dictionary> { }", returncode=0)
        self.assertEqual(rt.system_proxies(lambda *a, **k: empty)["status"], "system_proxy_unverified")
        _, route = rt.cli_environment(environ={}, platform="linux", discover=lambda: self.fail("no scutil on Linux"))
        self.assertEqual(route["readiness"], "not_probed")

    def test_system_proxy_is_subprocess_only(self):
        initial = {}
        env, route = rt.cli_environment(environ=initial, platform="darwin", discover=lambda: {
            "status": "system_proxy_configured", "values": {"HTTPS_PROXY": "http://127.0.0.1:4567"}})
        self.assertEqual(initial, {})
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:4567")
        self.assertEqual(route["route_source"], "system_proxy_configured")

    def test_nonce_identity_and_receipt_validation_fail_closed(self):
        def respond(system, query):
            return {"text": query, "error": None, "actual_models": ["m"], "credits": None, "tokens": None}
        ready = rt.probe(respond, executor="claude", model="m", environ={}, proxy_mode="env-only")
        rt.validate_probe_receipt(ready, executor="claude", model="m", environ={}, proxy_mode="env-only")
        with self.assertRaisesRegex(ValueError, "expired"):
            rt.validate_probe_receipt(ready, executor="claude", model="m", environ={}, proxy_mode="env-only",
                                      now=datetime.now(timezone.utc) + timedelta(minutes=16))
        with self.assertRaisesRegex(ValueError, "configuration differs"):
            rt.validate_probe_receipt(ready, executor="claude", model="m", proxy_mode="env-only",
                                      environ={"HTTPS_PROXY": "http://changed:1234"})
        for response in ({"text": "OK"}, {"text": ready["challenge"], "actual_models": ["other"]}, {"text": ""}):
            bad = rt.probe(lambda *a: response, executor="claude", model="m", environ={}, proxy_mode="env-only")
            self.assertEqual(bad["status"], "infrastructure_failed")
        broken = copy.deepcopy(ready)
        broken["challenge"] = '{}'
        broken["receipt_sha256"] = rt._digest({k: v for k, v in broken.items() if k != "receipt_sha256"})
        with self.assertRaisesRegex(ValueError, "challenge"):
            rt.validate_probe_integrity(broken, executor="claude", model="m")
        self.assertFalse(ready["cost"]["credits_known"])
        self.assertFalse(ready["cost"]["tokens_known"])

    def test_status_is_read_from_errors_not_generated_text(self):
        self.assertFalse(rt.classify_failure({"text": "HTTP 403", "error": None})["terminal"])
        self.assertEqual(rt.classify_failure({"error": "API Error: 403 denied"})["kind"], "auth")
        self.assertEqual(rt.classify_failure({"error": "bad", "status_code": 503})["kind"], "service")
        self.assertEqual(rt.error_status({"error": {"status_code": 429}}), 429)
        self.assertIsNone(rt.error_status({"result": {"status_code": 403}}))


if __name__ == "__main__":
    unittest.main()
