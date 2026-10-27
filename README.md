# LINE 長輩圖智慧自動回覆機器人 (line-grandma-bot)

[![Version](https://img.shields.io/badge/version-2.2.0-blue.svg)](file:///C:/Users/user/code/line-grandma-bot/auto_reply.py)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6.svg)](https://microsoft.com)
[![Python](https://img.shields.io/badge/python-3.10%2B-green.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

專為 Windows LINE 個人電腦版設計的**長輩圖智慧自動回覆守護程式**。  
當長輩或親友傳來長輩圖時，程式會以真人般隨機延遲時間，自動回傳一張**不重複、高畫質的正統台灣長輩圖**；對方傳文字訊息時保持沈默，**維持對話未讀狀態不秒讀**。

---

## 🌟 核心特色

### 1. 獨立「百輪雙向記憶庫」
* 為每位監控好友維護各自獨立的雙向歷史紀錄（預設 100 輪）。
* **保證 100 輪內絕不重複發送同一張圖**給該對象。
* **絕不回送對方曾傳來過的圖片**給對方（防失禮尷尬機制）。

### 2. 跨好友「借花獻佛」大循環
* 好友 A 傳來的正統長輩圖，在 A 的百輪記憶中被封印（不回發給 A）。
* 系統會自動將該圖收納進中央圖庫，日後「借花獻佛」流通發送給好友 B 或好友 C。

### 3. Windows 原生 OCR 真偽鑑別（非長輩圖不收）
* 整合 Windows 10/11 內建繁體中文 OCR 引擎（`Windows.Media.Ocr`）。
* 自動辨識並核對「早安、平安、喜樂、祝福、健康、美好」等核心問候關鍵字。
* 若對方傳送的是生活隨拍照、寵物照、發票或收據，自動拒絕入庫，確保圖庫血統純正。

### 4. 台塑貼圖酷官方圖庫爬蟲（杜絕雜圖）
* 連線台灣 Formosa「台塑貼圖酷」官方長輩圖專案，精準收錄上千幅高解析度每日問候與插畫。
* 具備連線熔斷保護、MD5 指紋去重、單檔 8MB 記憶體上限防護。
* 支援 7 天 TTL 自動過期清理與發送後自動補新圖機制。

### 5. 高 DPI 縮放校正與穩定性防護
* 啟用 Windows 原生 Per-Monitor V2 DPI 感知（`SetProcessDpiAwareness`），徹底解決筆電 125%/150% 螢幕縮放時的滑鼠座標漂移問題。
* 全面採用 `tempfile` + `os.replace` 原子寫入（Atomic Write），杜絕斷電或當機導致 JSON 資料檔損毀。
* 全域例外捕獲與自動退避復原，滑鼠甩至角落觸發防護時守護程式永不閃退中斷。

### 6. 背景動態名單熱更新（Hot Reload）
* 守護程式在背景執行期間，手動修改 `config.json` 新增或移除好友名單，**秒級自動生效**，無需手動重啟程式。

---

## 📁 專案檔案結構

```text
line-grandma-bot/
├── auto_reply.py        # 核心自動回覆守護進程 (v2.2.0)
├── crawl_images.py      # 台塑貼圖酷長輩圖抓取與過期清理模組
├── config.example.json  # 設定檔範本
├── config.json          # 個人實際設定檔 (已列入 .gitignore 保護隱私)
├── contact_history.json # 每位聯絡人百輪記憶歷史庫 (已列入 .gitignore)
├── chat_dirs.json       # LINE 聊天室快取雜湊對應檔 (已列入 .gitignore)
├── run_auto_reply.bat   # Windows 一鍵啟動批次檔
├── requirements.txt     # Python 相依套件清單
├── README.md            # 本專案說明文件
└── images/              # 正統長輩圖庫 (內建 20 張高解析度長輩圖備用)
```

---

## 🚀 快速開始

### 1. 系統需求
* 作業系統：Windows 10 / Windows 11 (64 位元)
* Python 版本：Python 3.10 或以上（建議 Python 3.12+）
* 電腦版 LINE：保持登入並正常運作

### 2. 安裝相依套件
在專案根目錄打開 PowerShell，執行：
```powershell
pip install -r requirements.txt
```

### 3. 設定監控名單
首次使用時，請複製 `config.example.json` 為 `config.json`：
```powershell
copy config.example.json config.json
```
使用任何文字編輯器打開 `config.json`，在 `targets` 陣列填入欲監控回覆的好友 LINE 顯示名稱：
```json
{
  "targets": [
    "媽媽",
    "爸爸",
    "大舅舅"
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

#### ⚙️ 設定參數詳解
| 參數名稱 | 預設值 | 說明 |
| :--- | :---: | :--- |
| `targets` | `["媽媽"]` | 監控回覆的好友名稱陣列（支援多位好友，需與 LINE 上顯示名稱完全相符）。 |
| `images_dir` | `"images"` | 長輩圖存放資料夾路徑。 |
| `trigger_mode` | `"photo_only"` | `"photo_only"`（僅對方發照片時回覆長輩圖；對方傳文字不回覆且維持未讀）；`"all"`（文字與照片皆回）。 |
| `min_delay_seconds` | `5` | 模擬真人延遲回覆**最短秒數**。 |
| `max_delay_seconds` | `25` | 模擬真人延遲回覆**最長秒數**（每次會在此區間內隨機擲出秒數）。 |
| `cooldown_seconds` | `30` | 防洗版全域冷卻安全間隔（秒）。 |
| `history_rounds` | `100` | 每位好友獨立記憶輪數（100 輪內不發送相同圖片，也不把對方曾傳給你的圖片回傳）。 |
| `collect_incoming_images` | `true` | 是否透過原生 OCR 自動辨識並收納對方傳來的長輩圖進入圖庫（借花獻佛）。 |
| `replace_after_send` | `true` | 圖片送出後是否立即從本機淘汰，並自動從網路抓取全新長輩圖遞補。 |
| `image_ttl_days` | `7` | 網路抓取的長輩圖保存期限（天），過期自動清理換新以保持圖庫新鮮。 |
| `min_image_pool` | `10` | 圖庫最低儲備數量，低於此數量時自動連網補充。 |
| `auto_crawl_images` | `true` | 是否允許圖庫不足時自動連網下載補充。 |
| `close_window_after_send` | `true` | 發送完成後是否自動將對話框最小化至工作列，維持後續未讀狀態。 |

---

## 💻 常用執行指令

### 一鍵啟動（最推薦）
直接雙擊執行目錄下的 **`run_auto_reply.bat`** 即可在背景常駐監聽。

### 命令列常用指令
```powershell
# 1. 啟動常駐自動回覆監聽 (圖庫不足會自動連網補圖)
python auto_reply.py

# 2. 查看當前程式版本號
python auto_reply.py --version

# 3. 馬上連網抓取 20 張正統長輩圖進圖庫備用
python auto_reply.py --crawl 20

# 4. 手動測試發送一次給指定聯絡人 (驗證發送流程與最小化動作)
python auto_reply.py --test "媽媽"

# 5. 查看現有圖庫清單與每張圖在各位好友百輪記憶庫中的使用狀態
python auto_reply.py --list-images

# 6. 掃描當前桌面上所有 LINE 視窗控制代碼與標題
python auto_reply.py --scan-windows

# 7. 清空所有聯絡人的百輪歷史記憶
python auto_reply.py --clear-history
```

---

## 🛡️ 隱私與安全說明
1. **本機原生操作**：本工具採用純桌面 GUI 與 Windows 內建輔助功能自動化（Win32 API 與 UIAutomation），**不攔截、不破解、不篡改 LINE 通訊協定與封包**。
2. **好友個資保護**：個人的聯絡人名單（`config.json`）、聊天歷史記錄（`contact_history.json`）及頻道指紋檔（`chat_dirs.json`）均已由 `.gitignore` 全面排除，**絕不會上傳或同步至 GitHub 公開儲存庫**。
3. **安全防循環**：內建快取時間戳記前推機制與視窗標題雙重校驗，**保證絕不觸發自己送出的圖片形成死循環**。

---

## 📄 授權條款
本專案採用 [MIT License](LICENSE) 授權。抓取自網路之長輩圖版權歸原作者所有，僅供個人私人通訊娛樂與親友問候使用。
