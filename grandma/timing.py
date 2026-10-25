"""勿擾時段的計算。"""
from datetime import datetime, timedelta


def in_quiet_hours(hour, start, end) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨午夜，例如 23 ~ 7


def quiet_end(now: datetime, start, end) -> datetime:
    """回傳這段勿擾時段結束的時間點。"""
    target = now.replace(hour=end, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target
