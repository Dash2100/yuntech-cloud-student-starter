#!/usr/bin/env python3
"""W6 T2 / T6: connect once as the gateway or the consumer identity and print the MQTT 5 reason codes.

  python3 tools/mqtt_check.py --as consumer --sub cxe/<代號>/gw/events --wait 60   # terminal 1, first
  python3 tools/mqtt_check.py --as gw --pub cxe/<代號>/gw/events                   # terminal 2
  python3 tools/mqtt_check.py --as gw --pub cxe/<代號>-x/gw/events                 # 越權：expect puback=135

Endpoint, Amazon Root CA 1 and the role's certificate/key come from .local/iot/ (deploy/iot-up.sh);
the 代號 <group>-<owner> comes from .local/resources.json. Client ID gw-<代號> or consumer-<代號>.
MQTT 5, QoS 1, port 8883, mutual TLS that always verifies the broker. Reason codes < 128 are
success, 135 = not authorized. Never prints keys or certificates.
"""
import argparse
import json
from pathlib import Path
import re
import ssl
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
IOT = ROOT / ".local" / "iot"
RESOURCES = ROOT / ".local" / "resources.json"
CODE_RE = re.compile(r"[a-z0-9]{1,16}-[a-z0-9]{1,16}")
ROLES = ("gw", "consumer")


def local_code():
    res = json.loads(RESOURCES.read_text(encoding="utf-8")) if RESOURCES.exists() else {}
    code = f"{res.get('group')}-{res.get('owner')}"
    if not CODE_RE.fullmatch(code):
        raise SystemExit("STOP: .local/resources.json 沒有 group／owner；先完成 W3 的 up.sh。")
    return code


class Session:
    """One MQTT 5 connection; CONNACK, PUBACK and SUBACK can be awaited and are matched by message ID."""

    def __init__(self, role, code, on_message=None):
        if role not in ROLES or not CODE_RE.fullmatch(code):
            raise SystemExit("STOP: 身分要是 gw 或 consumer，代號格式如 g03-m2。")
        files = [IOT / "endpoint", IOT / "AmazonRootCA1.pem", IOT / f"{role}.cert.pem", IOT / f"{role}.private.key"]
        missing = [p.name for p in files if not p.exists()]
        if missing:
            raise SystemExit(f"STOP: .local/iot/ 缺少 {', '.join(missing)}；先跑 deploy/iot-up.sh。")
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise SystemExit("STOP: 先 python3 -m pip install --user paho-mqtt==2.1.0")
        self.endpoint = files[0].read_text(encoding="utf-8").strip()
        self.client_id = f"{role}-{code}"
        self.connack = None
        self.disconnect = None
        self.acks = {}  # mid -> reason code value (PUBACK or the first SUBACK code)
        self._cond = threading.Condition()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, protocol=mqtt.MQTTv5)
        self.client.tls_set(ca_certs=str(files[1]), certfile=str(files[2]), keyfile=str(files[3]),
                            cert_reqs=ssl.CERT_REQUIRED)
        self.client.on_connect = self._on_connect
        self.client.on_publish = self._on_publish
        self.client.on_subscribe = self._on_subscribe
        self.client.on_disconnect = self._on_disconnect
        if on_message:
            self.client.on_message = lambda c, u, message: on_message(message)

    def _set(self, update):
        with self._cond:
            update()
            self._cond.notify_all()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._set(lambda: setattr(self, "connack", reason_code.value))

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        self._set(lambda: self.acks.__setitem__(mid, reason_code.value))

    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties):
        self._set(lambda: self.acks.__setitem__(mid, reason_code_list[0].value))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._set(lambda: setattr(self, "disconnect", reason_code.value))

    def _wait(self, ready, timeout):
        with self._cond:
            self._cond.wait_for(lambda: ready() or self.disconnect is not None, timeout)
            return ready()

    def connect(self, timeout=15):
        """Returns the CONNACK reason code, or None when the broker closed the connection first."""
        self.client.connect(self.endpoint, 8883, keepalive=30)
        self.client.loop_start()
        self._wait(lambda: self.connack is not None, timeout)
        return self.connack

    def publish(self, topic, payload, timeout=10):
        """QoS 1; returns the PUBACK reason code for THIS message ID, or None (no PUBACK)."""
        mid = self.client.publish(topic, payload, qos=1).mid
        return self.acks.get(mid) if self._wait(lambda: mid in self.acks, timeout) else None

    def subscribe(self, topic, timeout=10):
        """QoS 1; returns the SUBACK reason code (1 = granted QoS 1, 135 = not authorized) or None."""
        _, mid = self.client.subscribe(topic, qos=1)
        return self.acks.get(mid) if self._wait(lambda: mid in self.acks, timeout) else None

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


def show(code):
    return "none" if code is None else str(code)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as", dest="role", choices=ROLES, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pub", metavar="TOPIC")
    action.add_argument("--sub", metavar="TOPIC_FILTER")
    parser.add_argument("--wait", type=int, default=10, help="訂閱後等幾秒收訊息")
    args = parser.parse_args()
    code = local_code()

    def received(message):
        print(f"recv topic={message.topic} payload={message.payload.decode('utf-8', 'replace')[:300]}", flush=True)

    session = Session(args.role, code, on_message=received)
    rc = session.connect()
    print(f"client_id={session.client_id} connack={show(rc)}", flush=True)
    ok = rc == 0
    if ok and args.pub:
        rc = session.publish(args.pub, json.dumps({"check": "t2", "code": code}, separators=(",", ":")))
        print(f"puback={show(rc)}", flush=True)
        ok = rc is not None and rc < 128
    elif ok:
        rc = session.subscribe(args.sub)
        print(f"suback={show(rc)}", flush=True)
        ok = rc is not None and rc < 128
        if ok:
            time.sleep(max(args.wait, 0))
    if session.disconnect is not None:
        print(f"disconnected rc={session.disconnect}", flush=True)
    session.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
