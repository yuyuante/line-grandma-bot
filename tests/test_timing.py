from datetime import datetime

from grandma.timing import in_quiet_hours, quiet_end


def test_in_quiet_hours_across_midnight():
    assert in_quiet_hours(23, 23, 7)
    assert in_quiet_hours(3, 23, 7)
    assert not in_quiet_hours(7, 23, 7)
    assert not in_quiet_hours(12, 23, 7)


def test_in_quiet_hours_same_day():
    assert in_quiet_hours(13, 12, 14)
    assert not in_quiet_hours(14, 12, 14)
    assert not in_quiet_hours(5, 5, 5)


def test_quiet_end():
    assert quiet_end(datetime(2026, 1, 1, 23, 30), 23, 7) == datetime(2026, 1, 2, 7, 0)
    assert quiet_end(datetime(2026, 1, 2, 3, 0), 23, 7) == datetime(2026, 1, 2, 7, 0)
