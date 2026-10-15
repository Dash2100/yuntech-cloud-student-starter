# W6 Sprint：多個模擬設備回報與設備篩選

> 本頁的「步驟 1～8」和繳交範本的「第 1～8 段」**編號一一對應**：做完步驟 N，就把它要你保留的輸出貼到範本第 N 段。

## 這週要做什麼

Codespace 裡的一個**模擬閘道**代表 10 個設備送事件；事件經 **AWS IoT Core** 轉給主機上的 **consumer**，consumer 再交給 W5 的 API 存進資料庫。別人的閘道不能發到你的主題；顯示頁可以依設備篩選。

**每個人在自己的 Learner Lab 帳號做一套**（接在你 W5 的主機與資料庫上），各自繳交。可以和組員討論、互相審查程式，但不交換憑證或私鑰。

本週的「10 個設備」是**一個閘道身分下的 10 個設備編號**，不是 10 張獨立的設備憑證。

## 先懂這些名詞

| 名詞 | 一句話 |
|---|---|
| 訊息中介（broker）、AWS IoT Core | 收下訊息再轉給有登記的接收者；IoT Core 是 AWS 代管的 broker |
| 發布／訂閱（pub/sub） | 發布者只交給 broker，訂閱者向 broker 登記要收哪一類；雙方不必知道對方位址 |
| MQTT、主題（topic） | 設備常用的發布／訂閱協定；訊息的分類字串，例如 `cxe/g03-m2/gw/events` |
| QoS 1、PUBACK | broker 至少送一次（可能重複）；PUBACK 是 broker 說「我收到了」，不代表資料庫存了 |
| X.509 憑證、CA | 證明「這把公鑰屬於誰」的電子證明，私鑰不離開持有者；CA 是簽發憑證、被事先信任的單位 |
| 雙向 TLS | 連線時雙方都出示憑證、互相檢查 |
| 原因碼（reason code） | MQTT 回覆裡說明結果的數字；0＝成功，135＝未授權 |
| 聲稱（claim） | 訊息內容自己說的事（例如 `device_id`），沒有被憑證證明 |

## 路徑

```text
模擬閘道（Codespace，用戶端 gw-g03-m2）──8883──▶ IoT Core：cxe/g03-m2/gw/events ──▶ consumer（主機上，用戶端 consumer-g03-m2）
  只能「發布」這個主題                                                          只能「訂閱」這個主題
                                                                                │ 檢查 device_id 在你的設備清單
                                                                                ▼
                                                                  API 127.0.0.1:8080 ──▶ RDS
```

## 步驟 0：開工前（約 5 分鐘）

### 0-1 在哪裡跑

閘道、`iot-up.sh`、`mqtt_check.py`、對帳腳本**都在 Codespace 跑**。

在 Codespace 先裝好 MQTT 函式庫（版本要固定，才拿得到 MQTT 5 的原因碼）：

```bash
python3 -m pip install --user paho-mqtt==2.1.0
python3 -c "import paho.mqtt; print(paho.mqtt.__version__)"     # 要印 2.1.0
```

### 0-2 先定好名字（全部用你的代號，不用學號姓名）

**你的代號**＝組名-組內代號，例如 `g03-m2`。所有腳本都照這張表取名，分開叫 Copilot 寫的程式才找得到彼此的檔案。

| 東西 | 規則 | 例子（代號 `g03-m2`） |
|---|---|---|
| 主題 | `cxe/<代號>/gw/events` | `cxe/g03-m2/gw/events` |
| 閘道用戶端 ID | `gw-<代號>` | `gw-g03-m2` |
| consumer 用戶端 ID | `consumer-<代號>` | `consumer-g03-m2` |
| 兩份政策名稱 | `cxe-<代號>-gw`、`cxe-<代號>-consumer` | `cxe-g03-m2-gw` |
| `.local/iot/` 裡的檔案 | `endpoint`、`AmazonRootCA1.pem`、`gw.cert.pem`、`gw.private.key`、`consumer.cert.pem`、`consumer.private.key` | — |
| `.local/resources.json` 新增的鍵 | `iot_gw_cert_id`、`iot_consumer_cert_id`、`iot_gw_policy`、`iot_consumer_policy` | — |
| 10 個設備編號（清單內） | `<代號>-d01` … `<代號>-d10` | `g03-m2-d01` |
| 不在清單的設備 | `<代號>-d99` | `g03-m2-d99` |
| 本次前綴 | `<代號>-r<第幾次>`，**每跑一次對帳就換一個** | `g03-m2-r1`、`g03-m2-r2` |
| event_id | `<本次前綴>-d<設備序號>-<流水號>` | `g03-m2-r1-d01-01` |

