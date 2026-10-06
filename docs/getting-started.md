# 開始使用

1. 接受教師 Classroom 邀請或從本 template 建立自己的作業 repo，再開 2-core Codespace。
2. 等 creation log 出現 `Environment check passed` 與 `Next: bash scripts/set-learnerlab-credentials.sh`，再執行 `bash .devcontainer/scripts/verify-environment.sh`。SSH 可連／Available 並不單獨證明套件已裝好。
3. AWS Academy Start Lab 變綠後，在 AWS Details 找本次三個憑證值。
4. 在自己的 terminal 執行 `bash scripts/set-learnerlab-credentials.sh`，填帳號、region 與三個隱藏值。不要貼到聊天。
5. 執行 `bash scripts/verify-aws.sh`；身分比對不能單靠 STS 證明帳號屬於 Academy，需自行核對 console。
6. 選擇 [OpenCode Terminal Agent](opencode.md)（執行 `opencode`，預設 Big Pickle），或 VS Code 登入自己的 Copilot 並開 Agent。請它讀 AGENTS.md 及當週 Lab，先提出實作與驗收計畫；同一時間先用一個 Agent。
7. 完成基本自查：`bash scripts/validate.sh`。這不是雲端作品已完成的證明。

Learner Lab 重啟／ExpiredToken 時重做第 4–5 步；Codespace 重啟後外連 IP 可能改變。
每次只 stage 自己檢查過的檔案，先看 git diff，再 python3 scripts/verify-repo.py、commit、push。
停 Codespace／關 browser 都不會清除 AWS；先依資源清單清除並驗證再 End Lab。
跨週保留程式和去識別化證據，資料備份在核准位置；不要假設主機持續存在。

## 每週更新教材：同一份 repo 持續累積

只在開始時建立一次自己的 repo，不必每週重新 Fork 或建立 Codespace。
每週在原 repo 同步教師的新教材，保留自己的程式與作業版本。
Codespace 重啟不會自動同步教師 repo。合併會保留學生已提交的實作；老師與學生修改同一段時可能衝突。即使自動合併成功，也要確認前一週功能仍正常。繳交時記下的完整 commit 網址可保留當週評分版本，不受後續週次修改影響。

### 如果你是從教師 repo 按 Fork 建立

以下流程假設作業持續放在自己的 main。若目前在作業分支，先請 Agent 核對分支與未提交修改，不要直接切換或丟棄修改。

1. 先看 git status，逐一檢查並 commit 自己的程式修改。不要提交 .local/、秘密或填了姓名學號的報告。
2. 第一次同步先檢查遠端：

```bash
git remote -v
```

origin 應是自己的 repo；upstream 應是教師 repo。若 upstream 尚不存在，才執行：

```bash
git remote add upstream https://github.com/ENL1217/yuntech-cloud-student-starter.git
```

3. 確认工作目錄乾淨後，每週執行：

```bash
git switch main
git pull --ff-only origin main
git fetch upstream
git merge upstream/main
```

任一指令失敗就先處理，不繼續貼下一行。若發生合併衝突，請 Agent 說明衝突並協助保留「自己的實作＋教師的新要求」，由本人審查；不要用 reset --hard、強制推送或整份覆蓋來處理。

4. 檢查差異、完成合併，依 README 驗證後推回自己的 repo：

```bash
git push origin main
```

只推自己的 origin，不推教師 upstream。教師更新教材不會自動部署到 EC2，仍須依當週步驟部署與驗證。

GitHub 網頁的 Sync fork 只更新 GitHub 上的 fork；原 Codespace 還要同步自己的 origin。若網頁提示衝突或捨棄提交，不要選捨棄，改用上述合併方式。

### 如果你使用 Use this template 或 Classroom 建立

這些 repo 不一定與教師 repo 有共同 Git 歷史，不要直接套用上述 merge，也不要使用 --allow-unrelated-histories 強行合併。
先由教師確認課程採用的更新方式；可從教師當週發布包逐檔比較並合併教材與打包器更新，保留自己的實作。
已開始做作業者不必為了改成 Fork 重建 repo。新同學依教師指定的建立方式操作。

## 繳交與評分版本

依當週範本填個人資料或全體組員資料，並提供自己的 repo 網址、已 push 的完整 commit 網址，以及 /health version（適用時）。
實測截圖仍須繳交；只有程式碼不能證明雲端測試成功。
填好的報告放 .local/，連同截圖交 TronClass；公開 GitHub 只保留空白範本，不放學號姓名。

同步方法參考：https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/syncing-a-fork
