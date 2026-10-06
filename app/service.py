#!/usr/bin/env python3
"""Inspection service: W3 /health, the W4 event API and display page, W5 persistence.

W5: events live in a private RDS PostgreSQL (table `events`, primary key event_id),
so a restart keeps them and a resend of the same event is idempotent:
new event_id -> 201, same id + same content -> 200 (nothing added), same id +
different content -> 409. The primary key decides duplicates (INSERT ... ON CONFLICT),
never a "look first, then write" check. Every SQL statement is parameterised.

Tokens and DB settings come from the environment (systemd
EnvironmentFile=/etc/inspection/app.env) and are never logged, echoed in
responses or placed in the page. Without DB settings the service still starts:
/health answers 200 with db_configured=false and the event API answers 503.
Must stay compatible with the AL2023 system Python (3.9) and python3-psycopg2.
"""
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from urllib.parse import urlsplit

MAX_BODY = 4 * 1024
LIST_LIMIT = 50
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})")
TYPES = ("status", "anomaly", "test")
REQUIRED = ("event_id", "device_id", "observed_at", "type")
ALLOWED = REQUIRED + ("note",)
NOTE_MAX = 200
CONTENT = ("device_id", "observed_at", "type", "note")  # compared on a resend
DB_CA = "/etc/inspection/rds-ca.pem"
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    text PRIMARY KEY,
    device_id   text NOT NULL,
    observed_at text NOT NULL,  -- kept exactly as sent so a resend compares as identical
    type        text NOT NULL,
    note        text,
    received_at timestamptz NOT NULL
)
"""
COLUMNS = "event_id, device_id, observed_at, type, note, received_at"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Rejected(Exception):
    def __init__(self, status, error, field):
        super().__init__(error)
        self.status, self.error, self.field = status, error, field


def valid_timestamp(value):
    """ISO 8601 with a mandatory offset (Z or ±HH:MM); calendar values must exist."""
    match = TIME_RE.fullmatch(value)
    if not match:
        return False
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    if match.group(1):  # Python 3.9 fromisoformat needs exactly 3 or 6 fraction digits.
        text = text.replace(match.group(1), match.group(1).ljust(7, "0"), 1)
    try:
        return datetime.fromisoformat(text).utcoffset() is not None
    except ValueError:
        return False


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name):
    raise ValueError("non-standard JSON constant")


def validate_event(raw):
    """Return the event dict or raise Rejected(400) naming the first bad field."""
    try:
        event = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                           parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError):
        raise Rejected(400, "invalid_json", "body")
    if not isinstance(event, dict):
        raise Rejected(400, "invalid_json", "body")
    for key in event:
        if key not in ALLOWED:
            raise Rejected(400, "unknown_field", key[:64])
    for key in REQUIRED:
        if key not in event:
            raise Rejected(400, "missing_field", key)
    checks = (
        ("event_id", lambda v: isinstance(v, str) and ID_RE.fullmatch(v)),
        ("device_id", lambda v: isinstance(v, str) and DEVICE_RE.fullmatch(v)),
        ("observed_at", lambda v: isinstance(v, str) and valid_timestamp(v)),
        ("type", lambda v: isinstance(v, str) and v in TYPES),
        ("note", lambda v: isinstance(v, str) and len(v) <= NOTE_MAX),
    )
    for key, ok in checks:
        if key in event and not ok(event[key]):
            raise Rejected(400, "invalid_field", key)
    return event


def log(text):
    print(text, file=sys.stderr, flush=True)


def db_problem(exc):
    """Name the kind of DB failure without the message (it can contain host or user names)."""
    text = str(exc).lower()
    for words, kind in ((("timeout", "could not connect", "connection refused", "could not translate"), "network"),
                        (("password", "authentication"), "auth"),
                        (("certificate", "ssl", "tls"), "tls"),
                        (("syntax", "column", "relation", "permission denied"), "sql")):
        if any(w in text for w in words):
            return kind
    return type(exc).__name__


class DbStore:
    """Events in PostgreSQL. One short connection per request; the schema is created on first use."""

    def __init__(self, connect_kwargs):
        import psycopg2  # imported here so the service can start (db_configured=false) without the driver
        self._db = psycopg2
        self._kwargs = dict(connect_kwargs)
        self._ready = False
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        names = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
        if not all(env.get(n) for n in names):
            return None
        try:
            return cls({"host": env["DB_HOST"], "port": int(env.get("DB_PORT") or 5432), "dbname": env["DB_NAME"],
                        "user": env["DB_USER"], "password": env["DB_PASSWORD"], "sslmode": "verify-full",
                        "sslrootcert": DB_CA, "connect_timeout": 5, "application_name": "inspection"})
        except ImportError:
            log("db: psycopg2 is not installed")
            return None

    def _run(self, work):
        try:
            conn = self._db.connect(**self._kwargs)
        except self._db.Error as exc:
            log("db connect failed: " + db_problem(exc))
            raise Rejected(503, "db_unavailable", "database")
        try:
            if not self._ready:
                with self._lock:  # one CREATE at a time; concurrent IF NOT EXISTS can still collide
                    if not self._ready:
                        with conn, conn.cursor() as cur:
                            cur.execute(SCHEMA)
                        self._ready = True
            with conn:  # commit on success, roll back on error
                with conn.cursor() as cur:
                    return work(cur)
        except self._db.Error as exc:
            log("db query failed: " + db_problem(exc))
            raise Rejected(503, "db_unavailable", "database")
        finally:
            conn.close()

    @staticmethod
    def _row(row):
        event = dict(zip(COLUMNS.split(", "), row))
        if event["note"] is None:
            del event["note"]
        event["received_at"] = event["received_at"].astimezone(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        return event

    def check(self):
        """Startup probe: create the table if the DB is reachable; never raises."""
        try:
            self._run(lambda cur: None)
            log("db: ready")
        except Rejected:
            pass

    def add(self, event):
        """Returns (status, stored event): 201 new, 200 identical resend. Raises 409 on a conflicting resend."""
        def work(cur):
            cur.execute("INSERT INTO events (" + COLUMNS + ") VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (event_id) DO NOTHING RETURNING " + COLUMNS,
                        (event["event_id"], event["device_id"], event["observed_at"], event["type"],
                         event.get("note"), datetime.now(timezone.utc)))
            row = cur.fetchone()
            if row:
                return 201, self._row(row)
            # The primary key refused the insert: compare with the copy that is already stored.
            cur.execute("SELECT " + COLUMNS + " FROM events WHERE event_id = %s", (event["event_id"],))
            stored = self._row(cur.fetchone())
            if all(stored.get(k) == event.get(k) for k in CONTENT):
                return 200, stored
            raise Rejected(409, "duplicate_event", "event_id")
        return self._run(work)

    def latest(self, limit=LIST_LIMIT):
        def work(cur):
            cur.execute("SELECT " + COLUMNS + " FROM events ORDER BY received_at DESC, event_id DESC LIMIT %s",
                        (limit,))
            return [self._row(r) for r in cur.fetchall()]
        return self._run(work)

    def get(self, event_id):
        def work(cur):
            cur.execute("SELECT " + COLUMNS + " FROM events WHERE event_id = %s", (event_id,))
            row = cur.fetchone()
            return self._row(row) if row else None
        return self._run(work)


PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>巡檢事件</title>
<style nonce="__NONCE__">
  :root { color-scheme: light dark; --line: #8884; --muted: #777; }
  body { font: 15px/1.5 system-ui, -apple-system, "PingFang TC", "Noto Sans TC", sans-serif;
         max-width: 1100px; margin: 24px auto; padding: 0 16px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 16px; word-break: break-all; }
  .bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  input { flex: 1 1 280px; padding: 6px 8px; font: inherit; }
  button { padding: 6px 14px; font: inherit; cursor: pointer; }
  #status { min-height: 1.5em; margin: 8px 0; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
  th { white-space: nowrap; }
  td { word-break: break-word; }
</style>
</head>
<body>
<h1>巡檢事件</h1>
<div class="meta" id="health">讀取服務狀態中…</div>
<div class="bar">
  <input id="token" type="password" autocomplete="off" spellcheck="false"
         placeholder="貼上 operator 權杖（只存在這個頁面的變數，不寫進網址或瀏覽器）">
  <button id="load" type="button">讀取事件</button>
  <button id="forget" type="button">清除權杖</button>
</div>
<div id="status">尚未讀取。</div>
<div class="table-wrap">
<table>
  <thead><tr><th>event_id</th><th>device_id</th><th>observed_at</th><th>type</th><th>note</th><th>received_at</th></tr></thead>
  <tbody id="rows"></tbody>
</table>
</div>
<script nonce="__NONCE__">
(function () {
  "use strict";
  var token = "";
  var cols = ["event_id", "device_id", "observed_at", "type", "note", "received_at"];
  var el = function (id) { return document.getElementById(id); };
  function say(text) { el("status").textContent = text; }
  function render(events) {
    var body = el("rows");
    while (body.firstChild) { body.removeChild(body.firstChild); }
    events.forEach(function (ev) {
      var tr = document.createElement("tr");
      cols.forEach(function (c) {
        var td = document.createElement("td");
        td.textContent = ev[c] === undefined ? "" : String(ev[c]);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }
  function load() {
    var typed = el("token").value.trim();
    if (typed) { token = typed; }
    el("token").value = "";  // never leave the token visible on screen
    if (!token) { say("請先貼上 operator 權杖。"); return; }
    say("讀取中…");
    fetch("/events", { headers: { "Authorization": "Bearer " + token }, cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status !== 200) {
          render([]);
          say("HTTP " + res.status + "：" + (res.data.error || "錯誤"));
          return;
        }
        render(res.data.events);
        say("共 " + res.data.events.length + " 筆（最新在上，最多 50 筆）；讀取時間 " + new Date().toISOString());
      })
      .catch(function () { say("連線失敗。"); });
  }
  el("load").addEventListener("click", load);
  el("token").addEventListener("keydown", function (e) { if (e.key === "Enter") { load(); } });
  el("forget").addEventListener("click", function () {
    token = ""; el("token").value = ""; render([]); say("已清除權杖。");
  });
  fetch("/health", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (h) {
    el("health").textContent = "service=" + h.service + "　version=" + h.version +
      "　started_at=" + h.started_at + "　auth_configured=" + h.auth_configured +
      "　db_configured=" + h.db_configured;
  }).catch(function () { el("health").textContent = "無法讀取 /health"; });
}());
</script>
</body>
</html>
"""


