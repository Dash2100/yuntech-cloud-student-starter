#!/usr/bin/env python3
"""W6 T5 reconciliation: every number for template section 5 in one run (from the Codespace).

  python3 tests/w06_reconcile.py --code g03-m2 --run r1

Expected for one gateway_sim.py run: PUBACK 32; consumer created/duplicate/rejected/error
30/1/1/0; 30 rows in the DB with the prefix; 3 of them in GET /events?device_id=<代號>-d01.
Consumer counts and the DB count are read ON the host over SSH (same key and user as
deploy.sh), retried every 5 s until the four counts reach 32 or 60 s pass. The prefix is
regex-checked before it goes anywhere and reaches psql only as a -v variable, never inside
the SQL text. Tokens, passwords and keys are never printed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from reject_matrix import load_tokens, request  # noqa: E402

PREFIX_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
RUN_RE = re.compile(r"r[0-9]{1,4}")
LAST_RUN = ROOT / ".local" / "w06-last-run.json"
RESULTS = ("created", "duplicate", "rejected", "error")
EXPECTED = {"puback": 32, "created": 30, "duplicate": 1, "rejected": 1, "error": 0, "db": 30, "filter": 3}
JOURNAL = ("sudo journalctl -u inspection-consumer -o cat --no-pager "
           "| grep -F 'event_id={prefix}-' | grep -o 'result=[a-z]*' | sort | uniq -c")
PSQL = ("sudo bash -c 'set -a; . /etc/inspection/app.env; set +a; "
        "PGPASSWORD=\"$DB_PASSWORD\" psql -At \"host=$DB_HOST dbname=$DB_NAME user=$DB_USER "
        "sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem\" -v prefix={prefix}-'")
COUNT_SQL = "SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';\n"


def parse_counts(text):
    """`uniq -c` lines like '     30 result=created' -> {result: n}."""
    counts = dict.fromkeys(RESULTS, 0)
    for n, result in re.findall(r"^\s*(\d+) result=([a-z]+)\s*$", text, re.M):
        counts[result] = counts.get(result, 0) + int(n)
    return counts


def ssh_counts(ssh, prefix):
    def counts():
        run = subprocess.run(ssh + [JOURNAL.format(prefix=prefix)], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, check=False)
        return parse_counts(run.stdout)
    return counts


def ssh_db_count(ssh, prefix):
    def count():
        run = subprocess.run(ssh + [PSQL.format(prefix=prefix)], input=COUNT_SQL, capture_output=True,
                             text=True, check=False)
        return int(run.stdout.strip()) if run.returncode == 0 and run.stdout.strip().isdigit() else None
    return count


def run(base, operator, code, prefix, counts, db_count, last_run, out=print, limit=60, every=5):
    health_status, text = request(base, "GET", "/health")
    health = json.loads(text) if health_status == 200 else {}
    out(f"target: {base}   version: {health.get('version')}   db_configured: {health.get('db_configured')}")
    out(f"prefix: {prefix}")

    got = {"puback": last_run.get("puback_ok") if last_run.get("prefix") == prefix else None}
    if got["puback"] is None:
        out(f"PUBACK: .local/w06-last-run.json 的前綴是 {last_run.get('prefix')}，不是 {prefix}")

    began = time.monotonic()
    while True:
        consumer = counts()
        if sum(consumer.values()) >= EXPECTED["puback"] or time.monotonic() - began >= limit:
            break
        time.sleep(every)
    got.update(consumer)
    got["db"] = db_count()

    status, text = request(base, "GET", f"/events?device_id={code}-d01", operator)
    if operator in text:
        text, status = "<REDACTED>", None
    listed = [e["event_id"] for e in json.loads(text).get("events", [])] if status == 200 else []
    mine = [e for e in listed if e.startswith(prefix + "-")]
    got["filter"] = len(mine) if status == 200 else None

    rows = [("閘道收到的 PUBACK", "puback"), ("consumer created", "created"), ("consumer duplicate", "duplicate"),
            ("consumer rejected", "rejected"), ("consumer error", "error"),
            ("資料庫（本次前綴）", "db"), (f"GET /events?device_id={code}-d01（本次前綴）", "filter")]
    passed = 0
    for label, key in rows:
        ok = got[key] == EXPECTED[key]
        passed += ok
        out(f"{label:40} {str(got[key]):>5} (預期 {EXPECTED[key]}) {'PASS' if ok else 'FAIL'}")
    out(f"consumer 計數 建立／重複／拒絕／錯誤 = {got['created']}／{got['duplicate']}／{got['rejected']}／{got['error']}")
    out(f"篩選 HTTP {status}：{' '.join(mine) or '（沒有）'}")
    out(f"result: {passed}/{len(rows)} 符合")
    return passed == len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", required=True, help="代號，例如 g03-m2")
    parser.add_argument("--run", required=True, help="和 gateway_sim.py 同一個 r1、r2…")
    parser.add_argument("--env-file", default=str(ROOT / ".local" / "app.env"))
    args = parser.parse_args()
    prefix = f"{args.code}-{args.run}"
    if not RUN_RE.fullmatch(args.run) or not PREFIX_RE.match(prefix):
        raise SystemExit("STOP: 前綴只能是英數與 -（1–40 字），--run 如 r1。")
    _, operator = load_tokens(args.env_file)
    import idem_matrix  # noqa: E402  (looks up the host's CURRENT public IP via AWS)
    base, ssh, group, owner = idem_matrix.host()
    if args.code != f"{group}-{owner}":
        raise SystemExit(f"STOP: --code {args.code} 和 resources.json 的 {group}-{owner} 不同。")
    last_run = json.loads(LAST_RUN.read_text(encoding="utf-8")) if LAST_RUN.exists() else {}
    ok = run(base, operator, args.code, prefix, ssh_counts(ssh, prefix), ssh_db_count(ssh, prefix), last_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
