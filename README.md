# LINE 長輩圖自動回覆

對方傳長輩圖來時，用**你自己的 LINE 帳號**回一張不一樣的長輩圖；對方傳文字時不回應，也**不會顯示已讀**。

## 運作方式

1. 監看 Windows 通知：只看通知，不打開聊天室，所以文字訊息會一直保持未讀。
2. 通知顯示「某某 傳送了照片」時，隨機等 1~8 分鐘，再打開電腦版 LINE 的聊天室。
3. 截取對方傳來的最新一張圖，用本機 CLIP 模型判斷是不是長輩圖。
4. 是的話，從圖庫挑一張和收到的圖不一樣、最近也沒傳過的長輩圖，貼上送出。
5. 關閉聊天室並把 LINE 縮小，恢復你原本正在用的視窗。

**已知的副作用**：一打開聊天室，LINE 就會把整串訊息標成已讀。所以：

- 對方先傳文字、再傳長輩圖，回圖時那段文字也會變成已讀。
- 對方傳的是一般照片時，照片會被已讀，但程式不會回。

## 安裝（在 Windows 上，不是 WSL）

1. 從 python.org 安裝 **Windows 版 Python 3.12**，安裝時勾選「Add python.exe to PATH」。
2. 打開 PowerShell，進到這個資料夾（WSL 裡的路徑是 `\\wsl$\<發行版名稱>\home\yuyuante\code\line-grandma-bot`）。建議把整個資料夾複製到 Windows 本機磁碟，例如 `C:\line-grandma-bot`，速度和相容性都比較好。
3. 安裝套件：
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. 電腦版 LINE 的設定：
   - 保持登入，並開啟「通知」。
   - 開啟通知的「顯示訊息內容」。
5. Windows 的設定：
   - 「通知」裡要允許 LINE 通知。
   - 專注模式（勿擾）不能擋掉 LINE。
   - 電源設定改成「不睡眠」。螢幕可以關，但**不能鎖定**。
6. 依照 [templates/README.md](templates/README.md) 截兩張介面截圖。
7. 編輯 `config.yaml`，把 `friend_name` 改成對方的 LINE 顯示名稱。

## 測試步驟（請照順序做）

```powershell
# 1. 單元測試
pytest

# 2. 建立圖庫（第一次會下載約 600MB 的 CLIP 模型）
python -m grandma.crawler --count 100
#    建好後到 data\library\ 抽查，確認都是長輩圖

# 3. 調整長輩圖判斷的門檻（選用）
#    在 tests\samples\grandma\ 放 20 張長輩圖、tests\samples\other\ 放 20 張其他圖片
pytest tests\test_classifier.py -s

# 4. 測試通知監看：請朋友或家人用另一個帳號傳文字和照片給你
#    （先暫時把 friend_name 改成那個人）
#    確認只有照片會顯示「照片!」，而且對方那邊沒有出現已讀
python -m grandma.notify_watch --verbose

# 5. 測試畫面操作（dry_run: true 不會真的送出）
#    確認 LINE 聊天室有打開、data\debug\ 裡截到的圖正確、聊天室有關掉
python main.py --once

# 6. 在 config.yaml 把 dry_run 改成 false，用測試對象再跑一次，確認真的有送出
python main.py --once

# 7. 把 friend_name 改回真正的對象，開始常駐執行
python main.py
```

## 日常使用

- 啟動：`python main.py`。記錄會寫進 `data\bot.log`。
- 停止：在資料夾裡建立名為 `STOP` 的檔案，或把滑鼠快速移到螢幕左上角。
- 每天自動補充圖庫：用 Windows「工作排程器」新增一個每日工作，執行 `C:\line-grandma-bot\.venv\Scripts\python.exe -m grandma.crawler --count 50`，「開始位置」設成專案資料夾。

## 常見問題

| 狀況 | 處理方式 |
|---|---|
| 通知監看沒有反應 | 用 `--verbose` 看通知內容，把實際的文字加進 `photo_keywords`，或調整 `line_app_id_contains` |
| 「畫面上找不到 search_box / input_box」 | 重新截模板，或調低 `ui.match_threshold` |
| 截到的圖不對 | 看 `data\debug\` 的截圖，調整 `ui.chat_header_px`、`ui.chat_left_px` |
| 貼上後沒有送出 | LINE 可能會跳出預覽視窗，把 `ui.send_keys` 改成 `["enter", "enter"]` |
| 聊天室開在主視窗裡、不是獨立視窗 | 把 `ui.chat_left_px` 設成左側好友列表的寬度 |

## 注意

- 自動操作 LINE 用戶端屬於灰色地帶。這個程式不碰通訊協定、操作節奏也像真人，被偵測的風險低，但仍然不是零。
- 從網路抓的長輩圖版權歸屬不明，僅限私人聊天使用。
