#!/usr/bin/env bash
# W5 T2：建 2 個私有子網（不同 AZ、只有 local 的路由表）、DB 子網群組、SG-db（5432 只來自主機 SG）與私有 RDS PostgreSQL。
# 密碼寫進 .local/db.env（600），不顯示、不進命令列；ID 寫進 .local/resources.json。用法：bash deploy/db-up.sh
# 只能跑一次；之後讀回用：python3 deploy/infra.py db-status
set -euo pipefail
exec python3 "$(dirname "$0")/infra.py" db-up "$@"