**為什麼前綴每次要換**：同一批 event_id 再送一次，W5 的冪等規則會讓它們全部變成「重複」，對帳就對不上。

### 0-3 本週要寫的程式

全部請 Copilot 依下方「規格」寫。**Copilot 交出第一版就先 commit**，再依「審查三件事」看，沒過就請它改、再 commit。

| 程式 | 用在哪一步 | 規格 |
|---|---|---|
| `deploy/iot-up.sh` | 步驟 2 | 規格 A |
| `tools/mqtt_check.py`（兩個身分的連線測試，越權也用它） | 步驟 2、6 | 規格 B |
| `app/consumer.py` | 步驟 3 | 規格 C |
| `app/service.py`：`GET /events?device_id=`＋顯示頁的篩選輸入框 | 步驟 3 | 規格 D |
| `deploy/deploy.sh`（W4 寫的，加一件事） | 步驟 3、4 | 規格 E |
| `tools/gateway_sim.py` | 步驟 5 | 規格 F |
| `tests/w06_reconcile.py`（對帳腳本） | 步驟 5 | 規格 G |

`deploy/make_user_data.py`（打包器）已經做好：commit 裡有 `app/consumer.py` 就一起裝上主機、建立 `inspection-consumer` 服務，並在主機上裝好 `paho-mqtt 2.1.0`。

---

## 步驟 1：T1 開場（約 15 分鐘）→ 範本第 1 段

先確認 Lab 是綠燈。`<RDS 識別名稱>`、`<主機 ID>` 在 `.local/resources.json`。

```bash
# 1. RDS 和主機同時啟動（RDS 要好幾分鐘，不必等它好再開主機）
aws rds describe-db-instances --db-instance-identifier <RDS 識別名稱> --query 'DBInstances[0].DBInstanceStatus' --output text
#   stopped → 執行下一行；starting 或 available → 跳過下一行（停滿 7 天會被 AWS 自動啟動）
aws rds start-db-instance --db-instance-identifier <RDS 識別名稱> --query 'DBInstance.DBInstanceStatus' --output text
aws ec2 start-instances --instance-ids <主機 ID> --query 'StartingInstances[0].CurrentState.Name' --output text

# 2. 等主機、查新位址，寫回 .local/resources.json（W3 up.sh 存位址的那個欄位），不然 deploy.sh 會連到舊位址
aws ec2 wait instance-running --instance-ids <主機 ID>
aws ec2 describe-instances --instance-ids <主機 ID> --query 'Reservations[0].Instances[0].PublicIpAddress' --output text

# 3. 核對 Codespace 出口；和 SG 裡的 /32 不同，就照 W4 的方式列舊／新再改 SG（22、80 各一條）
curl -s https://checkip.amazonaws.com

# 4. 等 RDS 好了再測
aws rds wait db-instance-available --db-instance-identifier <RDS 識別名稱>
curl -s --max-time 8 http://<新位址>/health
```

**保留到範本第 1 段**：新位址末段、`/health` 的回應（這時的 version 還是 W5 的，正常）。

---

## 步驟 2：T2 建立身分，兩個身分各連一次（約 40 分鐘）→ 範本第 2 段

1. 請 Copilot 依**規格 A** 寫 `deploy/iot-up.sh`，commit 第一版，依審查第 3 件事看過後執行。**它只跑一次**；跑一半失敗，修好後重跑時要跳過已建立的項目（規格 A 第 6 條）。
2. 讀回兩份政策（帳號自動遮成末四碼）：

   ```bash
   for p in cxe-<代號>-gw cxe-<代號>-consumer; do
     echo "== $p"
     aws iot get-policy --policy-name "$p" --query policyDocument --output text | sed -E 's/[0-9]{8}([0-9]{4})/********\1/g'
   done
   ```

