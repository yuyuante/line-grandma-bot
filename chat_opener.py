"""
自動開啟好友的獨立聊天視窗：機器人所有判斷都以「標題剛好等於好友名稱的獨立聊天視窗」為前提，
視窗被關掉就停擺。這個模組在視窗不見時，從 LINE 主視窗的搜尋欄把它開回來。

做法：用 UIA 把好友名稱寫進主視窗的搜尋欄 (不經鍵盤與剪貼簿，不受輸入法影響) -> 對主視窗截圖，用 OCR 找「整列文字剛好等於好友名稱」的搜尋結果 -> 雙擊 ->
等標題剛好等於好友名稱的獨立視窗出現。LINE 的清單項目沒有名字可以用 UIA 讀，所以只能靠 OCR 找位置。

安全原則 (Fail-Closed)：
- 只接受「整列文字就是好友名稱」(忽略空白與大小寫，後面可接成員數與時間；名稱太長被列表截斷時，要前綴夠長且全搜尋結果只有一列符合)，
  不接受「包含」；找不到、分不出是哪一列都放棄。
- 主視窗裡是使用者真實的聊天清單：每一次點擊或貼上之前都核驗主視窗在前景，OCR 只在記憶體裡找名稱，
  不記錄也不輸出其他文字。
- 成功的唯一標準是出現標題剛好等於好友名稱的視窗；做完 (不論成敗) 都把搜尋欄清掉。
"""
import difflib
import re
import time

import image_kind
import ocr_engine

SEARCH_RESULT_WAIT = 1.5        # 寫入搜尋文字後等結果出現的秒數
WINDOW_APPEAR_TIMEOUT = 8.0     # 雙擊後等獨立聊天視窗出現的秒數
ALL_TAB_INDEX = 0              # 「全部」永遠是第一個分頁；官方帳號不在「好友」分頁，搜尋只找目前分頁


def _norm(text):
    """忽略空白與大小寫；OCR 常把繁體字讀成簡體 (理財 -> 理财)，所以兩邊都先轉成繁體再比"""
    return re.sub(r"\s+", "", image_kind.normalize_text(text or "")).casefold()


# 搜尋結果列後面可能接的東西：成員數 (只有群組才有)、最後一則訊息的時間或日期。OCR 常把整列讀成同一段文字
_COUNT = r"(?:[(（]\d+[)）]?)?"
_STAMP = r"(?:(?:上午|下午)?\d{1,2}[:：]\d{2}|昨天|星期.|\d{1,2}/\d{1,2})?"
# 名稱太長時列表會截成「前綴…」，OCR 讀出來是兩個以上的點或刪節號 (例如「服..．」)
_ELLIPSIS = re.compile(r"(?:[.．。·]{2,}|…+)")
MIN_TRUNCATED_PREFIX = 5        # 被截斷的名稱至少要露出這麼多字，才拿來當依據


def _tail(allow_count):
    return re.compile((_COUNT if allow_count else "") + _STAMP)


def _truncated_match(text, wanted, tail):
    """text (已正規化) 是不是「name 的開頭 + 刪節號 + (成員數) + 時間」；是回傳 True"""
    m = _ELLIPSIS.search(text)
    if not m:
        return False
    prefix = text[:m.start()]
    return (len(prefix) >= MIN_TRUNCATED_PREFIX and len(prefix) < len(wanted) and wanted.startswith(prefix)
            and tail.fullmatch(text[m.end():]) is not None)


def _dropped_tail_match(text, wanted, tail):
    """
    text (已正規化) 是不是「name 少了最後一個字 + (成員數) + 時間」：OCR 偶爾把名稱最後一個字吃掉
    (真機：「李月琴@全球人壽」被讀成「李月琴@全球人」，沒有刪節號)。最多只容許少一個字，露出的前綴一樣要夠長。
    """
    prefix = wanted[:-1]
    return (len(prefix) >= MIN_TRUNCATED_PREFIX and text.startswith(prefix)
            and tail.fullmatch(text[len(prefix):]) is not None)


# 搜尋結果分成幾個小標題區塊，標題後面接該區塊的筆數：「聊天 1」「訊息 1」「好友 2」…
_SECTION_HEADER = re.compile(r"(聊天|訊息|好友|群組|官方帳號|公開帳號)(\d+)")
SECTION_NAME_MIN_RATIO = 0.4        # 小標題底下那一列的文字，要跟名稱至少有這麼像 (SequenceMatcher)，擋掉明顯不是這位的列
SECTION_NAME_MIN_LEN = 6            # 名稱太短 (「媽媽」) 時，子字串搜尋容易冒出別的聊天室，不走這一招；OCR 讀歪的都是長名稱
SECTION_ROW_SAME_LINE = 14          # 同一行的字框，中心 y 差在這個範圍內 (名稱、時間在同一行)


