#!/usr/bin/env bash
# 把已 commit 的 HEAD 經 SSH 裝到同一台主機，放 .local/app.env＋.local/db.env（W5，若存在）到主機秘密檔（root 600），
# 放完再重啟服務並驗 /health。W6：有 .local/iot/ 時再加 IOT_ 設定、把 consumer 憑證／私鑰／Root CA 放到
# /etc/inspection/iot/（700／600，屬於 inspection），最後重啟 inspection-consumer。用法：bash deploy/deploy.sh
set -euo pipefail
exec python3 "$(dirname "$0")/infra.py" deploy "$@"
