#!/usr/bin/env bash
# W6 T2：讀 IoT 端點、建 2 張憑證（gw、consumer）與 2 份精確資源的政策並附加，下載 Amazon Root CA 1。
# 憑證與私鑰由 AWS CLI 直接寫進 .local/iot/（700／600），不顯示；ID 寫進 .local/resources.json。
# 用法：bash deploy/iot-up.sh。只跑一次；中途失敗修好後重跑，已記錄的項目會跳過，不建第二張憑證、不覆蓋私鑰。
set -euo pipefail
exec python3 "$(dirname "$0")/infra.py" iot-up "$@"