def _section_row(below, wanted):
    """
    OCR 把名稱讀歪 (中間的字被吃掉、讀成別的字) 時的最後一招，信任 LINE 自己的搜尋：名稱已經整串打進搜尋欄，
    「聊天」小標題的筆數剛好是 1，就是 LINE 認定恰好一個聊天室的名稱符合。取那個小標題底下第一行、最左邊的字框 (名稱；
    同一行右邊的是時間，下一行是最後一則訊息的預覽)，而且文字要跟名稱有一定的相似度。回傳 (x, y) 或 None。
    沒有「聊天」小標題 (這個人沒有聊天記錄)、筆數不是 1、小標題底下沒有東西，都回傳 None。
    below: [(正規化文字, (x, y))]，已經去掉搜尋欄以上的字框。
    """
    if len(wanted) < SECTION_NAME_MIN_LEN:
        return None
    ordered = sorted(below, key=lambda b: b[1][1])
    headers = [(i, _SECTION_HEADER.fullmatch(text)) for i, (text, _pos) in enumerate(ordered)]
    headers = [(i, m) for i, m in headers if m]
    chat = [(i, m) for i, m in headers if m.group(1) == "聊天"]
    if len(chat) != 1 or int(chat[0][1].group(2)) != 1:
        return None
    start = chat[0][0] + 1
    nxt = next((i for i, _m in headers if i > chat[0][0]), len(ordered))
    section = ordered[start:nxt]
    if not section:
        return None
    first_y = section[0][1][1]
    line = [b for b in section if abs(b[1][1] - first_y) <= SECTION_ROW_SAME_LINE]
    text, pos = min(line, key=lambda b: b[1][0])
    if difflib.SequenceMatcher(None, text, wanted).ratio() < SECTION_NAME_MIN_RATIO:
        return None
    return pos


def find_result_rows(boxes, name, min_y, allow_count=False):
    """
    boxes: [(文字, (中心x, 中心y))]。回傳搜尋結果裡「整列文字就是 name」的位置 [(x, y)]，由上到下排序。
    min_y 以上 (搜尋欄所在的區域，要用搜尋欄的「底部」) 的文字一律忽略，避免把搜尋欄裡剛貼上的那串字當成結果。
    - 完整名稱：名稱本身，後面可以接最後一則訊息的時間 (「羅毓斌 上午9:20」)，其餘多一個字都不算。
    - allow_count=True (要開的是**群組**) 才接受名稱後面接成員數 (「理財幼幼班_Z (142)」)；
      一對一的對象不接受，因為「名稱 (12)」那一列是同名的群組，不是這位好友。
    - 名稱太長被列表截斷 (「東南里李武雄服..．（477）上午9:20」)：只有「露出的前綴夠長、確實是名稱開頭、
      整個搜尋結果裡剛好只有這一列」才採用；有兩列以上符合就分不出是哪個聊天室，一律不點。
      (雙擊後仍要等到標題符合的視窗才算成功。)
    - OCR 把名稱最後一個字吃掉 (「李月琴@全球人壽」讀成「李月琴@全球人」，沒有刪節號)：規則同上，最多少一個字，
      露出的前綴夠長、後面只能是 (成員數) 與時間、整個搜尋結果裡剛好只有一列 (與截斷的列合併計算)；有完整名稱的列就一律用完整的。
    - 以上都沒有一列 (OCR 把名稱中間的字吃掉或讀成別的字，例如「翁彦臻(公羽)凱基人壽/信託咨詢」讀成「翁彦臻(公羽)凱基人/..」)：
      信任 LINE 的搜尋，「聊天」小標題的筆數剛好是 1 時取它底下那一列 (見 _section_row)。
      沒有這個小標題 (沒有聊天記錄的人)、筆數不是 1 的一律不點。有兩列以上的截斷符合時仍視為分不出，不走這一招。
    """
    wanted = _norm(name)
    if not wanted:
        return []
    tail = _tail(allow_count)
    exact_pattern = re.compile(re.escape(wanted) + tail.pattern)
    below = [(_norm(text), (x, y)) for text, (x, y) in boxes if y > min_y]
    exact = sorted((pos for text, pos in below if exact_pattern.fullmatch(text)), key=lambda p: p[1])
    if exact:
        return exact
    truncated = sorted((pos for text, pos in below
                        if _truncated_match(text, wanted, tail) or _dropped_tail_match(text, wanted, tail)), key=lambda p: p[1])
    if len(truncated) == 1:
        return truncated
    if truncated:
        return []                                   # 兩列以上的截斷符合：分不出是哪個聊天室
    row = _section_row(below, wanted)
    return [row] if row else []


