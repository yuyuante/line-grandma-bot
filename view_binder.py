"""
自動「開窗＋點圖＋綁定」：未綁定的好友或群組傳新圖時，機器人自己把他的聊天視窗開出來、點開視窗裡一張照片，
看 LINE 快取有哪一個頻道跟著寫入，確認就是他的頻道才綁定，做完把視窗放回去。等於把手動的 --bind-view 自動化。

安全原則 (Fail-Closed)，沿用 --bind-view 的規則，一律寧可不綁：
- 點圖之前，聊天視窗必須在前景、標題剛好是這個對象 (群組可帶成員數)；點的位置必須落在這個視窗上。
- 點圖之後的觀察期間，快取有寫入的頻道必須剛好 1 個，而且每一次寫入當下前景視窗標題都還是這個對象
  (背景自動下載縮圖、別的聊天室同時有動靜、點開的是別的視窗，都會讓數量不是 1 或標題不符而中止)。
- 找不到照片可以點、點了沒有任何快取寫入、點完前景不是這個聊天室，都放棄，最多試 MAX_CLICKS 張。
- 只有「這個函式自己開出來的視窗」做完才放回去；使用者本來就開著的視窗不動。

所有碰視窗、碰檔案的動作都經由 io 物件 (見 bind_by_opening)，所以可以不開 LINE 就測。
"""
import time

WATCH_SECONDS = 6.0         # 點圖之後最多觀察多久快取有沒有寫入
POLL_SECONDS = 0.5
MAX_CLICKS = 3              # 最多試幾張照片 (第一張點不出快取寫入時才換下一張)
VIEWER_CLOSE_WAIT = 0.6     # 按 Esc 關掉放大檢視之後等多久
MAX_SCROLL_PAGES = 5        # 視窗裡看不到照片時，最多往上捲幾頁找 (做完會捲回原位)
SCROLL_SETTLE = 0.8         # 捲動後等畫面穩定
QUIET_SECONDS = 1.0         # 點圖前確認快取已安靜：每隔這麼久比一次快照
QUIET_MAX_TRIES = 6         # 最多比幾次；一直在變就不點、不綁


def _watch(io, before, sleep, now, touched):
    """點圖之後觀察快取：回傳 (有寫入的頻道 {hash: 檔案數}, 寫入當下前景標題是否曾經不符)"""
    changed = {}
    mismatch = False
    end = now() + WATCH_SECONDS
    while now() < end:
        sleep(POLL_SECONDS)
        cur = io.snapshot()
        tick_changed = 0
        for path, meta in cur.items():
            if before.get(path) == meta:
                continue
            touched[path] = meta
            ch = io.channel_of(path)
            if ch:
                changed[ch] = changed.get(ch, 0) + 1
                tick_changed += 1
        if tick_changed and not io.foreground_matches():      # 這一輪有寫入：寫入當下前景標題必須還是這個對象
            mismatch = True
        before = cur
    return changed, mismatch


def _run(name, io, hwnd, log, sleep, now, touched):
    if not io.bring_to_front(hwnd) or not io.foreground_matches():
        return False, f"無法把「{name}」的聊天視窗帶到前景 (標題不符或被擋住)，不綁定。"
    centers = io.photo_centers(hwnd)
    scrolled = 0
    try:
        while not centers and scrolled < MAX_SCROLL_PAGES:
            if not io.scroll(hwnd, up=True):
                break
            scrolled += 1
            sleep(SCROLL_SETTLE)
            centers = io.photo_centers(hwnd)
        if not centers:
            return False, f"「{name}」的聊天視窗裡 (含往上捲 {scrolled} 頁) 找不到可以點開的照片，不綁定 (等他傳照片後會再試)。"
        return _click_photos(name, io, hwnd, centers[:MAX_CLICKS], log, sleep, now, touched)
    finally:
        for _ in range(scrolled):               # 捲回原本的位置 (只在往上捲過才捲)
            try:
                io.scroll(hwnd, up=False)
            except Exception:
                break