3. 請 Copilot 依**規格 B** 寫 `tools/mqtt_check.py`。開兩個終端機，**先訂閱、再發布**（broker 不會替還沒訂閱的人留訊息）：

   ```bash
   # 終端機 1（先跑，會等 60 秒）
   python3 tools/mqtt_check.py --as consumer --sub cxe/<代號>/gw/events --wait 60
   # 終端機 2
   python3 tools/mqtt_check.py --as gw --pub cxe/<代號>/gw/events
   ```

   預期：終端機 1 印 `connack=0 suback=1`（1＝以 QoS 1 訂閱成功）和 `recv …`；終端機 2 印 `connack=0 puback=0`。原因碼小於 128 都是成功，135 是未授權。

**保留到範本第 2 段**：兩份政策、兩個終端機的輸出。

連不上時：`connack` 非 0 → 用戶端 ID 不在政策、憑證沒附政策、CA 或位址錯；`suback=135` → 訂閱的資源寫成了 `topic/`，要用 `topicfilter/`（規格 A 第 3 條）。**不要把政策改成 `*`。**

---

## 步驟 3：T3 寫 consumer、篩選與部署設定（約 50 分鐘）→ 範本第 3 段

1. 請 Copilot 依**規格 C** 寫 `app/consumer.py`。
2. 請 Copilot 依**規格 D** 改 `app/service.py`（篩選 API＋顯示頁的設備編號輸入框）。
3. 請 Copilot 依**規格 E** 改 `deploy/deploy.sh`。
4. 每一支都：第一版 commit → 依「審查三件事」看 → 沒過請它改、再 commit。

**保留到範本第 3 段**：審查三件事的確認方式，以及第一版、修正版的 commit 網址。

---

## 步驟 4：凍結版本並部署（約 15 分鐘）→ 範本第 4 段

**先把步驟 5 要用的 `tools/gateway_sim.py`、`tests/w06_reconcile.py`（規格 F、G）也寫好 commit**，再做下面的事。之後若改了任何程式，就從這一步重來，並在步驟 5 換新的前綴。

```bash
git status                      # 要乾淨，沒有未 commit 的修改
git push
git rev-parse HEAD              # 這就是要填在範本「基本資料」的 commit
./deploy/deploy.sh              # 部署到同一台主機
curl -s http://<新位址>/health   # version 要等於上面的 HEAD
```

部署後在主機上檢查權限與服務（`sudo sh -c` 讓萬用字元由 root 展開，一般使用者進不了 700 的目錄）：

```bash
sudo sh -c 'stat -c "%a %U %n" /etc/inspection /etc/inspection/app.env /etc/inspection/iot /etc/inspection/iot/*'
# 預期：755 root、600 root、700 inspection、三個 600 inspection
systemctl is-active inspection inspection-consumer       # 兩個都要 active
sudo journalctl -u inspection-consumer -n 5 -o cat       # 要看到 subscribed rc=1
```

**保留到範本第 4 段**：HEAD、`/health`、上面三個指令的輸出。

---

## 步驟 5：對帳（約 15 分鐘）→ 範本第 5 段

1. 決定本次前綴（第一次 `<代號>-r1`，之後每跑一次加 1）。
2. 送 32 則：`python3 tools/gateway_sim.py --code <代號> --run r1`
3. 對帳：`python3 tests/w06_reconcile.py --code <代號> --run r1`

| 位置 | 預期 |
|---|---|
| 閘道收到的 PUBACK | 32 |
| consumer 計數：建立／重複／拒絕／錯誤 | 30／1／1／0 |
| 資料庫（主機上 `psql` 查本次前綴） | 30 |
| `GET /events?device_id=<代號>-d01` 裡本次前綴的筆數 | 3 |

4. 撈 consumer 日誌（會有 3 行）當證據（範本第 10 段要用）：

   ```bash
   # 在主機上
   sudo journalctl -u inspection-consumer -o cat --no-pager | grep -E 'event_id=<本次前綴>-d(01-01|99-01) '
   ```

**保留到範本第 5 段**：對帳腳本的完整輸出、上面 3 行日誌。

