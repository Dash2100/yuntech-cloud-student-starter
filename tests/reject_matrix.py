#!/usr/bin/env python3
"""W4 T3 rejection matrix: run the 7 rows against the deployed service in one go.

  python3 tests/reject_matrix.py               # look up the host's CURRENT public IP via AWS first
  python3 tests/reject_matrix.py --seq 2       # re-run: #1 uses a fresh event_id <group>-<owner>-0002
  python3 tests/reject_matrix.py --base-url http://127.0.0.1:8080 --env-file <file>   # offline

Tokens are read from .local/app.env (must be 600) and never printed; any response
that contained a token would be redacted and the row marked FAIL.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load_tokens(path):
    path = Path(path)
    if (path.stat().st_mode & 0o777) != 0o600:
        raise SystemExit(f"STOP: {path} 權限不是 600。")
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    reporter, operator = values.get("REPORTER_TOKEN", "").strip(), values.get("OPERATOR_TOKEN", "").strip()
    if not reporter or not operator:
        raise SystemExit("STOP: 缺少 REPORTER_TOKEN 或 OPERATOR_TOKEN（不顯示內容）。")
    return reporter, operator


def current_base_url():
    sys.path.insert(0, str(ROOT / "deploy"))
    import infra  # noqa: E402  (uses scripts/lab.py run_aws)
    infra.REGION = infra.lab.context()["region"]
    res = infra.current()
    inst = infra.describe_instance(res["instance"]["id"])
    if not inst or inst["State"]["Name"] != "running":
        raise SystemExit("STOP: 主機不是 running。")
    return f"http://{inst['PublicIpAddress']}", res["group"], res["owner"]


def request(base, method, path, token=None, body=None, content_type="application/json"):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def run(base, reporter, operator, group, owner, seq, out=print):
    accept = json.loads((FIXTURES / f"{group}-{owner}-accept.json").read_text(encoding="utf-8"))["event"]
    no_tz = json.loads((FIXTURES / f"{group}-{owner}-reject-no-timezone.json").read_text(encoding="utf-8"))["event"]
    event = copy.deepcopy(accept)
    event["event_id"] = f"{group}-{owner}-{seq:04d}"
    no_tz = dict(no_tz, event_id=f"{group}-{owner}-{seq:04d}-tz")

    status, text = request(base, "GET", "/health")
    health = json.loads(text) if status == 200 else {}
    out(f"target: {base}   at: {datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}")
    out(f"version: {health.get('version')}   auth_configured: {health.get('auth_configured')}")

    rows = [
        (1, "reporter 送一筆合法事件", 201, ("POST", "/events", reporter, event)),
        (2, "同上，不帶權杖", 401, ("POST", "/events", None, event)),
        (3, "operator 權杖送事件", 403, ("POST", "/events", operator, event)),
        (4, "reporter，observed_at 沒有時區", 400, ("POST", "/events", reporter, no_tz)),
        (5, "reporter，同 ID、note 不同", 409, ("POST", "/events", reporter, dict(event, note="改過的內容"))),
        (6, "reporter 權杖讀清單", 403, ("GET", "/events", reporter, None)),
        (7, "operator 權杖讀清單", 200, ("GET", "/events", operator, None)),
    ]
    passed = 0
    for number, label, expected, (method, path, token, body) in rows:
        status, text = request(base, method, path, token, body)
        leaked = any(secret in text for secret in (reporter, operator))
        if leaked:
            text = "<REDACTED: response contained a token>"
        ok = status == expected and not leaked
        if number == 1 and ok:
            ok = json.loads(text).get("event_id") == event["event_id"]
        if number == 4 and ok:
            ok = json.loads(text).get("field") == "observed_at"
        if number == 7 and ok:
            ok = any(e.get("event_id") == event["event_id"] for e in json.loads(text).get("events", []))
        passed += ok
        out(f"#{number} {method} {path:8} {label:22} HTTP {status} (預期 {expected}) {'PASS' if ok else 'FAIL'}")
        out(f"   {text}")
    out(f"result: {passed}/7 符合契約")
    return passed == 7


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", default=str(ROOT / ".local" / "app.env"))
    parser.add_argument("--group", default="g08")
    parser.add_argument("--owner", default="m1")
    parser.add_argument("--seq", type=int, default=1)
    args = parser.parse_args()
    reporter, operator = load_tokens(args.env_file)
    group, owner = args.group, args.owner
    base = args.base_url
    if not base:
        base, group, owner = current_base_url()
    return 0 if run(base.rstrip("/"), reporter, operator, group, owner, args.seq) else 1


if __name__ == "__main__":
    sys.exit(main())