def _wait_quiet(io, sleep):
    """
    點圖之前先等快取安靜 (連續兩次快照相同)。自動綁定正是在「某個頻道剛有新圖」時觸發，那個頻道可能還在寫檔；
    不先等它寫完，它的寫入會被誤當成點圖造成的，綁到錯的人。一直在變就回傳 False。
    """
    prev = io.snapshot()
    for _ in range(QUIET_MAX_TRIES):
        sleep(QUIET_SECONDS)
        cur = io.snapshot()
        if cur == prev:
            return True
        prev = cur
    return False


def _close_viewer(io, sleep):
    """按 Esc 關掉放大檢視，等一下讓畫面回到聊天室"""
    io.close_viewer()
    sleep(VIEWER_CLOSE_WAIT)


def _click_photos(name, io, hwnd, centers, log, sleep, now, touched):
    for x, y in centers:
        if not _wait_quiet(io, sleep):
            return False, f"點圖前快取一直在變動 (別的頻道正在寫入)，無法確認之後的寫入是點圖造成的，不綁定「{name}」。"
        before = io.snapshot()
        if not io.click(hwnd, x, y):
            return False, f"點圖位置不在「{name}」的聊天視窗上 (被別的視窗蓋住)，不綁定。"
        changed, mismatch = _watch(io, before, sleep, now, touched)
        if changed:
            _close_viewer(io, sleep)
            if mismatch:
                return False, f"快取寫入當下前景視窗不是「{name}」的聊天室，無法確認頻道歸屬，不綁定。"
            if len(changed) != 1:
                return False, f"點圖期間有 {len(changed)} 個頻道有快取寫入，必須剛好 1 個，不綁定。"
            return io.bind(next(iter(changed)))
        if not io.foreground_matches():
            _close_viewer(io, sleep)
            if not io.foreground_matches():
                return False, f"點圖後前景視窗不是「{name}」的聊天室，已中止，不綁定。"
        log(" -> 點開的這張沒有造成快取寫入 (可能是貼圖或已在快取)，換下一張試試...")
    return False, f"試了 {len(centers)} 張都沒有看到快取寫入，不綁定。"


def bind_by_opening(name, io, log=lambda msg: None, sleep=time.sleep, now=time.time):
    """
    回傳 (成功與否, 訊息, touched)。touched 是這段期間內快取有變動的檔案 {路徑: meta}，
    呼叫端要把它們標成「已處理」，否則剛綁好的頻道會把我們自己點圖造成的快取寫入當成對方傳來的新圖而回覆。

    io 要提供：find_window()、open_window()、bring_to_front(hwnd)、foreground_matches()、photo_centers(hwnd)
    (最新的在前)、scroll(hwnd, up) (捲一頁；滑鼠位置不在該視窗上或失敗回傳 False)、
    click(hwnd, x, y) (位置不在該視窗上要回傳 False 且不點)、close_viewer()、snapshot()、
    channel_of(path)、bind(ch) -> (ok, msg)、finish(hwnd, opened_by_us)。
    """
    touched = {}
    hwnd = io.find_window()
    opened_by_us = False
    if hwnd is None:
        if not io.open_window():
            return False, f"找不到「{name}」的聊天視窗，也開不起來，不綁定。", touched
        opened_by_us = True
        hwnd = io.find_window()
        if hwnd is None:
            return False, f"已嘗試開啟「{name}」，但沒有找到標題相符的視窗，不綁定。", touched
    try:
        ok, msg = _run(name, io, hwnd, log, sleep, now, touched)
    except Exception as e:
        ok, msg = False, f"自動綁定「{name}」時發生例外: {e}，不綁定。"
    finally:
        try:
            io.finish(hwnd, opened_by_us)
        except Exception:
            pass
    return ok, msg, touched