---

## 步驟 6：越權 2 列（約 10 分鐘）→ 範本第 6 段

同一個用戶端 ID 同時只能一條連線，所以**對帳做完才做越權**，而且 consumer 身分測試前要先停主機上的 consumer，測完一定要啟動回來：

```bash
ssh … 'sudo systemctl stop inspection-consumer'                                  # 和 deploy.sh 同一把金鑰、同一個使用者
python3 tools/mqtt_check.py --as gw --pub cxe/<代號>-x/gw/events                  # 預期 puback=135
python3 tools/mqtt_check.py --as consumer --sub 'cxe/#' --wait 5                  # 預期 suback=135
ssh … 'sudo systemctl start inspection-consumer && systemctl is-active inspection-consumer'
```

被拒是因為**你自己的政策**只允許你的主題，和那個主題屬於誰無關（每個人在自己的帳號，碰不到同學）。「等不到訊息」不能當作被拒的證據，要看原因碼。

**保留到範本第 6 段**：四行指令的輸出。

---

## 步驟 7：篩選截圖（約 10 分鐘）→ 範本第 7 段

主機的 80 埠只放行 Codespace 的 `/32`，你的筆電瀏覽器直接開會逾時——**不要為了截圖把 SG 開成 0.0.0.0/0**。同 W3、W4：在 Codespace 用**私人轉發埠**開顯示頁。

在顯示頁貼 **operator 權杖**、在設備編號輸入框填 `<代號>-d01`、按讀取，看得到本次前綴的 3 筆後清空權杖框，截圖存成 `W06_篩選_<學號>.png`。

---

## 步驟 8：T4 保留（約 10 分鐘，可不等停好就下課）→ 範本第 8 段

```bash
aws ec2 stop-instances --instance-ids <主機 ID> --query 'StoppingInstances[0].CurrentState.Name' --output text
aws rds stop-db-instance --db-instance-identifier <RDS 識別名稱> --query 'DBInstance.DBInstanceStatus' --output text
# 之後再讀回（RDS 停好約 7 分鐘）
aws ec2 describe-instances --instance-ids <主機 ID> --query 'Reservations[0].Instances[0].State.Name' --output text
aws rds describe-db-instances --db-instance-identifier <RDS 識別名稱> --query 'DBInstances[0].DBInstanceStatus' --output text
aws iot describe-certificate --certificate-id <憑證 ID> --query certificateDescription.status --output text   # 兩張都 ACTIVE
```

IoT 憑證與政策保留（閒置不計費）。**RDS 停止最多 7 天會被 AWS 自動啟動**。

---

## 規格（給 Copilot 的需求）

### 規格 A：`deploy/iot-up.sh`

1. `aws iot describe-endpoint --endpoint-type iot:Data-ATS` 取得 broker 位址，存 `.local/iot/endpoint`。
2. 建兩張憑證（閘道、consumer），檔名照 0-2 的表，存 `.local/iot/`（目錄 700、檔案 600）。**建立憑證的指令會把私鑰印在輸出裡**：輸出直接寫進檔案，不顯示在終端機，只取憑證 ID。
3. 兩份政策，名稱照 0-2 的表，資源寫**精確字串**、不用 `*`。四種動作的資源前綴不同：

   | 身分 | 動作 | 資源 |
   |---|---|---|
   | 閘道 | `iot:Connect` | `arn:aws:iot:us-east-1:<帳號>:client/gw-<代號>` |
   | 閘道 | `iot:Publish` | `arn:aws:iot:us-east-1:<帳號>:topic/cxe/<代號>/gw/events` |
   | consumer | `iot:Connect` | `arn:aws:iot:us-east-1:<帳號>:client/consumer-<代號>` |
   | consumer | `iot:Subscribe` | `arn:aws:iot:us-east-1:<帳號>:topicfilter/cxe/<代號>/gw/events` |
   | consumer | `iot:Receive` | `arn:aws:iot:us-east-1:<帳號>:topic/cxe/<代號>/gw/events` |

