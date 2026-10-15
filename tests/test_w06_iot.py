"""Offline W6 checks: device filter, consumer order of checks, gateway messages, policies, reconciliation.

No AWS and no MQTT broker: paho-mqtt is not needed. The filter and reconciliation tests run the
service on a throwaway local PostgreSQL (tests/local_pg.py) and are skipped without one.
"""
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import local_pg  # noqa: E402
from test_w05_persistence import OPERATOR, REPORTER, VERSION, Running, call, service  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


consumer = load("w06_consumer", "app/consumer.py")
gateway_sim = load("w06_gateway_sim", "tools/gateway_sim.py")
reconcile = load("w06_reconcile", "tests/w06_reconcile.py")
sys.path.insert(0, str(ROOT / "deploy"))
import infra  # noqa: E402

CODE = "g08-m3"
DEVICES = frozenset(f"{CODE}-d{n:02d}" for n in range(1, 11))


def event(event_id, device_id):
    return {"event_id": event_id, "device_id": device_id, "observed_at": "2026-10-13T10:00:00+08:00",
            "type": "status"}


class Consumer(unittest.TestCase):
    def test_unlisted_device_is_rejected_before_the_api(self):
        sent = []
        result = consumer.handle(json.dumps(event("x-d99-01", f"{CODE}-d99")).encode(), DEVICES,
                                 lambda body: sent.append(body) or 201)
        self.assertEqual((result, sent), (("x-d99-01", "rejected"), []))

    def test_api_answers_are_classified(self):
        body = json.dumps(event("x-d01-01", f"{CODE}-d01")).encode()
        for status, expected in ((201, "created"), (200, "duplicate"), (400, "rejected"), (409, "rejected"),
                                 (401, "rejected"), (500, "error"), (503, "error"), (None, "error")):
            with self.subTest(status=status):
                sent = []
                self.assertEqual(consumer.handle(body, DEVICES, lambda b: sent.append(b) or status),
                                 ("x-d01-01", expected))
                self.assertEqual(sent, [body], "the message is forwarded unchanged")

    def test_malformed_messages_are_rejected_and_not_echoed(self):
        for payload in (b"not json", b"[]", b"\xff", json.dumps({"device_id": ["list"]}).encode(),
                        json.dumps(event("bad id\nevent_id=forged", f"{CODE}-d01")).encode()):
            with self.subTest(payload=payload):
                shown, result = consumer.handle(payload, DEVICES, lambda b: 201)
                self.assertNotIn("\n", shown)
                if result != "created":
                    self.assertEqual(result, "rejected")

    def test_unreachable_api_is_error(self):
        self.assertIsNone(consumer.post(b"{}", "t", url="http://127.0.0.1:9/events", timeout=1))

    def test_settings_need_every_value(self):
        env = {name: "v" for name in consumer.SETTINGS}
        env["IOT_DEVICE_IDS"] = f"{CODE}-d01, {CODE}-d02,"
        self.assertEqual(consumer.settings(env)["devices"], {f"{CODE}-d01", f"{CODE}-d02"})
        with self.assertRaises(SystemExit) as caught:
            consumer.settings(dict(env, REPORTER_TOKEN=""))
        self.assertIn("REPORTER_TOKEN", str(caught.exception))


class Gateway(unittest.TestCase):
    def test_32_contract_events(self):
        prefix = gateway_sim.prefix_for(CODE, "r1")
        events = gateway_sim.build_events(CODE, prefix)
        self.assertEqual(len(events), gateway_sim.TOTAL)
        self.assertEqual(events[30], events[0], "the resend is byte-for-byte the first event")
        self.assertEqual((events[31]["event_id"], events[31]["device_id"]), (f"{prefix}-d99-01", f"{CODE}-d99"))
        self.assertEqual(len({e["event_id"] for e in events[:30]}), 30)
        self.assertEqual({e["device_id"] for e in events[:30]}, DEVICES)
        for e in events:
            self.assertEqual(service.validate_event(json.dumps(e).encode()), e)
            self.assertTrue(re.fullmatch(rf"{prefix}-d\d\d-\d\d", e["event_id"]))

    def test_prefix_is_checked(self):
        for code, run in ((CODE, "r1; rm -rf /"), ("G08 m3", "r1"), (CODE, "x1"), ("a" * 30, "r1")):
            with self.subTest(code=code, run=run), self.assertRaises(SystemExit):
                gateway_sim.prefix_for(code, run)


