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
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

MEDIA_SUBDIR = "media"
STATE_FILE = ".render_state.json"
KIND_LABEL = {"photo": "фото", "video": "видео", "video_note": "кружок",
              "voice": "голосовое", "document": "документ", "animation": "гифка",
              "sticker": "стикер", "location": "геолокация", "contact": "контакт",
              "poll": "опрос", "audio": "музыка", "dice": "эмодзи-кубик",
              "story": "история", "venue": "место", "game": "игра"}
# Kinds that never have a downloadable file (no log_* toggle exists for them):
# always a labelled chip, regardless of media_path, never a broken embed.
# (sticker is NOT here: it downloads now and renders as an image/video below.)
NO_FILE_CHIP_ICON = {"location": "📍", "contact": "👤", "poll": "📊",
                     "venue": "📍", "dice": "🎲", "story": "📖", "game": "🎮"}
# stable, readable colours for sender names, picked by hashing the user id
NAME_COLORS = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7",
               "#ee7aae", "#6ec9cb", "#faa774"]

# View filters (see apply_view_filters). These only shrink what the HTML shows;
# storage/backups keep everything. The archive had grown past ~18k messages and
# the single page began to choke the browser, so the two heavy accounts are
# trimmed to what's actually worth eyeballing.
NIKITA_USER_ID = 208210577   # his own account — he barely reviews it
DASHA_USER_ID = 751057661    # her account
NIKITA_TAIL = 30             # keep only the last N messages of each of his chats


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive_dir", help="folder containing messages.sqlite3 and media/")
    p.add_argument("--tz-offset", type=int, default=0,
                   help="hours to add to stored UTC times for display (e.g. 5)")
    p.add_argument("--out", default=None, help="output HTML path")
    p.add_argument("--reset-new", action="store_true",
                   help="forget the previous render's mark: this render shows no "
                        "'new messages' separator, and the next one measures from here")
    p.add_argument("--full", action="store_true",
                   help="disable the view filters (apply_view_filters) and render "
                        "every stored message — a one-off escape hatch")
    return p.parse_args()


