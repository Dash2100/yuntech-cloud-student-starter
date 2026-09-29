#!/usr/bin/env bash
# 建立 1 SG + 1 key pair + 1 台 t3.micro，部署指定 commit（預設 HEAD）。用法：bash deploy/up.sh [--commit SHA] [--group g08 --owner m1 --source-cidr x.x.x.x/32]
set -euo pipefail
exec python3 "$(dirname "$0")/infra.py" up "$@"