4. 政策附到對應的憑證；下載 Amazon Root CA 1 到 `.local/iot/AmazonRootCA1.pem`。
5. ID 寫進 `.local/resources.json`（鍵名照 0-2 的表）；結束時讀回兩張憑證 `ACTIVE`。
6. 重跑時，`resources.json` 已有的項目一律跳過，不建第二張憑證、不覆蓋私鑰。

### 規格 B：`tools/mqtt_check.py`

- 參數：`--as gw|consumer`（決定用哪張憑證和哪個用戶端 ID）、`--pub <主題>` 或 `--sub <主題篩選>`、`--wait <秒>`（訂閱時等多久）。
- MQTT 5、QoS 1、8883、雙向 TLS（`paho-mqtt 2.1.0`，`CallbackAPIVersion.VERSION2`）；憑證檔、位址照 0-2 從 `.local/iot/` 讀。
- 印出：`connack=<原因碼>`；發布時 `puback=<原因碼>`（要等 PUBACK 回來，用訊息編號核對）；訂閱時 `suback=<原因碼>`，收到訊息時印 `recv topic=<主題> payload=<內容>`。
- 發布的測試訊息內容固定 `{"check":"t2","code":"<代號>"}`。

### 規格 C：`app/consumer.py`

- 設定從環境變數讀（deploy.sh 放進秘密檔，見規格 E）：`IOT_ENDPOINT`、`IOT_TOPIC`、`IOT_CLIENT_ID`、`IOT_DEVICE_IDS`、`IOT_CA`、`IOT_CERT`、`IOT_KEY`、`REPORTER_TOKEN`。
- MQTT 5、QoS 1、8883、雙向 TLS；訂閱成功後印一行 `subscribed rc=<原因碼>`。
- 收到一則：檢查 `device_id` 在 `IOT_DEVICE_IDS` 內 → **不在就計為拒絕、不送 API** → 在的話用 reporter 權杖 POST `http://127.0.0.1:8080/events` → 依回應分類：

  | API 回應 | 計為 |
  |---|---|
  | 201 | `created` |
  | 200（W5 冪等：同 ID 同內容） | `duplicate` |
  | 400、409 等 4xx | `rejected` |
  | 5xx、連不上 | `error` |

- 不自己寫資料庫；每則只印一行 `event_id=<event_id> result=<created|duplicate|rejected|error>`，不印權杖、不印整則訊息。

### 規格 D：`app/service.py` 篩選

- `GET /events?device_id=<設備編號>`：operator 權杖才能用；回傳該設備最新 50 筆，**依 `received_at` 由新到舊**。
- 顯示頁在權杖框旁多一個「設備編號」**輸入框**，有填就帶 `?device_id=`；事件內容仍用 `textContent`。

### 規格 E：`deploy/deploy.sh` 本週多一件事

1. 目錄 `/etc/inspection/iot` 用 `install -d -o inspection -g inspection -m 700` 建立。
2. consumer 的憑證、私鑰、Root CA 三個檔：先 `umask 077` 再寫入，寫完用 `chown inspection:inspection` 改擁有者，權限 600。consumer 用 `inspection` 帳號執行，**檔案屬於 root 它就讀不到、會一直重啟**。
3. **上一層 `/etc/inspection` 維持 755**。W5 有同學設成 700，服務就讀不到 `rds-ca.pem`；設成 700 的話 consumer 也進不了 `iot/`。`app.env` 本身是 root 600，systemd 會替服務讀。
4. 秘密檔 `/etc/inspection/app.env` 加上規格 C 的 `IOT_` 那幾行（`IOT_CA`、`IOT_CERT`、`IOT_KEY` 填 `/etc/inspection/iot/` 下的完整路徑）。
5. 放完 `sudo systemctl restart inspection-consumer`，印出 `systemctl is-active inspection inspection-consumer`。

### 規格 F：`tools/gateway_sim.py`

- 參數 `--code <代號> --run <r1、r2…>`；前綴＝`<代號>-<run>`，先用正規式 `^[A-Za-z0-9-]{1,40}$` 檢查。
- 用閘道身分連線（同規格 B），發布到 `cxe/<代號>/gw/events`，共 32 則：10 個設備各 3 筆，再原樣重送第 1 筆，再送 1 筆 `<代號>-d99`。
- 每則是 W4 契約的事件：`event_id`、`device_id`、`observed_at`（現在時間，帶時區）、`type`＝`status`。
- 每則都等 PUBACK 回來（用訊息編號核對）才送下一則；最後印 `puback_ok=<n>/32`，並把前綴與 PUBACK 數寫進 `.local/w06-last-run.json`。

