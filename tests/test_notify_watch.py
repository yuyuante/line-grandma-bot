import sqlite3

from grandma.notify_watch import NotifyWatcher, is_photo_from, parse_toast

KEYWORDS = ["傳送了照片", "sent a photo"]


def toast_xml(*texts):
    body = "".join(f"<text>{t}</text>" for t in texts)
    return f'<toast><visual><binding template="ToastGeneric">{body}</binding></visual></toast>'.encode()


def test_parse_toast():
    assert parse_toast(toast_xml("阿姨", "傳送了照片")) == ["阿姨", "傳送了照片"]
    assert parse_toast(b"not xml") == []


def test_is_photo_from():
    assert is_photo_from(["阿姨", "傳送了照片"], "阿姨", KEYWORDS)
    assert not is_photo_from(["阿姨", "早安"], "阿姨", KEYWORDS)
    assert not is_photo_from(["別人", "傳送了照片"], "阿姨", KEYWORDS)
    assert not is_photo_from([], "阿姨", KEYWORDS)


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE NotificationHandler (RecordId INTEGER PRIMARY KEY, PrimaryId TEXT);
        CREATE TABLE Notification (Id INTEGER PRIMARY KEY, HandlerId INTEGER, Type TEXT, Payload BLOB);
        INSERT INTO NotificationHandler VALUES (1, 'NAVER.LINE.PC'), (2, 'Microsoft.Outlook');
        """
    )
    return conn


def insert(conn, nid, handler, payload, kind="toast"):
    conn.execute("INSERT INTO Notification VALUES (?, ?, ?, ?)", (nid, handler, kind, payload))
    conn.commit()


def test_watcher_only_counts_new_photo_toasts_from_friend(tmp_path):
    db = tmp_path / "wpn.db"
    conn = make_db(db)
    insert(conn, 1, 1, toast_xml("阿姨", "傳送了照片"))  # 啟動前的舊通知

    watcher = NotifyWatcher("阿姨", KEYWORDS, "line", db_path=db)
    assert watcher.poll() == 0

    insert(conn, 2, 1, toast_xml("阿姨", "早安"))
    insert(conn, 3, 1, toast_xml("阿姨", "傳送了照片"))
    insert(conn, 4, 2, toast_xml("阿姨", "傳送了照片"))  # 不是 LINE
    insert(conn, 5, 1, toast_xml("別人", "傳送了照片"))
    insert(conn, 6, 1, toast_xml("阿姨", "傳送了照片"), kind="badge")
    assert watcher.poll() == 1
    assert watcher.poll() == 0