def make_server(version_file, port=8080, tokens=None, store="env"):
    """tokens: {"reporter": str, "operator": str}; defaults to REPORTER_TOKEN/OPERATOR_TOKEN env.
    store: an object with add/latest/get (tests pass a DbStore on a local PostgreSQL), None for
    "no database", or "env" to build it from DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD."""
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = utc_now()
    if tokens is None:
        tokens = {"reporter": os.environ.get("REPORTER_TOKEN", ""),
                  "operator": os.environ.get("OPERATOR_TOKEN", "")}
    roles = [(role, (tokens.get(role) or "").encode("utf-8")) for role in ("reporter", "operator")]
    auth_configured = all(secret for _, secret in roles) and roles[0][1] != roles[1][1]
    if store == "env":
        store = DbStore.from_env()
        if store is not None:
            store.check()
    db_configured = store is not None

    def events():
        if store is None:
            raise Rejected(503, "db_not_configured", "database")
        return store

    class Handler(BaseHTTPRequestHandler):
        server_version = "inspection"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        # ---- responses -------------------------------------------------
        def _send(self, status, data, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, body, extra=None):
            self._send(status, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _error(self, exc, extra=None):
            self._json(exc.status, {"error": exc.error, "field": exc.field}, extra)

        # ---- checks ----------------------------------------------------
        def _role(self):
            """Authentication: returns the role or raises 401 without saying what was wrong."""
            header = self.headers.get("Authorization", "")
            scheme, _, presented = header.partition(" ")
            presented = presented.strip().encode("utf-8")
            if auth_configured and scheme.lower() == "bearer" and presented:
                for role, secret in roles:
                    if hmac.compare_digest(presented, secret):
                        return role
            raise Rejected(401, "unauthorized", "authorization")

        def _require(self, role):
            if self._role() != role:  # 401 is decided before 403
                raise Rejected(403, "forbidden", "role")

        def _route(self, method):
            path = urlsplit(self.path).path
            if path == "/health":
                allowed = ("GET",)
            elif path == "/":
                allowed = ("GET",)
            elif path == "/events":
                allowed = ("GET", "POST")
            elif path.startswith("/events/") and path.count("/") == 2:
                allowed = ("GET",)
            else:
                raise Rejected(404, "not_found", "path")
            if method not in allowed:
                raise Rejected(405, "method_not_allowed", "method")
            return path

        # ---- handlers --------------------------------------------------
        def _dispatch(self, method):
            extra = None
            try:
                path = self._route(method)
                if path == "/health":
                    self._json(200, {"status": "ok", "service": "inspection", "version": version,
                                     "started_at": started, "auth_configured": auth_configured,
                                     "db_configured": db_configured})
                elif path == "/":
                    nonce = secrets.token_urlsafe(16)
                    csp = ("default-src 'none'; script-src 'nonce-{0}'; style-src 'nonce-{0}'; "
                           "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                           "frame-ancestors 'none'").format(nonce)
                    self._send(200, PAGE.replace("__NONCE__", nonce).encode("utf-8"),
                               "text/html; charset=utf-8",
                               {"Content-Security-Policy": csp, "Referrer-Policy": "no-referrer"})
                elif path == "/events" and method == "POST":
                    self._create()
                elif path == "/events":
                    self._require("operator")
                    self._json(200, {"events": events().latest()})
                else:
                    self._require("operator")
                    event_id = path[len("/events/"):]
                    event = events().get(event_id) if ID_RE.fullmatch(event_id) else None
                    if event is None:
                        raise Rejected(404, "not_found", "event_id")
                    self._json(200, event)
            except Rejected as exc:
                if exc.status == 405:
                    extra = {"Allow": "GET, POST" if urlsplit(self.path).path == "/events" else "GET"}
                self._error(exc, extra)

        def _create(self):
            # Order matters: 401 -> 403 -> 400 -> 503 (no DB) -> 409 / 200 / 201. Nothing about
            # the body is inspected until the caller is known to be a reporter.
            self._require("reporter")
            media = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if media != "application/json":
                raise Rejected(400, "unsupported_content_type", "content-type")
            length = self.headers.get("Content-Length", "")
            if not length.isdigit():
                raise Rejected(400, "length_required", "body")
            if int(length) > MAX_BODY:
                raise Rejected(400, "body_too_large", "body")
            event = validate_event(self.rfile.read(int(length)))
            status, stored = events().add(event)
            # 200 = the same event was already stored (resend); received_at is the first arrival.
            self._json(status, {"event_id": stored["event_id"], "received_at": stored["received_at"]})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers or query strings.

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            # The default prints a traceback that may include request data; keep only the type.
            log("request failed: " + type(sys.exc_info()[1]).__name__)

    return Server(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