def load(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    has_username = "from_username" in cols
    # An export/backup taken before forward attribution shipped has neither
    # column -- fall back to NULL so an older archive still loads instead of
    # raising, same as the has_username fallback just above.
    has_forward = "forward_from_name" in cols
    rows = list(con.execute(
        "SELECT owner_id, chat_id, message_id, from_user_id, from_name, "
        + ("from_username, " if has_username else "NULL AS from_username, ")
        + "text, media_kind, media_path, sent_at, edited_at, deleted_at, "
        + ("forward_from_name, forward_from_username "
           if has_forward else "NULL AS forward_from_name, NULL AS forward_from_username ")
        + "FROM messages ORDER BY owner_id, chat_id, sent_at, message_id"))
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


def apply_view_filters(rows, owners):
    """Trim what goes into the HTML — the growing archive made the page too
    heavy to open. Storage is untouched; this only affects the rendered view.

      * The shared Nikita<->Dasha dialog (either side of the connection): keep
        only messages that were deleted. Their live conversation he reads in
        Telegram itself; the archive is there to surface what got removed.
      * Nikita's other chats: keep only the last NIKITA_TAIL messages of each —
        he skims his own account, huge histories aren't worth loading.
      * Everything else (Dasha's other chats): kept in full.

    Unknown owners, or an archive that isn't this two-account setup, fall
    through to "kept in full", so the viewer stays generic.
    """
    by_owner = defaultdict(list)
    for r in rows:
        by_owner[r["owner_id"]].append(r)
    owner_uid = {oid: owner_user_id_for(oid, owners, orows)
                 for oid, orows in by_owner.items()}

    by_oc = defaultdict(list)
    for r in rows:
        by_oc[(r["owner_id"], r["chat_id"])].append(r)

    kept = []
    for (owner_id, chat_id), crs in by_oc.items():
        uid = owner_uid.get(owner_id)
        is_shared = {uid, chat_id} == {NIKITA_USER_ID, DASHA_USER_ID}
        if is_shared:
            kept += [r for r in crs if r["deleted_at"]]
        elif uid == NIKITA_USER_ID:
            kept += crs[-NIKITA_TAIL:]  # crs already in chronological order
        else:
            kept += crs
    # render() and the watermark expect the same global order load() produced
    kept.sort(key=lambda r: (r["owner_id"], r["chat_id"], r["sent_at"], r["message_id"]))
    return kept


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


MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


def _local_dt(iso, offset_hours):
    """The stored UTC time shifted into the display timezone, or None."""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.astimezone(timezone.utc).timestamp() + offset_hours * 3600
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def fmt_time(iso, offset_hours):
    dt = _local_dt(iso, offset_hours)
    return dt.strftime("%d.%m %H:%M") if dt else (iso or "")


def day_key(iso, offset_hours):
    """ISO date (YYYY-MM-DD) of a message in display-local time, for the date
    jump — the anchors and the picker share this key."""
    dt = _local_dt(iso, offset_hours)
    return dt.strftime("%Y-%m-%d") if dt else ""


def day_label(iso, offset_hours):
    dt = _local_dt(iso, offset_hours)
    return f"{dt.day} {MONTHS_GEN[dt.month - 1]} {dt.year}" if dt else ""


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
        if kind == "sticker":
            # Old rows predate sticker downloading (media_path is NULL); still
            # label it rather than showing a broken embed.
            return f'<div class="chip">🎨 {html.escape(label)}</div>'
        if kind == "animation":
            return f'<div class="chip">🎞 {html.escape(label)} (не сохранена)</div>'
        return f'<div class="chip">🖇 {html.escape(label)} (файл не сохранён)</div>'
    src = html.escape(f"{MEDIA_SUBDIR}/{rel}")
    # Performance: images load lazily, players don't buffer until played
    # (preload="none"), and looping "autoplay" media carries data-auto instead
    # of the autoplay attribute so the page's IntersectionObserver plays it only
    # while it's on screen — dozens of videos decoding at once is what made
    # Safari crawl.
    if kind == "sticker":
        ext = os.path.splitext(rel)[1].lower()
        if ext == ".webp":
            return f'<img class="stk" loading="lazy" decoding="async" src="{src}">'
        if ext == ".webm":
            return (f'<video class="stk" data-auto muted loop playsinline '
                    f'preload="none" src="{src}"></video>')
        # .tgs is gzipped Lottie JSON — a plain browser can't play it inline.
        return f'<div class="chip">🎨 {html.escape(label)} (анимированный)</div>'
    if kind == "photo":
        return (f'<a href="{src}" target="_blank">'
                f'<img class="ph" loading="lazy" decoding="async" src="{src}"></a>')
    if kind in ("video", "video_note"):
        return f'<video class="vid" controls preload="none" src="{src}"></video>'
    if kind == "animation":
        # Gif-like: silent, looping — played only while visible (data-auto).
        return (f'<video class="vid" data-auto muted loop playsinline '
                f'preload="none" src="{src}"></video>')
    if kind in ("voice", "audio"):
        # Playback-speed button cycles 1×/1.5×/2× like Telegram (wired in JS).
        return (f'<span class="voice"><audio controls preload="none" src="{src}"></audio>'
                f'<button type="button" class="spd">1×</button></span>')
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
    if row["forward_from_name"]:
        who = html.escape(row["forward_from_name"])
        if row["forward_from_username"]:
            who = f'<b>{who}</b> (@{html.escape(row["forward_from_username"])})'
        else:
            who = f'<b>{who}</b>'
        parts.append(f'<div class="fwd">↩ Переслано от {who}</div>')
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
    uname = (f'<span class="uname">@{html.escape(row["from_username"])}</span> '
             if row["from_username"] else "")
    meta = (f'<div class="meta">{uname}{fmt_time(row["sent_at"], offset)} '
            f'{"".join(tags)}</div>')
    dd = day_key(row["sent_at"], offset)
    return f'<div class="{" ".join(classes)}" data-date="{dd}">{"".join(parts)}{meta}</div>'


def read_watermark(archive_dir):
    """Max sent_at recorded at the previous render — the 'new since last time'
    boundary. None on the first ever render (no separator, nothing is 'new')."""
    try:
        with open(os.path.join(archive_dir, STATE_FILE), encoding="utf-8") as fh:
            return json.load(fh).get("last_watermark")
    except (OSError, ValueError):
        return None


def write_watermark(archive_dir, value):
    if not value:
        return
    with open(os.path.join(archive_dir, STATE_FILE), "w", encoding="utf-8") as fh:
        json.dump({"last_watermark": value}, fh)


def feed_body(chat_rows, my_id, archive_dir, offset, watermark):
    """Messages interleaved with a date separator whenever the day changes (the
    date-jump targets), plus a one-time 'new messages' separator before the
    first message newer than `watermark` (the previous render's high-water
    mark). sent_at is a UTC ISO string, so a plain string compare orders it."""
    out, prev_day, new_shown = [], None, False
    for r in chat_rows:
        if watermark and not new_shown and r["sent_at"] > watermark:
            out.append('<div class="newsep">Новые сообщения</div>')
            new_shown = True
        dk = day_key(r["sent_at"], offset)
        if dk != prev_day:
            out.append(f'<div class="daysep" data-date="{dk}">'
                       f'{html.escape(day_label(r["sent_at"], offset))}</div>')
            prev_day = dk
        out.append(message_html(r, my_id, archive_dir, offset))
    return "".join(out)


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
.chat-item .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.chat-item .ct{color:var(--dim);font-size:12px;flex:none}
.newdot{width:8px;height:8px;border-radius:50%;background:#f5a623;flex:none;
align-self:center;box-shadow:0 0 0 2px rgba(245,166,35,.25)}
.chat-item.active .ct{color:#cfe0f0}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#head{padding:11px 20px;background:var(--panel);border-bottom:1px solid var(--line);
font-weight:600;font-size:16px;display:flex;align-items:center;justify-content:space-between;gap:12px}
#head small{color:var(--dim);font-weight:400;font-size:13px;margin-left:8px}
.headtools{display:flex;align-items:center;gap:10px;flex:none}
#datepick{background:#0e1621;color:var(--ink);border:1px solid var(--line);
border-radius:8px;padding:5px 8px;font-size:13px;color-scheme:dark;flex:none}
#newbtn{background:var(--mine);color:#fff;border:none;border-radius:8px;padding:6px 12px;
font-size:13px;cursor:pointer;display:none}
#newbtn:hover{background:#356ba0}
#feed{flex:1;overflow-y:auto;padding:20px 16px;display:flex;flex-direction:column}
.feed{display:none;flex-direction:column;gap:3px}
.daysep{align-self:center;background:#0e1621;color:var(--dim);font-size:12px;
padding:3px 12px;border-radius:10px;margin:10px 0;position:sticky;top:4px}
.newsep{align-self:stretch;text-align:center;color:#8ecdf0;font-size:12px;font-weight:600;
margin:12px 0 6px;border-top:1px solid #3a5a78;padding-top:6px}
.msg{max-width:64%;padding:6px 11px;border-radius:14px;word-wrap:break-word;margin-bottom:1px;
content-visibility:auto;contain-intrinsic-size:0 48px}
.msg.theirs{background:var(--theirs);align-self:flex-start;border-bottom-left-radius:4px}
.msg.mine{background:var(--mine);align-self:flex-end;border-bottom-right-radius:4px}
.msg.deleted{outline:1px solid var(--rust);opacity:.9}
.msg.deleted .txt{text-decoration:line-through;text-decoration-color:var(--rust)}
.who{font-size:12px;font-weight:600;margin-bottom:2px}
.fwd{font-size:12px;color:var(--dim);border-left:2px solid #4a5b6c;padding-left:7px;
margin-bottom:4px}
.fwd b{color:var(--ink)}
.txt{white-space:pre-wrap}
.txt.empty{color:var(--dim);font-style:italic}
.meta{font-size:11px;color:var(--dim);margin-top:3px;text-align:right}
.uname{color:#8ecdf0;opacity:.85}
.tag{font-size:10px;padding:1px 5px;border-radius:6px;margin-left:5px}
.tag.del{background:var(--rust);color:#fff}
.tag.ed{background:#3a4a5a;color:#cfe0f0}
img.ph{max-width:260px;max-height:320px;border-radius:8px;display:block}
video.vid{max-width:260px;border-radius:8px;display:block}
img.stk,video.stk{max-width:160px;max-height:160px;display:block}
audio{width:240px;margin:2px 0;vertical-align:middle}
.voice{display:inline-flex;align-items:center;gap:6px}
.spd{background:#0e1621;color:var(--ink);border:1px solid var(--line);border-radius:6px;
padding:3px 7px;font-size:12px;cursor:pointer;flex:none}
.spd:hover{background:#1c2b3a}
.chip{display:inline-block;background:#20303f;color:var(--dim);padding:5px 9px;
border-radius:8px;font-size:12px;text-decoration:none}
"""

PAGE_JS = """
const items=[...document.querySelectorAll('.chat-item')];
const feeds=[...document.querySelectorAll('.feed')];
const title=document.getElementById('title');
const feed=document.getElementById('feed');
const pick=document.getElementById('datepick');
const newbtn=document.getElementById('newbtn');
let activeFeed=null;
function show(key){
  const it=items.find(i=>i.dataset.key===key);
  feeds.forEach(f=>f.style.display=f.dataset.key===key?'flex':'none');
  items.forEach(i=>i.classList.toggle('active',i.dataset.key===key));
  title.innerHTML=it.dataset.name+' <small>'+it.dataset.acct+' · '+it.dataset.count+' сообщ.</small>';
  activeFeed=feeds.find(f=>f.dataset.key===key);
  // bound the date picker to this conversation's own range
  const dates=[...activeFeed.querySelectorAll('[data-date]')].map(e=>e.dataset.date).filter(Boolean).sort();
  pick.min=dates[0]||''; pick.max=dates[dates.length-1]||''; pick.value='';
  // the "new messages" button only appears when this chat has any
  newbtn.style.display=activeFeed.querySelector('.newsep')?'inline-block':'none';
  feed.scrollTop=0;
}
newbtn.addEventListener('click',()=>{
  const sep=activeFeed&&activeFeed.querySelector('.newsep');
  if(sep) sep.scrollIntoView({block:'start'});
});
pick.addEventListener('change',()=>{
  if(!activeFeed||!pick.value) return;
  const anchors=[...activeFeed.querySelectorAll('[data-date]')];
  // first element on or after the chosen day, so an empty day lands on the next one
  const target=anchors.find(e=>e.dataset.date>=pick.value)||anchors[anchors.length-1];
  if(target) target.scrollIntoView({block:'start'});
});
items.forEach(i=>i.addEventListener('click',()=>show(i.dataset.key)));
if(items.length)show(items[0].dataset.key);

// Voice/audio playback-speed toggle: cycles 1x -> 1.5x -> 2x on its own player.
const SPEEDS=[1,1.5,2];
document.addEventListener('click',e=>{
  const b=e.target.closest('.spd'); if(!b) return;
  const audio=b.parentElement.querySelector('audio'); if(!audio) return;
  const next=SPEEDS[(SPEEDS.indexOf(audio.playbackRate)+1)%SPEEDS.length];
  audio.playbackRate=next;
  b.textContent=(Number.isInteger(next)?next:next.toFixed(1))+'×';
});

// Looping "gif" media (animations, video stickers) plays only while on screen.
// Rendered with data-auto and preload="none" instead of the autoplay attribute,
// so the browser buffers and decodes just the few in view rather than every one
// on the page at once — the main thing that made a big archive lag.
const autoPlayVisible=new IntersectionObserver(entries=>{
  entries.forEach(e=>{
    const v=e.target;
    if(e.isIntersecting){ const p=v.play(); if(p&&p.catch) p.catch(()=>{}); }
    else { v.pause(); }
  });
},{root:feed,rootMargin:'150px'});
document.querySelectorAll('video[data-auto]').forEach(v=>autoPlayVisible.observe(v));
"""


def render(rows, owners, archive_dir, offset, watermark):
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
        # Most-recently-active chat on top, like a messenger's chat list.
        # sent_at is a UTC ISO string, so max() by plain string compare gives
        # the last message's time.
        chats = sorted(by_chat.items(),
                       key=lambda kv: max(r["sent_at"] for r in kv[1]), reverse=True)

        sidebar.append(f'<div class="acct">аккаунт: <b>{html.escape(acct_name)}</b></div>')
        for chat_id, crs in chats:
            key = f"o{owner_id}c{chat_id}"
            title = html.escape(chat_title(crs, my_id))
            # Unread dot: any message in this chat newer than the previous
            # render's mark — same boundary as the "new messages" separator.
            has_new = bool(watermark) and any(r["sent_at"] > watermark for r in crs)
            dot = '<span class="newdot" title="новые сообщения"></span>' if has_new else ''
            sidebar.append(
                f'<div class="chat-item" data-key="{key}" data-name="{title}" '
                f'data-acct="{html.escape(acct_name)}" data-count="{len(crs)}">'
                f'<span class="nm">{title}</span>{dot}<span class="ct">{len(crs)}</span></div>')
            feeds.append(f'<div class="feed" data-key="{key}">'
                         f'{feed_body(crs, my_id, archive_dir, offset, watermark)}</div>')

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Архив переписки</title><style>{PAGE_CSS}</style></head><body>
<div id="chats">{"".join(sidebar)}</div>
<div id="main"><div id="head"><span id="title"></span>
<span class="headtools"><button type="button" id="newbtn" title="к первому новому сообщению">↓ Новые</button>
<input type="date" id="datepick" title="перейти к дате"></span></div>
<div id="feed">{"".join(feeds)}</div></div>
<script>{PAGE_JS}</script></body></html>"""


def main():
    args = parse_args()
    archive_dir = os.path.abspath(args.archive_dir)
    # A one-off export unpacks to "messages.sqlite3"; the weekly backup keeps
    # the ever-growing merge as "master.sqlite3". Render whichever is present.
    db_path = next((os.path.join(archive_dir, n) for n in
                    ("messages.sqlite3", "master.sqlite3")
                    if os.path.isfile(os.path.join(archive_dir, n))), None)
    if db_path is None:
        raise SystemExit(f"no messages.sqlite3 or master.sqlite3 in {archive_dir}")

    rows, owners = load(db_path)
    if not rows:
        raise SystemExit("the database has no messages")

    # The high-water mark tracks the whole archive, not the filtered view, so it
    # keeps advancing correctly regardless of what the view happens to show.
    watermark = None if args.reset_new else read_watermark(archive_dir)
    newest = max(r["sent_at"] for r in rows)

    view_rows = rows if args.full else apply_view_filters(rows, owners)

    out = args.out or os.path.join(archive_dir, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(view_rows, owners, archive_dir, args.tz_offset, watermark))

    # Advance the mark so the next render measures "new" from this point.
    write_watermark(archive_dir, newest)

    new_count = sum(1 for r in view_rows if watermark and r["sent_at"] > watermark)
    print(f"accounts: {len(owners) or 'unknown'}")
    print(f"messages shown: {len(view_rows)} of {len(rows)} "
          f"({new_count} new since last render)")
    print(f"wrote: {out}")


if __name__ == "__main__":
    main()
