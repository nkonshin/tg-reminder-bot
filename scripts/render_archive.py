#!/usr/bin/env python3
"""Render a monitor export (messages.sqlite3 + media/) into a readable HTML page.

The exported archive holds one SQLite database and a media/ tree. This turns it
into a chat-style page grouped by connected account: each account (a Telegram
Business connection) gets its own set of conversations, that account owner's
messages sit on the right, everyone else's on the left with the sender's name
above the bubble, deletions are struck through and edits are flagged. Media is
embedded by relative link.

Usage:
    python scripts/render_archive.py <archive-dir> [--tz-offset <hours>] [--out <file.html>]

<archive-dir> is the folder you unpacked the export into (it contains
messages.sqlite3 and, usually, media/). The HTML is written inside that folder
by default so the relative media/ links resolve — move the whole folder, not
just the file.

This is a local, offline viewer. The data is private correspondence: do not
publish the output.
"""
import argparse
import html
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

MEDIA_SUBDIR = "media"
KIND_LABEL = {"photo": "фото", "video": "видео", "video_note": "кружок",
              "voice": "голосовое", "document": "документ", "animation": "гифка",
              "sticker": "стикер", "location": "геолокация", "contact": "контакт",
              "poll": "опрос", "audio": "музыка", "dice": "эмодзи-кубик",
              "story": "история", "venue": "место", "game": "игра"}
# Kinds that never have a downloadable file (no log_* toggle exists for them):
# always a labelled chip, regardless of media_path, never a broken embed.
NO_FILE_CHIP_ICON = {"sticker": "🎨", "location": "📍", "contact": "👤", "poll": "📊",
                     "venue": "📍", "dice": "🎲", "story": "📖", "game": "🎮"}
# stable, readable colours for sender names, picked by hashing the user id
NAME_COLORS = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7",
               "#ee7aae", "#6ec9cb", "#faa774"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive_dir", help="folder containing messages.sqlite3 and media/")
    p.add_argument("--tz-offset", type=int, default=0,
                   help="hours to add to stored UTC times for display (e.g. 5)")
    p.add_argument("--out", default=None, help="output HTML path")
    return p.parse_args()


