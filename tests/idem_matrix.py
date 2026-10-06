#!/usr/bin/env python3
"""W5 T3 idempotency matrix: run the 5 rows against the deployed service in one go.

  python3 tests/idem_matrix.py              # look up the host's CURRENT public IP via AWS first
  python3 tests/idem_matrix.py --seq 2      # re-run with a fresh event_id <group>-<owner>-w5-0002

#4 restarts the service over SSH (sudo systemctl restart inspection) and reads #1 back;
#5 counts #1 with psql ON the EC2 host (the DB is private; DB settings are read from
/etc/inspection/app.env there, so no password crosses the command line or this output).
Tokens come from .local/app.env (must be 600) and are never printed; a response that
contained a token is redacted and the row marked FAIL.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from reject_matrix import load_tokens, request  # noqa: E402

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Same query as labs/05-private-rds/README.md; the event_id goes in as a psql variable (:'event_id').
PSQL = ("sudo bash -c 'set -a; . /etc/inspection/app.env; set +a; "
        "PGPASSWORD=\"$DB_PASSWORD\" psql -At \"host=$DB_HOST dbname=$DB_NAME user=$DB_USER "
        "sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem\" -v event_id={event_id}'")
COUNT_SQL = "SELECT count(*) FROM events WHERE event_id = :'event_id';\n"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def host():
    """(base_url, ssh command prefix, group, owner) for the host in .local/resources.json."""
    sys.path.insert(0, str(ROOT / "deploy"))
    import infra  # noqa: E402  (uses scripts/lab.py run_aws)
    infra.REGION = infra.lab.context()["region"]
    res = infra.current()
    inst = infra.describe_instance(res["instance"]["id"])
    if not inst or inst["State"]["Name"] != "running":
        raise SystemExit("STOP: 主機不是 running。")
    cfg = infra.load_config(argparse.Namespace(group=res["group"], owner=res["owner"],
                                               source_cidr=res["source_cidr"], week=None, az=None))
    ip = inst["PublicIpAddress"]
    return f"http://{ip}", infra.ssh_base(cfg, ip), res["group"], res["owner"]


def ssh_restart(ssh):
    def restart():
        run = subprocess.run(ssh + ["sudo systemctl restart inspection && systemctl is-active inspection"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        return f"systemctl restart inspection → {run.stdout.strip() or 'failed'} (exit {run.returncode})"
    return restart


def ssh_count(ssh):
    def count(event_id):
        run = subprocess.run(ssh + [PSQL.format(event_id=event_id)], input=COUNT_SQL,
                             capture_output=True, text=True, check=False)
        if run.returncode:
            return None, f"psql exit {run.returncode}"
        return run.stdout.strip(), "psql on EC2: SELECT count(*) FROM events WHERE event_id = :'event_id'"
    return count


def run(base, reporter, operator, group, owner, seq, restart, count, out=print):
    event = {"event_id": f"{group}-{owner}-w5-{seq:04d}", "device_id": f"{group}-d03",
             "observed_at": "2026-10-06T10:00:00+08:00", "type": "anomaly", "note": "合成測資：W5 冪等測試"}
    if not ID_RE.fullmatch(event["event_id"]):
        raise SystemExit("STOP: event_id 格式不符。")
    secrets_ = (reporter, operator)

    def show(text):
        return "<REDACTED: response contained a token>" if any(s in text for s in secrets_) else text

    def health():
        try:
            status, text = request(base, "GET", "/health")
        except OSError:  # the service is between stop and start
            return {}
        return json.loads(text) if status == 200 else {}

    h = health()
    out(f"target: {base}   at: {now()}")
    out(f"version: {h.get('version')}   db_configured: {h.get('db_configured')}")
    passed = 0

    def row(number, label, expected, status, text, ok=True):
        nonlocal passed
        ok = ok and status == expected and show(text) == text
        passed += ok
        out(f"#{number} {label:28} HTTP {status} (預期 {expected}) {'PASS' if ok else 'FAIL'}")
        out(f"   {show(text)}")

    status, text = request(base, "POST", "/events", reporter, event)
    first = json.loads(text) if status in (200, 201) else {}
    row(1, "POST 新事件", 201, status, text, first.get("event_id") == event["event_id"])

    status, text = request(base, "POST", "/events", reporter, event)
    same = json.loads(text) if status == 200 else {}
    row(2, "POST 原樣重送", 200, status, text,
        same.get("received_at") == first.get("received_at"))  # the first copy, not a new one

    status, text = request(base, "POST", "/events", reporter, dict(event, note="合成測資：內容不同"))
    row(3, "POST 同 ID、note 不同", 409, status, text)

    before = h.get("started_at")
    out(f"   [{now()}] {restart()}")
    for _ in range(30):  # wait until the restarted process answers
        h2 = health()
        if h2.get("started_at") and h2.get("started_at") != before:
            break
        time.sleep(1)
    out(f"   [{now()}] /health started_at {before} → {h2.get('started_at')}")
    status, text = request(base, "GET", "/events/" + event["event_id"], operator)
    back = json.loads(text) if status == 200 else {}
    row(4, "重啟後 GET /events/#1", 200, status, text,
        h2.get("started_at") != before and all(back.get(k) == v for k, v in event.items()))

    n, how = count(event["event_id"])
    out(f"#5 {how}")
    ok = n == "1"
    passed += ok
    out(f"   count = {n} (預期 1) {'PASS' if ok else 'FAIL'}")
    out(f"result: {passed}/5 符合冪等規則")
    return passed == 5


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".local" / "app.env"))
    parser.add_argument("--seq", type=int, default=1)
    args = parser.parse_args()
    reporter, operator = load_tokens(args.env_file)
    base, ssh, group, owner = host()
    return 0 if run(base, reporter, operator, group, owner, args.seq, ssh_restart(ssh), ssh_count(ssh)) else 1


if __name__ == "__main__":
    sys.exit(main())