### 規格 G：`tests/w06_reconcile.py`

在 Codespace 跑，一次印出範本第 5 段要的全部數字：

1. 開頭印：主機 `/health` 的 `version`、`db_configured`，本次前綴。
2. PUBACK 數：讀 `.local/w06-last-run.json`（前綴要和參數相同）。
3. consumer 計數與資料庫筆數：**經 SSH**（和 deploy.sh 同一把金鑰、同一個使用者）在主機上執行下面兩段，每 5 秒重試直到四個計數加起來 ≥ 32 或滿 60 秒。前綴先用正規式檢查再傳；`psql` 用 `-v` 傳值，**不要把前綴拼進 SQL 字串**。

   ```bash
   sudo journalctl -u inspection-consumer -o cat --no-pager \
     | grep -F 'event_id=<本次前綴>-' | grep -o 'result=[a-z]*' | sort | uniq -c
   ```

   ```bash
   sudo bash -c 'set -a; . /etc/inspection/app.env; set +a
     PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \
       -v prefix="<本次前綴>-"' <<'SQL'
   SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';
   SQL
   ```

4. 篩選：用 `.local/app.env` 的 operator 權杖打 `GET http://<主機>/events?device_id=<代號>-d01`，印出本次前綴的 event_id 與筆數。
5. 不印權杖、密碼、私鑰。

---

## 審查 Copilot 的程式：三件事

| 看什麼 | 為什麼 | 怎麼看 |
|---|---|---|
| 1. consumer 先檢查 `device_id` 在你的設備清單，才送 API | 閘道的憑證只證明「哪個閘道」，`device_id` 是訊息自己寫的，不檢查就什麼都收 | 看 consumer 收到訊息後的順序：檢查 → 不在清單就計為拒絕、不送 API |
| 2. 沒有關掉 TLS 檢查 | 關掉之後，任何冒牌的 broker 都會被當成真的 | `grep -rnE 'tls_insecure_set\(True\)\|CERT_NONE' app/ tools/ tests/`，應該沒有結果 |
| 3. `iot-up.sh` 不顯示建立憑證的輸出；政策沒有 `*` | 建立憑證的輸出裡有私鑰；`*` 會讓憑證能發、能收所有主題 | 看建立憑證那一步的輸出是否直接寫進檔案；讀回的政策裡找 `*` |

## 常見狀況

- **consumer 一直重啟**：`sudo journalctl -u inspection-consumer -n 30` 看原因。最常見：`/etc/inspection/iot/` 的檔案屬於 root、`/etc/inspection` 被設成 700、秘密檔少了 `IOT_` 那幾行、`consumer.py` 沒 commit 就部署。
- **PUBACK 32 但資料庫是 0**：consumer 沒在跑或沒訂閱成功（看有沒有 `subscribed rc=1`），或越權測試停掉後忘了啟動。
- **30 筆全部變成「重複」**：前綴沒換。換一個新的前綴重跑。
- **計數比 32 多**：QoS 1 可能送兩次；多出來的應該都落在「重複」，資料庫仍是 30。照實寫進報告。
- **`error` 不是 0**：API 回 5xx 或連不上，多半是 RDS 還沒 available。
- **部署時出現 `User data exceeds the course limit`**：程式太長（上限 16 KB）。請 Copilot 刪掉沒用到的函式與長註解，不要改打包器。
- **不小心把私鑰印出來**：停下來，撤銷那張憑證重建，並寫進報告第 9 段。

## 交付（TronClass，每個人都交）

照 [繳交範本](../../reports/W06_繳交範本.md) 填寫。上傳 `W06_繳交_<學號>.md` 和 `W06_篩選_<學號>.png`。沒做到的步驟，在那一段寫「未完成」和最後看到的錯誤訊息。

上傳前檢查：不能有私鑰、權杖、密碼、`.local/` 裡的任何內容或完整帳號 ID。
