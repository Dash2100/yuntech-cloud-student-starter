"""Offline W5 checks: events survive a restart, resends are idempotent, SQL stays parameterised.

Runs the service against a throwaway local PostgreSQL (tests/local_pg.py); no AWS calls.
"""
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("w05_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
sys.path.insert(0, str(ROOT / "tests"))
import idem_matrix  # noqa: E402
import local_pg  # noqa: E402

REPORTER = "reporter-synthetic-token-for-offline-tests"
OPERATOR = "operator-synthetic-token-for-offline-tests"
VERSION = "c" * 40
GOOD = {"event_id": "g08-m3-9001", "device_id": "g08-d03", "observed_at": "2026-10-06T10:00:00+08:00",
        "type": "test", "note": "合成"}


class Running:
    """One service process stand-in: a server bound to a port, started and stopped explicitly."""

    def __init__(self, store, version, port=0):
        self.server = service.make_server(version, port=port, tokens={"reporter": REPORTER, "operator": OPERATOR},
                                          store=store)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)


def call(base, method, path, token=None, body=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@unittest.skipUnless(local_pg.available(), "needs psycopg2 and PostgreSQL server binaries")
class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.version = Path(self.tmp.name) / "version"
        self.version.write_text(VERSION)
        self.kwargs = local_pg.connect_kwargs()
        self.app = Running(local_pg.fresh_store(service), self.version)

    def tearDown(self):
        self.app.stop()
        self.tmp.cleanup()

    def restart(self):
        """Like systemctl restart: a brand-new process with a brand-new store on the same DB."""
        port = self.app.server.server_port
        self.app.stop()
        time.sleep(1.1)  # started_at has 1-second resolution; the matrix checks that it moved
        self.app = Running(service.DbStore(self.kwargs), self.version, port)
        return "restarted"

    def count(self, event_id):
        import psycopg2
        with psycopg2.connect(**self.kwargs) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM events WHERE event_id = %s", (event_id,))
            n = cur.fetchone()[0]
        conn.close()
        return str(n), "local count"

    def test_idempotency_rules(self):
        status, first = call(self.app.base, "POST", "/events", REPORTER, GOOD)
        self.assertEqual(status, 201)
        status, again = call(self.app.base, "POST", "/events", REPORTER, GOOD)
        self.assertEqual((status, again), (200, first), "identical resend: 200 with the first received_at")
        for changed in (dict(GOOD, note="不同"), {k: v for k, v in GOOD.items() if k != "note"},
                        dict(GOOD, observed_at="2026-10-06T02:00:00Z"), dict(GOOD, type="status")):
            with self.subTest(changed=changed):
                self.assertEqual(call(self.app.base, "POST", "/events", REPORTER, changed),
                                 (409, {"error": "duplicate_event", "field": "event_id"}))
        self.assertEqual(self.count(GOOD["event_id"])[0], "1")
        status, stored = call(self.app.base, "GET", "/events/" + GOOD["event_id"], OPERATOR)
        self.assertEqual(stored, dict(GOOD, received_at=first["received_at"]))

    def test_events_survive_restart(self):
        call(self.app.base, "POST", "/events", REPORTER, GOOD)
        self.restart()
        status, body = call(self.app.base, "GET", "/events", OPERATOR)
        self.assertEqual((status, [e["event_id"] for e in body["events"]]), (200, [GOOD["event_id"]]))
        self.assertEqual(call(self.app.base, "POST", "/events", REPORTER, GOOD)[0], 200)

    def test_concurrent_resends_store_one_row(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            call(self.app.base, "POST", "/events", REPORTER, GOOD)[0])) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200] * 7 + [201])
        self.assertEqual(self.count(GOOD["event_id"])[0], "1")

    def test_hostile_text_is_only_data(self):
        evil = dict(GOOD, event_id="g08-m3-9002", note="x'); DROP TABLE events; --")
        self.assertEqual(call(self.app.base, "POST", "/events", REPORTER, evil)[0], 201)
        status, stored = call(self.app.base, "GET", "/events/g08-m3-9002", OPERATOR)
        self.assertEqual((status, stored["note"]), (200, evil["note"]))

    def test_matrix_script_offline(self):
        lines = []
        ok = idem_matrix.run(self.app.base, REPORTER, OPERATOR, "g08", "m3", 1,
                             self.restart, self.count, out=lines.append)
        self.assertIn("db_configured: True", lines[1])
        self.assertTrue(all(REPORTER not in l and OPERATOR not in l for l in lines))
        self.assertIn("HTTP 201", lines[2])
        self.assertIn("HTTP 200", lines[4])
        self.assertIn("HTTP 409", lines[6])
        self.assertTrue(ok, "\n".join(lines))


class WithoutDatabase(unittest.TestCase):
    def test_starts_and_reports_db_not_configured(self):
        with tempfile.TemporaryDirectory() as td:
            version = Path(td) / "version"
            version.write_text(VERSION)
            app = Running(None, version)
            try:
                status, health = call(app.base, "GET", "/health")
                self.assertEqual((status, health["db_configured"], health["auth_configured"]), (200, False, True))
                self.assertEqual(call(app.base, "POST", "/events", REPORTER, GOOD),
                                 (503, {"error": "db_not_configured", "field": "database"}))
                self.assertEqual(call(app.base, "POST", "/events", None, GOOD)[0], 401, "auth still first")
            finally:
                app.stop()

    def test_from_env_needs_every_setting_and_verifies_tls(self):
        self.assertIsNone(service.DbStore.from_env({"DB_HOST": "h", "DB_NAME": "n", "DB_USER": "u"}))
        if local_pg.available():
            store = service.DbStore.from_env({"DB_HOST": "h", "DB_NAME": "n", "DB_USER": "u", "DB_PASSWORD": "p"})
            self.assertEqual((store._kwargs["sslmode"], store._kwargs["sslrootcert"]),
                             ("verify-full", "/etc/inspection/rds-ca.pem"))

    def test_unreachable_db_is_503_and_secret_free(self):
        if not local_pg.available():
            self.skipTest("needs psycopg2")
        store = service.DbStore({"host": "127.0.0.1", "port": 1, "dbname": "x", "user": "u",
                                 "password": "pw-must-not-leak", "connect_timeout": 2})
        with self.assertRaises(service.Rejected) as caught:
            store.latest()
        self.assertEqual((caught.exception.status, caught.exception.error), (503, "db_unavailable"))

    def test_sql_is_parameterised(self):
        source = (ROOT / "app/service.py").read_text(encoding="utf-8")
        for call_ in re.findall(r"cur\.execute\(([^)]*)", source):
            self.assertNotRegex(call_, r"f\"|f'|%\s*\(|\.format\(", call_)


if __name__ == "__main__":
    unittest.main()
