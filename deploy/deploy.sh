#!/usr/bin/env bash
# 把已 commit 的 HEAD 經 SSH 裝到同一台主機，放 .local/app.env（root 600）並驗 /health。用法：bash deploy/deploy.sh
set -euo pipefail
exec python3 "$(dirname "$0")/infra.py" deploy "$@"