def _find_rows(ui, main, box, name, read_boxes, allow_count=False):
    """對主視窗截圖，找搜尋結果裡整列文字剛好等於 name 的位置 (螢幕座標)，由上到下"""
    shot, origin = ui.screenshot(main)
    rows = find_result_rows(read_boxes(shot), name, min_y=box[3] - origin[1], allow_count=allow_count)
    return [(origin[0] + x, origin[1] + y) for x, y in rows]


def _switch_tab(ui, main, index):
    """
    切到指定分頁。分頁列太窄時第一個分頁會被淡出遮罩蓋住點不到，所以只在切分頁的這一下把主視窗拉寬，
    切完立刻縮回原本的位置與大小 (寬版面下雙擊好友不會彈出獨立視窗，搜尋與雙擊一定要在原本的寬度做)。
    """
    wide_from = ui.ensure_wide(main)
    try:
        return ui.select_tab(main, index)
    finally:
        if wide_from is not None:
            try:
                ui.restore_rect(main, wide_from)
            except Exception:
                pass


def open_chat_window(name, ui, read_boxes=ocr_engine.read_boxes, log=lambda msg: None, sleep=time.sleep, allow_member_count=False):
    """
    開啟名稱為 name 的好友的獨立聊天視窗，成功 (視窗已存在或已開好) 回傳 True，任何一步沒把握回傳 False。
    一律先切到「全部」分頁再搜尋 (已經在「全部」就不動)，結束後把使用者原本的分頁切回去。
    ui 提供：find_chat_window(name)、main_window()、bring_to_front(hwnd)、is_front(hwnd)、search_box_rect(hwnd) -> (左, 上, 右, 下)、
    set_search_text(text)、clear_search()、selected_tab(hwnd) -> 索引或 None、select_tab(hwnd, index) -> 是否成功、
    ensure_wide(hwnd) -> 原本的位置 (沒有改動回傳 None)、restore_rect(hwnd, rect)、double_click(x, y)、screenshot(hwnd) -> (PIL 圖, (左, 上))。
    allow_member_count：要開的是群組時才設 True (搜尋結果的群組名稱後面會接成員數)；一對一不接受那種列。
    """
    if ui.find_chat_window(name):
        return True
    main = ui.main_window()
    if main is None:
        log("[開啟聊天視窗] 找不到 LINE 主視窗。")
        return False
    opened = False
    original_tab = None
    try:
        ui.bring_to_front(main)
        if not ui.is_front(main):
            log("[開啟聊天視窗] 無法把 LINE 主視窗帶到前景，放棄。")
            return False
        box = ui.search_box_rect(main)
        if box is None:
            log("[開啟聊天視窗] 找不到主視窗的搜尋欄，放棄。")
            return False
        current = ui.selected_tab(main)
        if current is not None and current != ALL_TAB_INDEX:
            # 一律在「全部」分頁搜尋：搜尋只找目前的分頁，而且在別的分頁 (真機：停在第 8 個分頁) 找得到列也可能雙擊不開視窗
            original_tab = current                          # 先記下：點擊可能已經切過去、只是事後驗證失敗，結束時一律切回使用者原本的分頁
            if not _switch_tab(ui, main, ALL_TAB_INDEX):
                log("[開啟聊天視窗] 無法切到「全部」分頁，放棄。")
                return False
            box = ui.search_box_rect(main) or box
            ui.set_search_text("")                          # 切分頁後先清掉搜尋欄 (萬一殘留同樣的字，LINE 不會重新搜尋)
        ui.set_search_text(name)
        sleep(SEARCH_RESULT_WAIT)
        if not ui.is_front(main):
            log("[開啟聊天視窗] 搜尋時主視窗不在前景，放棄。")
            return False
        rows = _find_rows(ui, main, box, name, read_boxes, allow_member_count)
        if not rows:
            log(f"[開啟聊天視窗] 搜尋結果裡找不到「{name}」(名稱必須完全相符)，放棄。")
            return False
        if not ui.is_front(main):
            log("[開啟聊天視窗] 雙擊前主視窗不在前景，放棄。")
            return False
        ui.double_click(*rows[0])
        deadline = time.time() + WINDOW_APPEAR_TIMEOUT
        while time.time() < deadline:
            if ui.find_chat_window(name):
                opened = True
                break
            sleep(0.5)
        if not opened:
            log(f"[開啟聊天視窗] 雙擊後沒有出現標題為「{name}」的視窗，放棄。")
        return opened
    finally:
        try:
            ui.clear_search()
        except Exception:
            pass
        if original_tab is not None:
            try:
                ui.bring_to_front(main)                     # 剛開好的聊天視窗會搶走前景，select_tab 要主視窗在前景才肯動
                if not _switch_tab(ui, main, original_tab):     # 把使用者原本的分頁還原
                    log("[開啟聊天視窗] 沒能把主視窗的分頁切回原本的位置，目前停在「全部」。")
            except Exception:
                log("[開啟聊天視窗] 還原主視窗的分頁時發生例外，目前可能停在「全部」。")
