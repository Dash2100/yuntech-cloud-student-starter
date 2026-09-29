"""Offline W4 event-API contract checks against a local server; no AWS calls."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
spec = importlib.util.spec_from_file_location("w04_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
sys.path.insert(0, str(ROOT / "tests"))
import reject_matrix  # noqa: E402

REPORTER = "reporter-synthetic-token-for-offline-tests"
OPERATOR = "operator-synthetic-token-for-offline-tests"
VERSION = "b" * 40
GOOD = {"event_id": "g08-m1-9001", "device_id": "g08-d01",
        "observed_at": "2026-09-29T10:00:00+08:00", "type": "test"}


def fixtures():
    return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.json"))}


class EventApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        version = Path(self.tmp.name) / "version"
        version.write_text(VERSION)
        self.server = service.make_server(version, port=0, tokens={"reporter": REPORTER, "operator": OPERATOR})
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method, path, token=None, body=None, content_type="application/json", raw=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = token if token.startswith(("Bearer ", "Basic ")) else "Bearer " + token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                text = response.read().decode()
                status, content_type_out = response.status, response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode()
            status, content_type_out = exc.code, exc.headers.get("Content-Type", "")
        self.assertNotIn(REPORTER, text)
        self.assertNotIn(OPERATOR, text)
        body_out = json.loads(text) if content_type_out.startswith("application/json") else text
        if status >= 400:
            self.assertEqual(set(body_out), {"error", "field"}, "error responses carry only error and field")
        return status, body_out

    def rejected(self, expected_status, field, *args, **kwargs):
        status, body = self.call(*args, **kwargs)
        self.assertEqual(status, expected_status, body)
        self.assertEqual(body["field"], field, body)
        return body

    # ---- fixtures ------------------------------------------------------
    def test_fixtures_are_read_and_behave_as_declared(self):
        found = fixtures()
        statuses = [f["expect"]["status"] for f in found.values()]
        self.assertGreaterEqual(statuses.count(201), 1, "need at least one accepted fixture")
        self.assertGreaterEqual(sum(s == 400 for s in statuses), 2, "need at least two rejected fixtures")
        for name, fixture in found.items():
            with self.subTest(fixture=name):
                status, body = self.call("POST", "/events", REPORTER, fixture["event"])
                self.assertEqual(status, fixture["expect"]["status"], body)
                for key in ("error", "field"):
                    if key in fixture["expect"]:
                        self.assertEqual(body[key], fixture["expect"][key])
                if status == 201:
                    self.assertEqual(body["event_id"], fixture["event"]["event_id"])
                    self.assertTrue(body["received_at"].endswith("Z"))

    # ---- the 7-row matrix (same code the cloud run uses) ----------------
    def test_rejection_matrix_all_seven_rows(self):
        lines = []
        self.assertTrue(reject_matrix.run(self.base, REPORTER, OPERATOR, "g08", "m1", 1, out=lines.append),
                        "\n".join(lines))
        self.assertIn("version: " + VERSION, lines[1])
        self.assertTrue(all(REPORTER not in line and OPERATOR not in line for line in lines))

    # ---- order of checks: 401 -> 403 -> 400 -> 409 -> 201 ---------------
    def test_identity_is_checked_before_content(self):
        bad = dict(GOOD, observed_at="yesterday", extra=1)
        self.rejected(401, "authorization", "POST", "/events", None, bad)
        self.rejected(401, "authorization", "POST", "/events", "wrong-token", bad)
        self.rejected(401, "authorization", "POST", "/events", "Basic " + REPORTER, bad)
        self.rejected(401, "authorization", "POST", "/events", None, raw=b"not json", content_type="text/plain")
        self.rejected(403, "role", "POST", "/events", OPERATOR, bad)

    def test_validation_names_the_field(self):
        cases = [
            (dict(GOOD, extra="x"), "unknown_field", "extra"),
            ({k: v for k, v in GOOD.items() if k != "device_id"}, "missing_field", "device_id"),
            (dict(GOOD, event_id="has space"), "invalid_field", "event_id"),
            (dict(GOOD, event_id="x" * 65), "invalid_field", "event_id"),
            (dict(GOOD, device_id="d" * 33), "invalid_field", "device_id"),
            (dict(GOOD, observed_at="2026-09-29 10:00:00+08:00"), "invalid_field", "observed_at"),
            (dict(GOOD, observed_at="2026-02-30T10:00:00+08:00"), "invalid_field", "observed_at"),
            (dict(GOOD, observed_at=1790000000), "invalid_field", "observed_at"),
            (dict(GOOD, type="ANOMALY"), "invalid_field", "type"),
            (dict(GOOD, note=None), "invalid_field", "note"),
            (dict(GOOD, note="n" * 201), "invalid_field", "note"),
        ]
        for event, error, field in cases:
            with self.subTest(field=field, error=error):
                body = self.rejected(400, field, "POST", "/events", REPORTER, event)
                self.assertEqual(body["error"], error)
        self.rejected(400, "body", "POST", "/events", REPORTER, raw=b"[1, 2]")
        self.rejected(400, "body", "POST", "/events", REPORTER, raw=b'{"event_id": "a", "event_id": "b"}')
        self.rejected(400, "content-type", "POST", "/events", REPORTER, GOOD, content_type="text/plain")
        self.rejected(400, "body", "POST", "/events", REPORTER, dict(GOOD, note="x" * 4200))

    def test_accepts_boundaries(self):
        for i, event in enumerate([dict(GOOD, observed_at="2026-09-29T02:00:00Z"),
                                   dict(GOOD, observed_at="2026-09-29T10:00:00.5+08:00"),
                                   dict(GOOD, note="n" * 200, event_id="x" * 64),
                                   dict(GOOD, device_id="d" * 32, type="status")]):
            with self.subTest(i=i):
                status, body = self.call("POST", "/events", REPORTER, dict(event, event_id=event["event_id"][:60] + str(i)))
                self.assertEqual(status, 201, body)
        status, _ = self.call("POST", "/events", REPORTER, GOOD, content_type="application/json; charset=utf-8")
        self.assertEqual(status, 201)

    def test_duplicate_is_409_and_first_copy_kept(self):
        self.assertEqual(self.call("POST", "/events", REPORTER, dict(GOOD, note="first"))[0], 201)
        self.rejected(409, "event_id", "POST", "/events", REPORTER, dict(GOOD, note="second"))
        status, event = self.call("GET", "/events/" + GOOD["event_id"], OPERATOR)
        self.assertEqual((status, event["note"]), (200, "first"))

    # ---- reads -----------------------------------------------------------
    def test_reads_are_operator_only(self):
        self.call("POST", "/events", REPORTER, GOOD)
        self.rejected(401, "authorization", "GET", "/events")
        self.rejected(403, "role", "GET", "/events", REPORTER)
        self.rejected(401, "authorization", "GET", "/events/" + GOOD["event_id"])
        self.rejected(403, "role", "GET", "/events/" + GOOD["event_id"], REPORTER)
        self.rejected(404, "event_id", "GET", "/events/g08-m1-none", OPERATOR)
        status, event = self.call("GET", "/events/" + GOOD["event_id"], OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual(set(event), set(GOOD) | {"received_at"})

    def test_list_returns_latest_50_newest_first(self):
        for i in range(55):
            self.call("POST", "/events", REPORTER, dict(GOOD, event_id=f"g08-m1-{i:04d}"))
        status, body = self.call("GET", "/events", OPERATOR)
        ids = [e["event_id"] for e in body["events"]]
        self.assertEqual((status, len(ids), ids[0], ids[-1]), (200, 50, "g08-m1-0054", "g08-m1-0005"))

    def test_methods_and_paths(self):
        self.rejected(405, "method", "PUT", "/events", REPORTER, GOOD)
        self.rejected(405, "method", "POST", "/health", REPORTER, GOOD)
        self.rejected(404, "path", "GET", "/nope")

    # ---- health and page ---------------------------------------------------
    def test_health_reports_auth_configured(self):
        status, health = self.call("GET", "/health")
        self.assertEqual((status, health["version"], health["auth_configured"]), (200, VERSION, True))
        with tempfile.TemporaryDirectory() as td:
            version = Path(td) / "version"
            version.write_text(VERSION)
            server = service.make_server(version, port=0, tokens={"reporter": "", "operator": ""})
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_port)
                with urllib.request.urlopen(base + "/health") as response:
                    self.assertIs(json.load(response)["auth_configured"], False)
                req = urllib.request.Request(base + "/events", headers={"Authorization": "Bearer "})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req)
                self.assertEqual(caught.exception.code, 401, "unconfigured tokens fail closed")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_display_page_is_safe(self):
        with urllib.request.urlopen(self.base + "/") as response:
            page = response.read().decode()
            csp = response.headers["Content-Security-Policy"]
        self.assertIn("textContent", page)
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                          "localStorage", "sessionStorage", "document.cookie", "?token=", "<form"):
            self.assertNotIn(forbidden, page)
        self.assertIn("script-src 'nonce-", csp)
        self.assertIn("connect-src 'self'", csp)


if __name__ == "__main__":
    unittest.main()
