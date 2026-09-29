#!/usr/bin/env bash
# 以 .local/resources.json 的 ID 回收並讀回；--stop 只停止保留。用法：bash deploy/down.sh [--stop]
set -euo pipefail
exec python3 "$(dirname "$0")/infra.py" down "$@"
