"""HTTP/MCP authorization, input and stale-evidence regressions, offline only."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from moonshot_scanner.app import create_app, public_snapshot
from moonshot_scanner.config import Settings


class OfflineScanner:
    def __init__(self):
        self.calls = []

    def execute_scan(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("Tests must not launch the scanner or access the network")


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="moonshot-app-test-")
        self.env = patch.dict(os.environ, {
            "SCANNER_COOKIE_SECURE": "0", "SCANNER_TRUSTED_HOSTS": "localhost",
            "SCANNER_ALLOWED_ORIGINS": "", "SCANNER_SESSION_SECRET": "synthetic-test-session-secret",
        })
        self.env.start()
        self.settings = Settings(state_dir=Path(self.tmp.name), token="synthetic-admin-token",
                                 read_token="synthetic-read-token", persistent_storage=False,
                                 scan_interval_seconds=300, auto_scan=False, session_reports=False,
                                 exchanges=("bitget",), auto_limit=2, bot_token="", chat_id="", webhook_url="")
        self.scanner = OfflineScanner()
        self.app = create_app(self.settings, scanner=self.scanner, start_worker=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["scanner_store"]
        self.admin = {"Authorization": "Bearer synthetic-admin-token"}
        self.read = {"Authorization": "Bearer synthetic-read-token"}

    def tearDown(self):
        self.assertEqual(self.scanner.calls, [])
        self.env.stop()
        self.tmp.cleanup()

    def login(self):
        response = self.client.post("/api/login", json={"token": self.settings.token},
                                    headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["csrf"]

    def rpc(self, method, params=None, **kwargs):
        return self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 7,
                                              "method": method, "params": params or {}},
                                headers=kwargs.pop("headers", self.read), **kwargs)

    def snapshot(self, *, expired=False, plan=None):
        now = int(time.time() * 1000)
        plan = copy.deepcopy(plan or {
            "side": "LONG", "setup_type": "PULLBACK", "status": "CONDITIONAL",
            "builder_status": "CONDITIONAL", "execution_status": "READY",
            "entry_low": 99, "entry_high": 100, "entry_mid": 99.5, "stop": 90,
            "targets": [130, 150], "tp1": 130, "tp2": 150, "fees_bps": 12,
            "slippage_bps": 10, "expires_at_ms": now + 3_600_000,
            "decision": {"status": "READY", "ready": True, "waiting_for": [], "probability": None},
        })
        return {"schema_version": 3, "exchange": "bitget", "symbol": "EVAAUSDT",
                "status": "READY", "trade_ready": True, "early_discovery": True,
                "analyzed_at_ms": now - 1000, "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "valid_until_ms": now - 1 if expired else now + 90_000,
                "market": {"price": 100, "ticker_observed_at_ms": now - 1000,
                           "product": "SPOT", "candles": {"4h": [{"is_closed": True}]}},
                "plans": [plan]}

    def save_snapshot(self, data):
        job = self.store.enqueue("scan", {"symbols": [data["symbol"]]})
        self.store.add_snapshot(job, data)
        return job


class AuthenticationTests(AppCase):
    def test_every_research_route_requires_authentication(self):
        for path in ("/api/bootstrap", "/api/health", "/api/feed/latest", "/api/session",
                     "/api/market", "/api/candles", "/api/watch", "/api/learn/stats",
                     "/api/evidence/EVAAUSDT", "/api/rose/recent", "/api/scans/unknown", "/readyz"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post("/api/scan", json={}).status_code, 401)
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)

    def test_login_is_public_and_issues_session_csrf(self):
        csrf = self.login()
        self.assertTrue(csrf)
        bootstrap = self.client.get("/api/bootstrap").get_json()
        self.assertEqual(bootstrap["scope"], "admin")
        self.assertEqual(bootstrap["csrf"], csrf)
        response = self.client.post("/api/scan", json={"symbols": ["EVAAUSDT"]},
                                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 202)

    def test_incorrect_login_never_authenticates(self):
        response = self.client.post("/api/login", json={"token": "incorrect"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/api/bootstrap").status_code, 401)

    def test_cross_origin_login_is_rejected(self):
        response = self.client.post("/api/login", json={"token": self.settings.token},
                                    headers={"Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)

    def test_session_mutations_require_correct_csrf(self):
        self.login()
        for headers in ({}, {"X-CSRF-Token": "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/api/scan", json={}, headers=headers).status_code, 403)

    def test_query_token_does_not_bypass_mutation_csrf(self):
        response = self.client.post("/api/scan?token=" + self.settings.token, json={})
        self.assertEqual(response.status_code, 403)
        self.login()
        self.assertEqual(self.client.post("/api/scan?token=" + self.settings.token, json={}).status_code, 403)

    def test_bogus_bearer_cannot_bypass_session_csrf(self):
        self.login()
        response = self.client.post("/api/scan", json={},
                                    headers={"Authorization": "Bearer incorrect-token"})
        self.assertIn(response.status_code, (401, 403))

    def test_read_token_can_read_but_cannot_mutate(self):
        self.assertEqual(self.client.get("/api/feed/latest", headers=self.read).status_code, 200)
        self.assertEqual(self.client.get("/api/bootstrap", headers=self.read).get_json()["scope"], "read")
        for path in ("/api/scan", "/api/evidence", "/api/watch", "/api/backtest/replay"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}, headers=self.read).status_code, 403)
        self.assertEqual(self.client.delete("/api/watch/any", headers=self.read).status_code, 403)

    def test_read_token_cannot_inherit_session_admin_for_mutation(self):
        csrf = self.login()
        headers = {**self.read, "X-CSRF-Token": csrf}
        self.assertEqual(self.client.post("/api/scan", json={}, headers=headers).status_code, 403)

    def test_origin_is_checked_even_for_valid_read_bearer(self):
        response = self.client.get("/api/feed/latest", headers={**self.read, "Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)

    def test_security_headers_prohibit_caching_embedding_and_referrer_leakage(self):
        response = self.client.get("/api/feed/latest", headers=self.read)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


class InputValidationTests(AppCase):
    def test_non_object_json_bodies_return_validation_errors(self):
        for path in ("/api/login", "/api/scan", "/api/watch", "/api/backtest/replay", "/api/evidence"):
            with self.subTest(path=path):
                response = self.client.post(path, json=[1], headers=self.admin)
                self.assertEqual(response.status_code, 400)

    def test_non_object_replay_items_return_validation_errors(self):
        for data in ({"candles": ["bad"]}, {"candles": [], "signals": ["bad"]}):
            with self.subTest(data=data):
                response = self.client.post("/api/backtest/replay", json=data, headers=self.admin)
                self.assertEqual(response.status_code, 400)

    def test_valid_admin_scan_enqueues_without_running_network(self):
        response = self.client.post("/api/scan", headers=self.admin,
                                    json={"symbols": "EVAAUSDT,SOLUSDT", "risk_pct": .5})
        self.assertEqual(response.status_code, 202)
        job = self.store.job(response.get_json()["job_id"])
        self.assertEqual(job["status"], "PENDING")
        self.assertEqual(job["payload"]["risk_pct"], .5)
        self.assertEqual(job["payload"]["symbols"], ["EVAAUSDT", "SOLUSDT"])

    def test_nonfinite_numeric_inputs_are_rejected(self):
        for field in ("risk_pct", "account_usd", "limit", "min_turnover", "max_turnover"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value):
                    response = self.client.post("/api/scan", json={field: value}, headers=self.admin)
                    self.assertEqual(response.status_code, 400)

    def test_boolean_numeric_inputs_are_rejected(self):
        for field in ("risk_pct", "account_usd", "limit", "min_turnover", "max_turnover"):
            with self.subTest(field=field):
                response = self.client.post("/api/scan", json={field: True}, headers=self.admin)
                self.assertEqual(response.status_code, 400)

    def test_invalid_risk_bounds_are_rejected(self):
        for risk in (0, -1, 1.001, None, "not-a-number"):
            with self.subTest(risk=risk):
                self.assertEqual(self.client.post("/api/scan", json={"risk_pct": risk}, headers=self.admin).status_code, 400)

    def test_watch_requires_existing_valid_stored_plan(self):
        data = {"exchange": "bitget", "symbol": "EVAAUSDT", "setup_type": "PULLBACK"}
        self.assertEqual(self.client.post("/api/watch", json=data, headers=self.admin).status_code, 400)
        row = self.snapshot()
        row["plans"][0].update(side="SHORT")  # long geometry cannot be a short plan
        self.save_snapshot(row)
        self.assertEqual(self.client.post("/api/watch", json=data, headers=self.admin).status_code, 400)
        self.assertEqual(self.store.watches(), [])

    def test_expired_plan_cannot_be_added_to_watch(self):
        row = self.snapshot()
        row["plans"][0]["expires_at_ms"] = int(time.time() * 1000) - 1000
        self.save_snapshot(row)
        response = self.client.post("/api/watch", json={"exchange": "bitget", "symbol": "EVAAUSDT", "setup_type": "PULLBACK"}, headers=self.admin)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.watches(), [])

    def test_client_cannot_replace_server_side_watch_levels(self):
        row = self.snapshot()
        self.save_snapshot(row)
        response = self.client.post("/api/watch", json={"exchange": "bitget", "symbol": "EVAAUSDT",
                                    "setup_type": "PULLBACK", "stop": 1, "targets": [999]}, headers=self.admin)
        self.assertEqual(response.status_code, 201)
        watched = self.store.watches()[0]["plan"]
        self.assertEqual(watched["stop"], 90)
        self.assertEqual(watched["targets"], [130, 150])


class SnapshotAndHealthTests(AppCase):
    def test_stale_ready_downgrades_row_and_nested_execution_without_mutating_archive(self):
        row = self.snapshot(expired=True)
        saved = copy.deepcopy(row)
        result = public_snapshot(row)
        self.assertEqual(result["recorded_status"], "READY")
        self.assertEqual(result["status"], "WATCH")
        self.assertFalse(result["trade_ready"])
        self.assertFalse(result["fresh_for_entry"])
        plan = result["plans"][0]
        self.assertEqual(plan["execution_status"], "WAIT")
        self.assertEqual(plan["decision"]["status"], "WAIT")
        self.assertFalse(plan["decision"]["ready"])
        self.assertIsNone(plan["decision"]["probability"])
        self.assertEqual(row, saved)

    def test_saved_feed_and_scan_status_cannot_resurrect_stale_ready(self):
        job_id = self.save_snapshot(self.snapshot(expired=True))
        feed = self.client.get("/api/feed/latest", headers=self.read).get_json()["results"][0]
        job = self.client.get("/api/scans/" + job_id, headers=self.read).get_json()["results"][0]
        self.assertFalse(feed["trade_ready"])
        self.assertFalse(job["trade_ready"])
        self.assertEqual(feed["plans"][0]["decision"]["status"], "WAIT")

    def test_default_feed_excludes_candles_but_explicit_snapshot_can_keep_them(self):
        row = self.snapshot()
        self.assertNotIn("candles", public_snapshot(row)["market"])
        self.assertIn("candles", public_snapshot(row, include_candles=True)["market"])

    def test_liveness_is_public_and_explicitly_not_watcher_health(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "WEB_RUNNING")
        self.assertTrue(response.get_json()["not_a_watcher_healthcheck"])
        health = self.client.get("/api/health", headers=self.read).get_json()
        self.assertFalse(health["worker"]["healthy"])
        self.assertFalse(health["ready_for_24_7"])
        self.assertEqual(health["rose"]["status"], "NOT_CONNECTED")

    def test_readyz_is_unavailable_before_worker_started(self):
        response = self.client.get("/readyz", headers=self.read)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["worker"]["healthy"])


class ReadOnlyMCPTests(AppCase):
    def test_read_token_initializes_and_lists_only_research_tools(self):
        response = self.rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "offline-test", "version": "1"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"]["serverInfo"]["name"], "moonshot-research-readonly")
        self.assertEqual(response.get_json()["result"]["protocolVersion"], "2025-06-18")
        tools = self.rpc("tools/list").get_json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"market_latest", "scanner_health", "session_brief", "rose_recent"})
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        self.assertTrue(all(t["annotations"]["destructiveHint"] is False for t in tools))

    def test_tools_call_returns_evidence_with_stale_entry_downgraded(self):
        self.save_snapshot(self.snapshot(expired=True))
        response = self.rpc("tools/call", {"name": "market_latest", "arguments": {}})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["result"]
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertFalse(data["results"][0]["trade_ready"])
        self.assertEqual(data["results"][0]["plans"][0]["decision"]["status"], "WAIT")

    def test_trading_and_unknown_tools_are_not_exposed(self):
        for name in ("place_order", "transfer_funds", "open_position", "delete_watch"):
            with self.subTest(name=name):
                body = self.rpc("tools/call", {"name": name, "arguments": {}}).get_json()
                self.assertEqual(body["error"]["code"], -32602)

    def test_tool_arguments_cannot_mutate_or_request_unknown_fields(self):
        response = self.rpc("tools/call", {"name": "market_latest", "arguments": {"execute": True}})
        self.assertEqual(response.get_json()["error"]["code"], -32602)

    def test_malformed_rpc_and_params_return_protocol_errors(self):
        for body, expected in (([], -32600), ({"jsonrpc": "1.0", "id": 1}, -32600),
                               ({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []}, -32602)):
            with self.subTest(body=body):
                response = self.client.post("/mcp", json=body, headers=self.read)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"]["code"], expected)
        malformed = self.client.post("/mcp", data="{not-json", content_type="application/json", headers=self.read)
        self.assertEqual(malformed.status_code, 400)
        self.assertIn(malformed.get_json()["error"]["code"], (-32600, -32700))

    def test_unhashable_tool_names_return_error_instead_of_server_crash(self):
        for name in ([], {}):
            with self.subTest(name=name):
                response = self.rpc("tools/call", {"name": name, "arguments": {}})
                self.assertLess(response.status_code, 500)
                self.assertEqual(response.get_json()["error"]["code"], -32602)

    def test_invalid_origin_rejected_for_mcp(self):
        response = self.rpc("tools/list", headers={**self.read, "Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)

    def test_initialized_notification_and_protocol_version_are_checked(self):
        response = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=self.read)
        self.assertEqual(response.status_code, 202)
        response = self.rpc("tools/list", headers={**self.read, "MCP-Protocol-Version": "unsupported-version"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/mcp", headers=self.read).status_code, 405)


if __name__ == "__main__":
    unittest.main()
