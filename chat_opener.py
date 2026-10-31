"""
自動開啟好友的獨立聊天視窗：機器人所有判斷都以「標題剛好等於好友名稱的獨立聊天視窗」為前提，
視窗被關掉就停擺。這個模組在視窗不見時，從 LINE 主視窗的搜尋欄把它開回來。

做法：用 UIA 把好友名稱寫進主視窗的搜尋欄 (不經鍵盤與剪貼簿，不受輸入法影響) -> 對主視窗截圖，用 OCR 找「整列文字剛好等於好友名稱」的搜尋結果 -> 雙擊 ->
等標題剛好等於好友名稱的獨立視窗出現。LINE 的清單項目沒有名字可以用 UIA 讀，所以只能靠 OCR 找位置。

安全原則 (Fail-Closed)：
- 只接受「整列文字完全等於好友名稱」(忽略空白與大小寫)，不接受「包含」；找不到、找到不只一個位置不合理都放棄。
- 主視窗裡是使用者真實的聊天清單：每一次點擊或貼上之前都核驗主視窗在前景，OCR 只在記憶體裡找名稱，
  不記錄也不輸出其他文字。
- 成功的唯一標準是出現標題剛好等於好友名稱的視窗；做完 (不論成敗) 都把搜尋欄清掉。
"""
import re
import time

import ocr_engine

SEARCH_RESULT_WAIT = 1.5        # 寫入搜尋文字後等結果出現的秒數
WINDOW_APPEAR_TIMEOUT = 8.0     # 雙擊後等獨立聊天視窗出現的秒數
ALL_TAB_INDEX = 0              # 「全部」永遠是第一個分頁；官方帳號不在「好友」分頁，搜尋只找目前分頁


def _norm(text):
    return re.sub(r"\s+", "", text or "").casefold()


def find_result_rows(boxes, name, min_y):
    """
    boxes: [(文字, (中心x, 中心y))]。回傳整列文字剛好等於 name 的位置 [(x, y)]，由上到下排序。
    min_y 以上 (搜尋欄所在的區域，要用搜尋欄的「底部」) 的文字一律忽略，避免把搜尋欄裡剛貼上的那串字當成結果。
    """
    wanted = _norm(name)
    rows = [(x, y) for text, (x, y) in boxes if _norm(text) == wanted and y > min_y]
    return sorted(rows, key=lambda p: p[1])


def _find_rows(ui, main, box, name, read_boxes):
    """對主視窗截圖，找搜尋結果裡整列文字剛好等於 name 的位置 (螢幕座標)，由上到下"""
    shot, origin = ui.screenshot(main)
    rows = find_result_rows(read_boxes(shot), name, min_y=box[3] - origin[1])
    return [(origin[0] + x, origin[1] + y) for x, y in rows]


def open_chat_window(name, ui, read_boxes=ocr_engine.read_boxes, log=lambda msg: None, sleep=time.sleep):
    """
    開啟名稱為 name 的好友的獨立聊天視窗，成功 (視窗已存在或已開好) 回傳 True，任何一步沒把握回傳 False。
    先在目前的分頁搜尋 (最不打擾)；找不到才切到「全部」分頁再搜，結束後把使用者原本的分頁切回去。
    ui 提供：find_chat_window(name)、main_window()、bring_to_front(hwnd)、is_front(hwnd)、search_box_rect(hwnd) -> (左, 上, 右, 下)、
    set_search_text(text)、clear_search()、selected_tab(hwnd) -> 索引或 None、select_tab(hwnd, index) -> 是否成功、
    ensure_wide(hwnd) -> 原本的位置 (沒有改動回傳 None)、restore_rect(hwnd, rect)、double_click(x, y)、screenshot(hwnd) -> (PIL 圖, (左, 上))。
    主視窗太窄時 LINE 會把分頁捲到看不見 (在分頁列上拖曳只會拖動整個視窗)，所以暫時把視窗拉寬，做完縮回原本的大小。
    """
    if ui.find_chat_window(name):
        return True
    main = ui.main_window()
    if main is None:
        log("[開啟聊天視窗] 找不到 LINE 主視窗。")
        return False
    opened = False
    original_tab = None
    original_rect = None
    try:
        ui.bring_to_front(main)
        if not ui.is_front(main):
            log("[開啟聊天視窗] 無法把 LINE 主視窗帶到前景，放棄。")
            return False
        original_rect = ui.ensure_wide(main)
        box = ui.search_box_rect(main)
        if box is None:
            log("[開啟聊天視窗] 找不到主視窗的搜尋欄，放棄。")
            return False
        ui.set_search_text(name)
        sleep(SEARCH_RESULT_WAIT)
        if not ui.is_front(main):
            log("[開啟聊天視窗] 搜尋時主視窗不在前景，放棄。")
            return False
        rows = _find_rows(ui, main, box, name, read_boxes)
        if not rows:
            current = ui.selected_tab(main)
            if current is None or current == ALL_TAB_INDEX:
                log(f"[開啟聊天視窗] 搜尋結果裡找不到「{name}」(名稱必須完全相符)，放棄。")
                return False
            log("[開啟聊天視窗] 目前的分頁找不到，改到「全部」分頁搜尋。")
            if not ui.select_tab(main, ALL_TAB_INDEX):
                log("[開啟聊天視窗] 無法切到「全部」分頁，放棄。")
                return False
            original_tab = current
            ui.set_search_text("")
            ui.set_search_text(name)
            sleep(SEARCH_RESULT_WAIT)
            if not ui.is_front(main):
                log("[開啟聊天視窗] 搜尋時主視窗不在前景，放棄。")
                return False
            rows = _find_rows(ui, main, box, name, read_boxes)
            if not rows:
                log(f"[開啟聊天視窗] 「全部」分頁的搜尋結果裡也找不到「{name}」(名稱必須完全相符)，放棄。")
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
                ui.select_tab(main, original_tab)           # 把使用者原本的分頁還原 (視窗還是寬的時候才看得到分頁)
            except Exception:
                pass
        if original_rect is not None:
            try:
                ui.restore_rect(main, original_rect)        # 視窗縮回原本的位置與大小
            except Exception:
                pass
