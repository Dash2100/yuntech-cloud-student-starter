# W5 Sprint：重啟不丟資料、重送不重複新增

## 這週要做什麼

服務重開後，事件還在；同一筆事件重送，不會變成兩筆。做法：把事件存進一台**私有的 PostgreSQL 資料庫（RDS）**，只有你的主機連得到。

**本週每個人都在自己的 Learner Lab 帳號做一套**（自己的主機＋自己的資料庫），各自繳交。可以和組員討論、互相審查程式，但 AWS 的動作只用自己的帳號做，不交換憑證。

## 先懂這些名詞

| 名詞 | 一句話 |
|---|---|
| 持久化（persistence） | 資料寫到程式結束後仍存在的地方 |
| RDS、PostgreSQL | AWS 代管的資料庫服務；PostgreSQL 是其中一種資料庫軟體 |
| 私有子網（private subnet） | 實際使用的路由表**沒有** `0.0.0.0/0 → igw` 的子網；外面進不來 |
| DB 子網群組 | 告訴 RDS 可以放在哪幾個子網；至少跨兩個 AZ |
| SG 參照 | SG 規則的來源寫「另一個 SG」：套用那個 SG 的主機都放行 |
| 加密連線（TLS） | 連線加密，並檢查對方真的是那台資料庫（機制 W6 詳談） |
| 資料表、SQL | 欄位固定的一格一格資料；查詢與寫入它的語言 |
| 主鍵（primary key） | 每一列的唯一代號；資料庫會拒絕重複 |
| 冪等（idempotency） | 同一個請求做一次和做很多次，結果一樣 |
| 參數化查詢 | SQL 與資料分開送，資料永遠只當資料 |

## 誰連得到誰

```text
Codespace ──/32──▶ 主機的 SG（22、80）──▶ EC2（你上週那台）
                                             │ 5432，TLS
                                             ▼
                              SG-db（來源＝主機的 SG）──▶ RDS（私有子網 ×2 AZ）
Codespace ──▶ RDS:5432   ✗ 逾時
```

兩種來源別搞混：**外面進主機（22、80）只寫 Codespace 的 `/32`；資料庫的 5432 只寫「主機的 SG」，不寫任何位址。**

## 哪些已經做好、哪些你要做

**已經做好，不用改：**

- `deploy/make_user_data.py`（打包器）這週多做三件事：
  1. 在主機上安裝連 PostgreSQL 用的 Python 套件和 `psql` 指令。
  2. 下載 AWS 的 RDS 憑證到 `/etc/inspection/rds-ca.pem`，讓服務能檢查「連到的真的是 RDS」。
  3. 服務的執行環境補上一個設定，讓資料庫套件在受保護的服務裡也能正常運作（09-28 實測時沒有它會 502）。
- 下方的 `db-up.sh` 五條規格、冪等規則與矩陣：規則已經定好，照著做。

**你要做：**

| 做什麼 | 放哪裡 |
|---|---|
| 建私有子網、資料庫的腳本 | `deploy/db-up.sh` |
| 事件改存進資料庫、照冪等規則回應 | `app/service.py` |
| 部署時多放資料庫的秘密 | `deploy/deploy.sh`（W4 寫的，加一件事） |
| 冪等矩陣腳本（5 列一次跑完） | `tests/` 底下，檔名自訂 |

## 任務卡

| 卡 | 做什麼 | 完成證據 |
|---|---|---|
| T1 重啟實驗 | 先確認 Lab 是綠燈、剩餘時間夠（`verify-aws.sh` 成功不代表 EC2 指令一定能用）；主機若已經在跑就直接用，停止的才 Start；送 1 筆事件 → `sudo systemctl restart inspection` → 再查 | 重啟前後的筆數 |
| T2 建資料庫 | 請 Copilot 依下方五條寫 `db-up.sh`，自己（或請組員）審過後執行。**資料庫開始建立後要等好幾分鐘**，這段時間繼續聽 C 段 | 讀回 `available`、`PubliclyAccessible=false`；Codespace 連 5432 連不到 |
| T3 存進資料庫 | 請 Copilot 改程式存資料庫、實作冪等規則；`deploy.sh` 部署；跑冪等矩陣 5 列 | 矩陣結果、在 EC2 上查到的筆數 |
| T4 保留 | 主機與 RDS 都停止；寫保留清單 | 保留清單 |

**你的主機在 W4 T4 已經回收了？** 先用你 W3 的 `deploy/up.sh` 重建一台、用 `deploy.sh` 部署你 W4 的版本（權杖沿用 `.local/app.env`），`/health` 200 之後再從 T1 開始。

T2 檢查 Codespace 連不到 5432（加上逾時上限，不會一直等）：

```bash
timeout 8 bash -c 'echo > /dev/tcp/<你的 RDS 位址>/5432' && echo "連得到：錯了，立刻檢查" || echo "連不到：符合設計"
```

## `db-up.sh` 規格（五條）

1. 在主機所在的 VPC 建 2 個私有子網：選和既有子網**不重疊**的兩段 `/24`，放在**兩個不同 AZ**；兩個子網都要**明確關聯**到一張只有 `local` 路由的新路由表（不關聯的話會用到有 IGW 路由的主路由表，就變成公有子網）。
2. 建 DB 子網群組，以及 SG-db：入站只有一條 TCP 5432，來源＝**主機現在用的 SG**。
3. 建 RDS：PostgreSQL、`db.t3.micro`、儲存 20 GiB gp3、不公開、儲存加密、單一 AZ、初始資料庫 `inspection`。
4. 密碼由腳本產生、寫進 `.local/db.env`（600），不顯示，也不能出現在命令列參數。
5. 每建一項就把 ID 寫進 `.local/resources.json`；結束時讀回 `available` 與 `PubliclyAccessible=false`。