class Policies(unittest.TestCase):
    def test_exact_resources_without_wildcards(self):
        docs = infra.iot_policies("us-east-1", "123456789012", CODE)
        self.assertNotIn("*", json.dumps(docs))
        arn = "arn:aws:iot:us-east-1:123456789012:"
        grants = {role: {(s["Action"], s["Resource"]) for s in doc["Statement"]} for role, doc in docs.items()}
        self.assertEqual(grants["gw"], {("iot:Connect", arn + f"client/gw-{CODE}"),
                                        ("iot:Publish", arn + f"topic/cxe/{CODE}/gw/events")})
        self.assertEqual(grants["consumer"], {("iot:Connect", arn + f"client/consumer-{CODE}"),
                                              ("iot:Subscribe", arn + f"topicfilter/cxe/{CODE}/gw/events"),
                                              ("iot:Receive", arn + f"topic/cxe/{CODE}/gw/events")})

    def test_consumer_settings_point_into_etc_inspection_iot(self):
        env = dict(line.split("=", 1) for line in infra.iot_env(CODE, "abc-ats.iot.us-east-1.amazonaws.com").splitlines())
        self.assertEqual(set(env) | {"REPORTER_TOKEN"}, set(consumer.SETTINGS))
        self.assertEqual(env["IOT_DEVICE_IDS"].split(","), sorted(DEVICES))
        self.assertEqual((env["IOT_TOPIC"], env["IOT_CLIENT_ID"]), (f"cxe/{CODE}/gw/events", f"consumer-{CODE}"))
        for key in ("IOT_CA", "IOT_CERT", "IOT_KEY"):
            self.assertTrue(env[key].startswith("/etc/inspection/iot/"))

    def test_tls_checks_are_never_disabled(self):
        pattern = re.compile(r"tls_insecure_set\(True\)|CERT_" + "NONE")
        for folder in ("app", "tools", "tests"):
            for path in (ROOT / folder).glob("*.py"):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)


@unittest.skipUnless(local_pg.available(), "needs psycopg2 and PostgreSQL server binaries")
class Filter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        version = Path(self.tmp.name) / "version"
        version.write_text(VERSION)
        self.app = Running(local_pg.fresh_store(service), version)

    def tearDown(self):
        self.app.stop()
        self.tmp.cleanup()

    def post_all(self, events):
        for e in events:
            call(self.app.base, "POST", "/events", REPORTER, e)

    def test_device_filter_newest_first(self):
        events = gateway_sim.build_events(CODE, f"{CODE}-r1")
        self.post_all(events[:30])
        status, body = call(self.app.base, "GET", f"/events?device_id={CODE}-d01", OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual([e["event_id"] for e in body["events"]],
                         [f"{CODE}-r1-d01-03", f"{CODE}-r1-d01-02", f"{CODE}-r1-d01-01"])
        self.assertEqual(len(call(self.app.base, "GET", "/events", OPERATOR)[1]["events"]), 30)
        self.assertEqual(call(self.app.base, "GET", f"/events?device_id={CODE}-d42", OPERATOR), (200, {"events": []}))

    def test_filter_checks_role_then_value(self):
        self.assertEqual(call(self.app.base, "GET", f"/events?device_id={CODE}-d01")[0], 401)
        self.assertEqual(call(self.app.base, "GET", f"/events?device_id={CODE}-d01", REPORTER)[0], 403)
        for query in ("device_id=", "device_id=a%27%20OR%201=1", "device_id=a&device_id=b", "device_id=" + "x" * 33):
            with self.subTest(query=query):
                self.assertEqual(call(self.app.base, "GET", "/events?" + query, OPERATOR),
                                 (400, {"error": "invalid_query", "field": "device_id"}))

    def test_page_has_device_box(self):
        import urllib.request
        with urllib.request.urlopen(self.app.base + "/", timeout=5) as response:
            page = response.read().decode()
        self.assertIn('id="device"', page)
        self.assertIn("encodeURIComponent(device)", page)
        self.assertNotIn("innerHTML", page)

    def test_reconcile_script_offline(self):
        prefix = f"{CODE}-r1"
        events = gateway_sim.build_events(CODE, prefix)
        results = [consumer.handle(json.dumps(e).encode(), DEVICES,
                                   lambda body: consumer.post(body, REPORTER, url=self.app.base + "/events"))[1]
                   for e in events]
        counts = {r: results.count(r) for r in reconcile.RESULTS}
        self.assertEqual(counts, {"created": 30, "duplicate": 1, "rejected": 1, "error": 0})
        uniq = "".join(f"{n:7d} result={r}\n" for r, n in counts.items() if n)
        lines = []
        ok = reconcile.run(self.app.base, OPERATOR, CODE, prefix, lambda: reconcile.parse_counts(uniq),
                           lambda: 30, {"prefix": prefix, "puback_ok": 32}, out=lines.append, limit=0)
        self.assertTrue(ok, "\n".join(lines))
        self.assertTrue(all(OPERATOR not in line and REPORTER not in line for line in lines))
        self.assertIn("result: 7/7", lines[-1])


if __name__ == "__main__":
    unittest.main()