def load(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    has_username = "from_username" in cols
    rows = list(con.execute(
        "SELECT owner_id, chat_id, message_id, from_user_id, from_name, "
        + ("from_username, " if has_username else "NULL AS from_username, ")
        + "text, media_kind, media_path, sent_at, edited_at, deleted_at "
        "FROM messages ORDER BY owner_id, chat_id, sent_at, message_id"))
    owners = {}
    try:
        for r in con.execute("SELECT id, owner_user_id, owner_name FROM owners"):
            owners[r["id"]] = (r["owner_user_id"], r["owner_name"])
    except sqlite3.OperationalError:
        pass  # older export without the owners table
    con.close()
    return rows, owners


def owner_user_id_for(owner_id, owners, chat_rows):
    """Prefer the connection's real owner id from the owners table; fall back to
    the user who appears in the most of this connection's chats."""
    if owner_id in owners:
        return owners[owner_id][0]
    per_user = defaultdict(set)
    for r in chat_rows:
        if r["from_user_id"] is not None:
            per_user[r["from_user_id"]].add(r["chat_id"])
    return max(per_user, key=lambda u: len(per_user[u])) if per_user else None


def chat_title(chat_rows, my_id):
    names = defaultdict(int)
    for r in chat_rows:
        if r["from_user_id"] != my_id and r["from_name"]:
            names[r["from_name"]] += 1
    if names:
        return max(names, key=names.get)
    for r in chat_rows:
        if r["from_name"]:
            return r["from_name"]
    return str(chat_rows[0]["chat_id"])


def name_color(user_id):
    return NAME_COLORS[(user_id or 0) % len(NAME_COLORS)]


def fmt_time(iso, offset_hours):
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.astimezone(timezone.utc).timestamp() + offset_hours * 3600
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m %H:%M")


def media_html(row, archive_dir):
    kind = row["media_kind"]
    if not kind:
        return ""
    label = KIND_LABEL.get(kind, kind)
    if kind in NO_FILE_CHIP_ICON:
        # sticker/location/contact/poll/venue/dice/story/game are never
        # downloaded -- always a chip, media_path is always NULL for these.
        return f'<div class="chip">{NO_FILE_CHIP_ICON[kind]} {html.escape(label)}</div>'
    rel = row["media_path"]
    if not rel or not os.path.exists(os.path.join(archive_dir, MEDIA_SUBDIR, rel)):
        if kind == "animation":
            return f'<div class="chip">🎞 {html.escape(label)} (не сохранена)</div>'
        return f'<div class="chip">🖇 {html.escape(label)} (файл не сохранён)</div>'
    src = html.escape(f"{MEDIA_SUBDIR}/{rel}")
    if kind == "photo":
        return f'<a href="{src}" target="_blank"><img class="ph" src="{src}"></a>'
    if kind in ("video", "video_note"):
        return f'<video class="vid" controls src="{src}"></video>'
    if kind == "animation":
        # Gif-like: silent, looping, plays without a click.
        return (f'<video class="vid" autoplay muted loop playsinline '
                f'src="{src}"></video>')
    if kind in ("voice", "audio"):
        return f'<audio controls src="{src}"></audio>'
    return f'<a class="chip" href="{src}" target="_blank">📎 {html.escape(label)}</a>'


def message_html(row, my_id, archive_dir, offset):
    mine = row["from_user_id"] == my_id
    classes = ["msg", "mine" if mine else "theirs"]
    if row["deleted_at"]:
        classes.append("deleted")

    parts = []
    if not mine and row["from_name"]:
        c = name_color(row["from_user_id"])
        parts.append(f'<div class="who" style="color:{c}">{html.escape(row["from_name"])}</div>')
    media = media_html(row, archive_dir)
    if media:
        parts.append(media)
    if row["text"]:
        parts.append(f'<div class="txt">{html.escape(row["text"])}</div>')
    elif not media:
        parts.append('<div class="txt empty">(без текста)</div>')

    tags = []
    if row["deleted_at"]:
        tags.append('<span class="tag del">удалено</span>')
    if row["edited_at"]:
        tags.append('<span class="tag ed">изменено</span>')
    meta = f'<div class="meta">{fmt_time(row["sent_at"], offset)} {"".join(tags)}</div>'
    return f'<div class="{" ".join(classes)}">{"".join(parts)}{meta}</div>'


PAGE_CSS = """
:root{--bg:#0e1621;--panel:#17212b;--mine:#2b5278;--theirs:#182533;--ink:#e9eef3;
--dim:#7d8b99;--rust:#c65b3f;--line:#0b1219}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--ink);display:flex;height:100vh;overflow:hidden}
#chats{width:310px;flex:none;background:var(--panel);border-right:1px solid var(--line);
overflow-y:auto}
.acct{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);
padding:16px 16px 6px;border-bottom:1px solid var(--line)}
.acct b{color:#8ecdf0}
.chat-item{padding:11px 16px 11px 22px;border-bottom:1px solid var(--line);cursor:pointer;
display:flex;justify-content:space-between;gap:8px}
.chat-item:hover{background:#1c2b3a}
.chat-item.active{background:var(--mine)}
.chat-item .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-item .ct{color:var(--dim);font-size:12px;flex:none}
.chat-item.active .ct{color:#cfe0f0}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#head{padding:13px 20px;background:var(--panel);border-bottom:1px solid var(--line);
font-weight:600;font-size:16px}
#head small{color:var(--dim);font-weight:400;font-size:13px;margin-left:8px}
#feed{flex:1;overflow-y:auto;padding:20px 16px;display:flex;flex-direction:column}
.feed{display:none;flex-direction:column;gap:3px}
.msg{max-width:64%;padding:6px 11px;border-radius:14px;word-wrap:break-word;margin-bottom:1px}
.msg.theirs{background:var(--theirs);align-self:flex-start;border-bottom-left-radius:4px}
.msg.mine{background:var(--mine);align-self:flex-end;border-bottom-right-radius:4px}
.msg.deleted{outline:1px solid var(--rust);opacity:.9}
.msg.deleted .txt{text-decoration:line-through;text-decoration-color:var(--rust)}
.who{font-size:12px;font-weight:600;margin-bottom:2px}
.txt{white-space:pre-wrap}
.txt.empty{color:var(--dim);font-style:italic}
.meta{font-size:11px;color:var(--dim);margin-top:3px;text-align:right}
.tag{font-size:10px;padding:1px 5px;border-radius:6px;margin-left:5px}
.tag.del{background:var(--rust);color:#fff}
.tag.ed{background:#3a4a5a;color:#cfe0f0}
img.ph{max-width:260px;max-height:320px;border-radius:8px;display:block}
video.vid{max-width:260px;border-radius:8px;display:block}
audio{width:240px;margin:2px 0}
.chip{display:inline-block;background:#20303f;color:var(--dim);padding:5px 9px;
border-radius:8px;font-size:12px;text-decoration:none}
"""

PAGE_JS = """
const items=[...document.querySelectorAll('.chat-item')];
const feeds=[...document.querySelectorAll('.feed')];
const head=document.getElementById('head');
function show(key){
  const it=items.find(i=>i.dataset.key===key);
  feeds.forEach(f=>f.style.display=f.dataset.key===key?'flex':'none');
  items.forEach(i=>i.classList.toggle('active',i.dataset.key===key));
  head.innerHTML=it.dataset.name+' <small>'+it.dataset.acct+' · '+it.dataset.count+' сообщ.</small>';
  document.getElementById('feed').scrollTop=0;
}
items.forEach(i=>i.addEventListener('click',()=>show(i.dataset.key)));
if(items.length)show(items[0].dataset.key);
"""


def render(rows, owners, archive_dir, offset):
    by_owner = defaultdict(list)
    for r in rows:
        by_owner[r["owner_id"]].append(r)

    # accounts with more messages first
    account_order = sorted(by_owner, key=lambda o: len(by_owner[o]), reverse=True)

    sidebar, feeds = [], []
    for owner_id in account_order:
        orows = by_owner[owner_id]
        my_id = owner_user_id_for(owner_id, owners, orows)
        acct_name = owners.get(owner_id, (None, None))[1] or f"подключение {owner_id}"

        by_chat = defaultdict(list)
        for r in orows:
            by_chat[r["chat_id"]].append(r)
        chats = sorted(by_chat.items(), key=lambda kv: len(kv[1]), reverse=True)

        sidebar.append(f'<div class="acct">аккаунт: <b>{html.escape(acct_name)}</b></div>')
        for chat_id, crs in chats:
            key = f"o{owner_id}c{chat_id}"
            title = html.escape(chat_title(crs, my_id))
            sidebar.append(
                f'<div class="chat-item" data-key="{key}" data-name="{title}" '
                f'data-acct="{html.escape(acct_name)}" data-count="{len(crs)}">'
                f'<span class="nm">{title}</span><span class="ct">{len(crs)}</span></div>')
            msgs = "".join(message_html(r, my_id, archive_dir, offset) for r in crs)
            feeds.append(f'<div class="feed" data-key="{key}">{msgs}</div>')

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Архив переписки</title><style>{PAGE_CSS}</style></head><body>
<div id="chats">{"".join(sidebar)}</div>
<div id="main"><div id="head"></div><div id="feed">{"".join(feeds)}</div></div>
<script>{PAGE_JS}</script></body></html>"""


def main():
    args = parse_args()
    archive_dir = os.path.abspath(args.archive_dir)
    db_path = os.path.join(archive_dir, "messages.sqlite3")
    if not os.path.isfile(db_path):
        raise SystemExit(f"no messages.sqlite3 in {archive_dir}")

    rows, owners = load(db_path)
    if not rows:
        raise SystemExit("the database has no messages")

    out = args.out or os.path.join(archive_dir, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(rows, owners, archive_dir, args.tz_offset))

    print(f"accounts: {len(owners) or 'unknown'}")
    print(f"messages: {len(rows)}")
    print(f"wrote: {out}")


if __name__ == "__main__":
    main()
