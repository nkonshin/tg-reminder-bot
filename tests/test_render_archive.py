import html
import os
import sqlite3

from scripts.render_archive import load, media_html, message_html

BASE_ROW = dict(from_user_id=300, from_name="Собеседник", from_username=None,
                text="привет", media_kind=None, media_path=None,
                sent_at="2026-07-30T12:00:00+00:00", edited_at=None, deleted_at=None,
                forward_from_name=None, forward_from_username=None)


def a_row(**over):
    row = dict(BASE_ROW)
    row.update(over)
    return row


def test_a_normal_message_has_no_forward_header():
    out = message_html(a_row(), my_id=100, archive_dir="/tmp", offset=0)
    assert "Переслано" not in out


def test_a_forwarded_message_shows_its_source_and_username():
    out = message_html(a_row(forward_from_name="Иван Иванов", forward_from_username="ivan_i"),
                       my_id=100, archive_dir="/tmp", offset=0)
    assert "Переслано от" in out
    assert "Иван Иванов" in out
    assert "@ivan_i" in out


def test_a_forwarded_message_without_a_username_shows_only_the_name():
    out = message_html(
        a_row(forward_from_name="Скрытый Пользователь", forward_from_username=None),
        my_id=100, archive_dir="/tmp", offset=0)
    assert "Переслано от" in out
    assert "Скрытый Пользователь" in out
    assert "@" not in out


def test_a_forward_source_name_is_html_escaped():
    out = message_html(a_row(forward_from_name="<b>x</b>", forward_from_username=None),
                       my_id=100, archive_dir="/tmp", offset=0)
    assert html.escape("<b>x</b>") in out
    assert "<b>x</b>" not in out  # the raw, unescaped attacker string must never appear


def test_forward_header_appears_before_the_message_text():
    out = message_html(a_row(text="тело", forward_from_name="Иван Иванов"),
                       my_id=100, archive_dir="/tmp", offset=0)
    assert out.index("Переслано от") < out.index("тело")


def test_load_reads_the_forward_columns(tmp_path):
    db_path = str(tmp_path / "messages.sqlite3")
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE owners (id INTEGER PRIMARY KEY, owner_user_id INTEGER, owner_name TEXT);
        CREATE TABLE messages (
          owner_id INTEGER, chat_id INTEGER, message_id INTEGER,
          from_user_id INTEGER, from_name TEXT, from_username TEXT,
          text TEXT, media_kind TEXT, media_path TEXT,
          sent_at TEXT, edited_at TEXT, deleted_at TEXT,
          forward_from_name TEXT, forward_from_username TEXT
        );
        INSERT INTO messages VALUES (1, -1, 5, 300, 'Собеседник', NULL, 'привет',
          NULL, NULL, '2026-07-30T12:00:00+00:00', NULL, NULL, 'Иван Иванов', 'ivan_i');
    """)
    con.commit()
    con.close()
    rows, owners = load(db_path)
    assert rows[0]["forward_from_name"] == "Иван Иванов"
    assert rows[0]["forward_from_username"] == "ivan_i"


def test_load_defaults_forward_fields_to_none_for_an_older_export(tmp_path):
    # An export/backup taken before this feature shipped has no forward
    # columns at all -- load() must not crash on it, and rows must simply
    # render as non-forwarded.
    db_path = str(tmp_path / "old.sqlite3")
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE messages (
          owner_id INTEGER, chat_id INTEGER, message_id INTEGER,
          from_user_id INTEGER, from_name TEXT, from_username TEXT,
          text TEXT, media_kind TEXT, media_path TEXT,
          sent_at TEXT, edited_at TEXT, deleted_at TEXT
        );
        INSERT INTO messages VALUES (1, -1, 5, 300, 'Собеседник', NULL, 'привет',
          NULL, NULL, '2026-07-30T12:00:00+00:00', NULL, NULL);
    """)
    con.commit()
    con.close()
    rows, owners = load(db_path)
    assert rows[0]["forward_from_name"] is None
    assert rows[0]["forward_from_username"] is None


# --- stickers now download and render as images/video, not just a chip ---

def _touch_media(archive_dir, rel):
    path = os.path.join(archive_dir, "media", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()


def test_webp_sticker_renders_as_an_image(tmp_path):
    _touch_media(str(tmp_path), "1/2/3.webp")
    out = media_html(a_row(media_kind="sticker", media_path="1/2/3.webp"), str(tmp_path))
    assert '<img class="stk"' in out and "3.webp" in out


def test_webm_sticker_renders_as_a_looping_muted_video(tmp_path):
    _touch_media(str(tmp_path), "1/2/3.webm")
    out = media_html(a_row(media_kind="sticker", media_path="1/2/3.webm"), str(tmp_path))
    assert '<video class="stk"' in out
    assert "autoplay" in out and "loop" in out and "muted" in out


def test_tgs_animated_sticker_stays_a_labelled_chip(tmp_path):
    # A .tgs is gzipped Lottie — a plain browser can't play it, so it must not
    # become a broken <img>/<video>; keep it a chip.
    _touch_media(str(tmp_path), "1/2/3.tgs")
    out = media_html(a_row(media_kind="sticker", media_path="1/2/3.tgs"), str(tmp_path))
    assert "chip" in out and "<img" not in out and "<video" not in out


def test_sticker_without_a_downloaded_file_is_a_labelled_chip(tmp_path):
    # Rows from before sticker downloading shipped have media_path=NULL.
    out = media_html(a_row(media_kind="sticker", media_path=None), str(tmp_path))
    assert "chip" in out and "стикер" in out and "<img" not in out
