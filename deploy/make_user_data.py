#!/usr/bin/env python3
"""Package ONLY allowlisted committed files. No cloud calls, secrets or git clone."""
import argparse
import base64
import gzip
import io
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def build(commit):
    sha = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options",
                                   commit + "^{commit}"], cwd=ROOT, text=True).strip()
    files = {}
    for name in ("app/service.py", "deploy/nginx.conf"):
        files[name] = subprocess.check_output(["git", "show", sha + ":" + name], cwd=ROOT)
    files["app/version"] = (sha + "\n").encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    payload = base64.b64encode(gzip.compress(archive.getvalue(), mtime=0)).decode()
    script = """#!/bin/bash
set -euo pipefail
dnf install -y nginx python3 python3-psycopg2 postgresql15
install -d -m 755 /etc/inspection
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o /etc/inspection/rds-ca.pem
chmod 644 /etc/inspection/rds-ca.pem
id inspection >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin inspection
install -d -m 755 /opt/inspection
base64 --decode <<'W3_ARCHIVE' | tar -xz -C /opt/inspection
PAYLOAD
W3_ARCHIVE
install -m 644 /opt/inspection/deploy/nginx.conf /etc/nginx/nginx.conf
cat > /etc/systemd/system/inspection.service <<'W3_UNIT'
[Unit]
Description=W3 inspection service
After=network.target
[Service]
Type=simple
User=inspection
EnvironmentFile=-/etc/inspection/app.env
Environment=HOME=/opt/inspection
WorkingDirectory=/opt/inspection/app
ExecStart=/usr/bin/python3 /opt/inspection/app/service.py
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
[Install]
WantedBy=multi-user.target
W3_UNIT
systemctl daemon-reload
nginx -t
systemctl enable --now inspection nginx
systemctl restart inspection
systemctl reload nginx
""".replace("PAYLOAD", payload)
    data = script.encode()
    if len(data) >= 16 * 1024:
        raise ValueError("User data exceeds the course limit (<16384 raw bytes)")
    return sha, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="Commit to deploy; uncommitted changes are not included")
    parser.add_argument("output", type=Path, help="New output file, e.g. .local/w03-user-data.sh")
    args = parser.parse_args()
    sha, data = build(args.commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        args.output.chmod(0o600)
        stream.write(data)
    print(f"Commit: {sha}\nUser data: {len(data)} bytes (<16384); {args.output}")


if __name__ == "__main__":
    main()
