"""
OCR 引擎層：優先用 RapidOCR (離線、辨識中文藝術字比 Windows 內建 OCR 準很多)，沒安裝時退回呼叫端提供的後備函式。

實測 (30 張 Formosa 每日問候圖)：Windows 內建 OCR 只讀得到 2 張的問候語，RapidOCR 讀得到 15 張，
而且沒讀到問候語的圖，文字其實都讀得很準 (平安叮嚀、祝福類)，所以「讀得到字但沒有問候語」才可以當作可信的證據。
每張約 0.5 秒。第一次呼叫會載入模型 (約 1 秒)，之後重複使用同一個引擎。
"""
import threading

_engine = None
_engine_error = None
_lock = threading.Lock()


def _get_engine():
    """載入 RapidOCR 引擎 (只載一次)；沒安裝或載入失敗回傳 None，並記住失敗不再重試"""
    global _engine, _engine_error
    with _lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            return None
        try:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        except Exception as e:
            _engine_error = e
            return None
        return _engine


def is_available():
    """RapidOCR 能不能用 (會嘗試載入一次)"""
    return _get_engine() is not None


def unavailable_reason():
    """RapidOCR 不能用的原因 (能用或還沒嘗試載入回傳 None)"""
    return None if _engine_error is None else f"{type(_engine_error).__name__}: {_engine_error}"


def read_text(image, fallback=None):
    """
    讀出圖片上的文字，所有文字區塊直接串起來 (去掉空白，與 Windows OCR 的輸出格式一致)。
    image 可以是檔案路徑或 PIL 圖片 / numpy 陣列。RapidOCR 不能用時呼叫 fallback(image) (必須是檔案路徑)；
    沒有 fallback 就擲出 RuntimeError。RapidOCR 執行時出錯也退回 fallback。
    """
    engine = _get_engine()
    if engine is not None:
        try:
            source = image
            if not isinstance(image, (str, bytes)) and hasattr(image, "convert"):
                import numpy as np
                source = np.array(image.convert("RGB"))[:, :, ::-1]     # PIL (RGB) -> OpenCV (BGR)
            result, _ = engine(source)
            return "".join(item[1] for item in result).replace(" ", "") if result else ""
        except Exception:
            if fallback is None:
                raise
    if fallback is None:
        raise RuntimeError("RapidOCR 不能用，也沒有後備 OCR")
    return fallback(image)


def read_boxes(image):
    """
    讀出圖片上每一段文字和它的中心位置，回傳 [(文字, (中心x, 中心y))]，座標是圖片內的像素。
    只在 RapidOCR 能用時才有位置資訊，不能用或失敗回傳空清單 (呼叫端要把「讀不到」當成找不到)。
    image 可以是檔案路徑或 PIL 圖片。
    """
    engine = _get_engine()
    if engine is None:
        return []
    try:
        source = image
        if not isinstance(image, (str, bytes)) and hasattr(image, "convert"):
            import numpy as np
            source = np.array(image.convert("RGB"))[:, :, ::-1]
        result, _ = engine(source)
        boxes = []
        for item in result or []:
            points = item[0]
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            boxes.append((item[1], ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)))
        return boxes
    except Exception:
        return []
