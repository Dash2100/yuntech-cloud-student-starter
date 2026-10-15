#!/usr/bin/env python3
"""W6 consumer: AWS IoT Core (MQTT 5, QoS 1, mutual TLS on 8883) -> device list -> the W5 API.

The gateway certificate only proves WHICH gateway sent a message; device_id is a claim
inside the message. So every message is checked against IOT_DEVICE_IDS first: a device
that is not on the list is counted as rejected and never reaches the API. Listed devices
are POSTed with the reporter token to http://127.0.0.1:8080/events and the answer is
classified: 201 created, 200 duplicate (W5 idempotent resend), other 4xx rejected,
5xx or no answer error. The consumer never writes the database itself.

Settings come from the environment (systemd EnvironmentFile=/etc/inspection/app.env):
IOT_ENDPOINT, IOT_TOPIC, IOT_CLIENT_ID, IOT_DEVICE_IDS, IOT_CA, IOT_CERT, IOT_KEY,
REPORTER_TOKEN. One log line per message: `event_id=<id> result=<result>`; the token
and the message body are never logged. TLS always verifies the broker against IOT_CA.
Must stay compatible with the AL2023 system Python (3.9); paho-mqtt 2.1.0 is vendored
in /opt/inspection/vendor by the packer.
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

API = "http://127.0.0.1:8080/events"
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SETTINGS = ("IOT_ENDPOINT", "IOT_TOPIC", "IOT_CLIENT_ID", "IOT_DEVICE_IDS", "IOT_CA", "IOT_CERT", "IOT_KEY",
            "REPORTER_TOKEN")


def log(text):
    print(text, flush=True)


def classify(status):
    """API status -> result; None means the API did not answer."""
    if status == 201:
        return "created"
    if status == 200:
        return "duplicate"
    if status is not None and 400 <= status < 500:
        return "rejected"
    return "error"


def post(body, token, url=API, timeout=5):
    """POST the message bytes unchanged (the API validates them); returns the HTTP status or None."""
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, ValueError):
        return None


def handle(payload, devices, send):
    """Returns (event_id to log, result). The device check comes BEFORE anything is sent."""
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        event = None
    if not isinstance(event, dict):
        return "-", "rejected"
    event_id = event.get("event_id")
    shown = event_id if isinstance(event_id, str) and ID_RE.fullmatch(event_id) else "-"  # no log injection
    device_id = event.get("device_id")
    if not isinstance(device_id, str) or device_id not in devices:
        return shown, "rejected"
    return shown, classify(send(payload))


def settings(environ=None):
    env = os.environ if environ is None else environ
    missing = [name for name in SETTINGS if not env.get(name)]
    if missing:
        raise SystemExit("consumer: missing settings " + ", ".join(missing))
    cfg = {name: env[name].strip() for name in SETTINGS}
    cfg["devices"] = frozenset(d.strip() for d in cfg["IOT_DEVICE_IDS"].split(",") if d.strip())
    return cfg


def main():
    cfg = settings()
    import paho.mqtt.client as mqtt  # vendored on the host; imported here so tests run without it

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["IOT_CLIENT_ID"], protocol=mqtt.MQTTv5)
    client.tls_set(ca_certs=cfg["IOT_CA"], certfile=cfg["IOT_CERT"], keyfile=cfg["IOT_KEY"],
                   cert_reqs=ssl.CERT_REQUIRED)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(c, userdata, flags, reason_code, properties):
        log("connack rc=%d" % reason_code.value)
        if not reason_code.is_failure:
            c.subscribe(cfg["IOT_TOPIC"], qos=1)  # again after every reconnect

    def on_subscribe(c, userdata, mid, reason_code_list, properties):
        log("subscribed rc=%d" % reason_code_list[0].value)

    def on_message(c, userdata, message):
        event_id, result = handle(message.payload, cfg["devices"], lambda body: post(body, cfg["REPORTER_TOKEN"]))
        log("event_id=%s result=%s" % (event_id, result))

    def on_disconnect(c, userdata, flags, reason_code, properties):
        log("disconnected rc=%d" % reason_code.value)

    client.on_connect, client.on_subscribe = on_connect, on_subscribe
    client.on_message, client.on_disconnect = on_message, on_disconnect
    client.connect(cfg["IOT_ENDPOINT"], 8883, keepalive=60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    sys.exit(main())