`deploy.sh` 本週多一件事：把 `.local/db.env` 的內容一起放進主機的秘密檔，放完再重啟服務。

## 冪等規則

- 一張 `events` 表：契約的欄位＋`received_at`；`event_id` 是主鍵；所有 SQL 用參數化查詢。
- 連線用 `sslmode=verify-full`，憑證檔 `/etc/inspection/rds-ca.pem`。
- 重複交給資料庫的主鍵判斷，不靠程式「先查再寫」。
- `/health` 多回報 `db_configured`（true／false）。資料庫的秘密還沒放上主機時，服務照樣啟動、`/health` 回 200 並回報 false，不要直接當掉（主機壞了用 `up.sh` 重建時才檢查得過）。

| 情況 | 回應 |
|---|---|
| 新的 `event_id` | 201 |
| 同一個 `event_id`、內容相同 | **200**，不新增 |
| 同一個 `event_id`、內容不同 | 409 |

### 冪等矩陣（T3，請 Copilot 寫成一支小腳本一次跑完）

腳本的輸出要能直接貼進繳交範本：開頭印出主機 `/health` 的 `version` 和 `db_configured`；每一列印出編號、HTTP 狀態碼、服務回應的本文；第 5 列印出 EC2 上 `psql` 查到的筆數。不要印出權杖、密碼或請求標頭。

| # | 做什麼 | 預期 |
|---|---|---|
| 1 | 送一筆新事件 | 201 |
| 2 | 原樣重送 | 200，不新增 |
| 3 | 同 ID、`note` 不同 | 409 |
| 4 | `sudo systemctl restart inspection` 後查 #1 | 還在 |
| 5 | 在 EC2 上用 `psql` 查 #1 的筆數 | 1 |

第 5 列在 EC2 上這樣查（密碼從秘密檔讀進環境變數，不出現在命令列；`<你的 event_id>` 換成 #1 的值）：

```bash
sudo bash -c 'set -a; . /etc/inspection/app.env; set +a
  PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \
    -v event_id="<你的 event_id>"' <<'SQL'
SELECT count(*) FROM events WHERE event_id = :'event_id';
SQL
```

## 審查 Copilot 的程式：三件事

Copilot 寫的程式通常能跑，但常漏掉下面這些。commit 前逐條看；沒過就請它改，並記下是哪一條。

| 看什麼 | 為什麼 | 怎麼看 |
|---|---|---|
| 1. SQL 用參數化查詢，不用 f-string 或字串拼接 | 事件內容是別人送來的文字，拼進 SQL 可能被資料庫當成指令執行 | 在程式裡找 `f"`、`+`、`%` 組 SQL 的地方；應該是 `cur.execute("… %s …", (值,))` |
| 2. 重複交給主鍵判斷，不靠「先查再寫」 | 兩個請求幾乎同時到，可能都查到「沒有」而寫成兩筆 | 看寫入的程式：應該是直接寫入、遇到主鍵衝突再決定回 200 或 409 |
| 3. 日誌和錯誤回應不印出連線字串或密碼 | 日誌很多人看得到、也留很久；密碼外流，別人就能連你的資料庫 | 找 `print`、`logging` 和錯誤回應；連線失敗時只說「哪一類問題」，不印整個連線字串 |

## 常見狀況

- **服務連不到資料庫**：先分類——逾時（SG、路由）、密碼錯（秘密檔）、憑證驗證失敗（憑證檔、名稱）、SQL 錯（程式）。不要把 `sslmode` 改成 `disable`。
- **部署後健康頁 502**：nginx 還在、服務沒起來（W3 學過）。看服務日誌 `sudo journalctl -u inspection -n 30`，找出是連不到、密碼、憑證還是 SQL 的問題，再照上一條處理。
- **RDS 一直在建立中**：正常要好幾分鐘（老師實測約 7.5 分鐘）；不要重跑 `db-up.sh`，會建出第二台。
- **Codespace 連得到 5432**：錯了，立刻停下檢查 `PubliclyAccessible` 與 SG-db 來源。
- **同一個點卡了超過 10 分鐘**：先問 AI 或隔壁同學；還是不行，記下最後成功的那一步與錯誤訊息，再找老師。

## 交付（TronClass，每個人都交）

照 [繳交範本](../../reports/W05_繳交範本.md) 填寫，範本第一段寫了怎麼複製、怎麼下載。上傳填好的 `.md` 一份即可。

內容：T1 重啟前後筆數、`db-up.sh` 審查紀錄與讀回、冪等矩陣 5 列、Copilot 審查三件事、保留清單、一段自己的話。

每個人都要能說出：為什麼 Codespace 連不到資料庫，EC2 卻連得到。

上傳前檢查：不能有密碼、權杖、連線字串、`.local/` 裡的任何內容、私鑰或完整帳號 ID。

## 保留到下週

主機與 RDS 都停止；私有子網、路由表、子網群組、SG-db 保留。**RDS 停止最多 7 天會被 AWS 自動啟動**並恢復計費，費用依當期官方計價頁。W6 開場先啟動 RDS（要幾分鐘），再啟動主機。
