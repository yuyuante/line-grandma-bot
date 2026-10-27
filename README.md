# line-grandma-bot

LINE 自動回覆長輩圖工具。

---

## Windows 個人帳號 1 對 1 自動回覆（長輩圖智慧互傳）

### 核心特性
- **各對象獨立「百輪雙向記憶庫」**：為每位聯絡人建立獨立歷史記憶（預設 100 輪），**保證 100 輪內絕不重複發送同一張圖給該對象，也絕不把該對象曾送過的圖回送給對方**。
- **跨好友借花獻佛大循環**：好友 A 傳來的長輩圖，在 A 的百輪記憶中被封印（不回發給 A），但會流通借花獻佛發送給好友 B 或 C。
- **智慧自動保鮮**：若某好友的百輪記憶幾乎吃滿現有圖庫，系統會自動連網抓取全新長輩圖，確保永遠有新圖可用。
- **自然隨機延遲**：每次回覆隨機在設定範圍（例如 5～25 秒）內挑選秒數等待，絕不使用死板固定頻率，宛如真人手動回覆。
- **維持未讀防秒讀**：平時視窗保持最小化或關閉，訊息維持未讀狀態；送出後自動最小化至工作列。
- **定期汰舊換新**：送出後自動淘汰換新，且超過 7 天未使用的舊圖自動清理，保持圖庫新鮮。

---

### 設定檔：config.json（可複製 config.example.json）

```json
{
  "targets": [
    "林如婷",
    "媽媽"
  ],
  "images_dir": "images",
  "trigger_mode": "photo_only",
  "min_delay_seconds": 5,
  "max_delay_seconds": 25,
  "cooldown_seconds": 30,
  "history_rounds": 100,
  "replace_after_send": true,
  "collect_incoming_images": true,
  "image_ttl_days": 7,
  "min_image_pool": 10,
  "auto_crawl_images": true,
  "close_window_after_send": true
}
```

| 參數 | 說明 |
| :--- | :--- |
| `targets` | 監控回覆的好友名單陣列（可填入多位，例如 `["林如婷", "媽媽"]`）。 |
| `images_dir` | 長輩圖資料夾路徑（預設 `images`）。 |
| `trigger_mode` | `"photo_only"`（只在對方傳照片/長輩圖時回覆）或 `"all"`（文字與照片皆回）。 |
| `min_delay_seconds` | 隨機回覆延遲**下限**（秒）。 |
| `max_delay_seconds` | 隨機回覆延遲**上限**（秒）。每次會在此區間內隨機擲出秒數。 |
| `history_rounds` | 每位好友獨立歷史記錄記憶輪數（預設 `100` 輪不重複）。 |
| `collect_incoming_images` | 是否自動辨識收納對方發送的正統長輩圖進入圖庫借花獻佛（預設 `true`）。 |
| `replace_after_send` | 圖片送出後是否立即淘汰，並自動從網路抓取 1 張全新圖片遞補（預設 `true`）。 |
| `image_ttl_days` | 網路抓來的圖片最長存放天數（預設 `7` 天，逾期自動清理換新）。 |
| `min_image_pool` | 圖庫最低儲備數量（低於此數量自動連網補充，預設 `10` 張）。 |
| `auto_crawl_images` | 是否允許自動連網補充長輩圖（預設 `true`）。 |
| `cooldown_seconds` | 防洗版冷卻時間（秒）。 |
| `close_window_after_send` | 發送完成後是否自動最小化對話框（預設 `true`）。 |

---

### 啟動指令

```powershell
# 1. 雙擊 run_auto_reply.bat 一鍵常駐啟動

# 2. 或在終端機中常駐啟動 (庫存不足會自動連網抓圖)
python auto_reply.py

# 3. 馬上上網抓取 20 張長輩圖儲存備用
python auto_reply.py --crawl 20

# 4. 手動測試發送一次
python auto_reply.py --test 林如婷

# 5. 查看目前圖庫清單與發送狀態
python auto_reply.py --list-images

# 6. 清空已發送圖庫歷史記憶
python auto_reply.py --clear-history
```
