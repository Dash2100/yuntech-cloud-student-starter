#!/usr/bin/env python3
"""W6 T5: one simulated gateway reports for 10 devices; 32 messages, each waits for its PUBACK.

  python3 tools/gateway_sim.py --code g03-m2 --run r1     # use a NEW --run for every reconciliation

Prefix = <代號>-<run>. Sends, as the gateway identity (gw-<代號>, same connection as
tools/mqtt_check.py) to cxe/<代號>/gw/events:
  30  devices <代號>-d01 … d10, 3 events each: event_id <prefix>-dNN-01 … 03
   1  the very first event again, byte for byte (W5 idempotency -> duplicate)
   1  <代號>-d99, a device NOT on the consumer's list (-> rejected, never reaches the API)
Every message is a W4 contract event (event_id, device_id, observed_at with offset, type=status).
Writes the prefix and the PUBACK count to .local/w06-last-run.json for tests/w06_reconcile.py.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
from mqtt_check import CODE_RE, Session  # noqa: E402
import lab  # noqa: E402

PREFIX_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
RUN_RE = re.compile(r"r[0-9]{1,4}")
LAST_RUN = ROOT / ".local" / "w06-last-run.json"
TOTAL = 32


def prefix_for(code, run):
    prefix = f"{code}-{run}"
    if not CODE_RE.fullmatch(code) or not RUN_RE.fullmatch(run) or not PREFIX_RE.match(prefix):
        raise SystemExit("STOP: --code 如 g03-m2、--run 如 r1；前綴只能是英數與 -（1–40 字）。")
    return prefix


def build_events(code, prefix, now=None):
    """The 32 events in send order."""
    def stamp():
        return now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [{"event_id": f"{prefix}-d{d:02d}-{n:02d}", "device_id": f"{code}-d{d:02d}",
               "observed_at": stamp(), "type": "status"}
              for d in range(1, 11) for n in range(1, 4)]
    events.append(events[0])  # identical resend
    events.append({"event_id": f"{prefix}-d99-01", "device_id": f"{code}-d99", "observed_at": stamp(),
                   "type": "status"})
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", required=True, help="代號，例如 g03-m2")
    parser.add_argument("--run", required=True, help="r1、r2…；每次對帳換一個")
    args = parser.parse_args()
    prefix = prefix_for(args.code, args.run)
    topic = f"cxe/{args.code}/gw/events"
    session = Session("gw", args.code)
    rc = session.connect()
    print(f"client_id={session.client_id} connack={rc} topic={topic} prefix={prefix}", flush=True)
    ok = 0
    if rc == 0:
        for i, event in enumerate(build_events(args.code, prefix), 1):
            rc = session.publish(topic, json.dumps(event, separators=(",", ":")))
            ok += rc is not None and rc < 128
            print(f"{i:02d} event_id={event['event_id']} device_id={event['device_id']} "
                  f"puback={'none' if rc is None else rc}", flush=True)
            if rc is None:
                break  # no PUBACK: the connection is gone, do not keep sending blind
    if session.disconnect is not None:
        print(f"disconnected rc={session.disconnect}", flush=True)
    session.close()
    lab.atomic_write(LAST_RUN, json.dumps({"prefix": prefix, "puback_ok": ok, "total": TOTAL,
                                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    print(f"puback_ok={ok}/{TOTAL}")
    return 0 if ok == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
