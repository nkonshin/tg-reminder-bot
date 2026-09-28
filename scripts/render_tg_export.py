#!/usr/bin/env python3
"""Render a Telegram Desktop HTML export (messages*.html + media folders) into a
single readable chat-style page — the same look as scripts/render_archive.py, plus
full-text search — because Telegram's own exported messages.html is unusable.

Usage:
    python scripts/render_tg_export.py "<export-dir>" [--owner "дашк"] [--out viewer.html]

<export-dir> is the folder the export unpacked into: it holds messages.html,
messages2.html, ... and the media folders (photos/, video_files/,
voice_messages/, round_video_messages/, stickers/, files/, thumbs/). The output
is written inside that folder so the relative media links resolve — keep it there.

Offline, local viewer of private correspondence: do not publish the output.
"""
import argparse
import glob
import html
import os
import re
from datetime import datetime

MONTHS_GEN = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
              "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
              "ноября": 11, "декабря": 12}
MONTHS_NOM = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
NAME_COLORS = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7",
               "#ee7aae", "#6ec9cb", "#faa774"]

DATE_TITLE_RE = re.compile(
    r'class="pull_right date details" title="(\d{1,2}) (\w+) (\d{4}), '
    r'(\d{2}):(\d{2}):(\d{2})')
FROM_NAME_RE = re.compile(r'<div class="from_name">\s*(.*?)\s*</div>', re.S)
TEXT_RE = re.compile(r'<div class="text">(.*?)</div>\s*(?=<div class="(?:pull_right|signature|reply|media|forwarded)|</div>)', re.S)
TEXT_RE_SIMPLE = re.compile(r'<div class="text">(.*?)</div>', re.S)
FWD_RE = re.compile(r'<div class="forwarded_from details">\s*Forwarded from\s*(.*?)\s*</div>', re.S)
ID_RE = re.compile(r'id="message(-?\d+)"')
# each media anchor: <a class="..." href="folder/file">
MEDIA_RE = re.compile(r'<a class="([^"]*?)" href="([^"]+?)"')
FILE_TITLE_RE = re.compile(r'<div class="title bold">(.*?)</div>', re.S)


def strip_tags(fragment: str) -> str:
    """Inner HTML of a Telegram text/caption div -> plain text with newlines."""
    fragment = re.sub(r'<br\s*/?>', '\n', fragment)
    fragment = re.sub(r'<[^>]+>', '', fragment)          # drop <a>, <strong>, ...
    return html.unescape(fragment).strip()


def parse_dt(chunk: str):
    m = DATE_TITLE_RE.search(chunk)
    if not m:
        return None
    day, mon_word, year, hh, mm, ss = m.groups()
    month = MONTHS_GEN.get(mon_word)
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), int(hh), int(mm), int(ss))
    except ValueError:
        return None


def classify_media(cls: str, href: str):
    """(kind, href) for a recognised media anchor, else None. kind drives how it
    is embedded in the page."""
    if "photo_wrap" in cls or href.startswith("photos/"):
        return "photo", href
    if "video_file_wrap" in cls and "round" in cls:
        return "round", href
    if "video_file_wrap" in cls or href.startswith("video_files/"):
        return "video", href
    if "media_voice_message" in cls or href.startswith("voice_messages/"):
        return "voice", href
    if "sticker_wrap" in cls or href.startswith("stickers/"):
        return "sticker", href
    if "media_file" in cls or href.startswith("files/"):
        return "file", href
    return None


def parse_messages(export_dir):
    files = sorted(glob.glob(os.path.join(export_dir, "messages*.html")),
                   key=lambda p: (len(p), p))  # messages.html before messages2.html
    if not files:
        raise SystemExit(f"no messages*.html in {export_dir}")
    blob = ""
    for path in files:
        with open(path, encoding="utf-8") as fh:
            blob += fh.read()

    # Split into per-message chunks on the message div boundary, keeping the class.
    parts = re.split(r'(<div class="message [^"]*")', blob)
    messages, last_sender = [], None
    for i in range(1, len(parts), 2):
        cls, chunk = parts[i], parts[i + 1]
        if "service" in cls:
            continue  # date separators are regenerated; other service events skipped
        joined = "joined" in cls
        fn = FROM_NAME_RE.search(chunk)
        sender = strip_tags(fn.group(1)) if fn else None
        if sender:
            last_sender = sender
        elif joined:
            sender = last_sender
        dt = parse_dt(chunk)

        tm = TEXT_RE_SIMPLE.search(chunk)
        text = strip_tags(tm.group(1)) if tm else ""

        media = []
        for cmatch, href in MEDIA_RE.findall(chunk):
            got = classify_media(cmatch, href)
            if got:
                media.append(got)

        fwd = FWD_RE.search(chunk)
        forwarded = strip_tags(fwd.group(1)) if fwd else None
        file_title = FILE_TITLE_RE.search(chunk)
        fname = strip_tags(file_title.group(1)) if file_title else None

        if not (text or media or forwarded):
            continue  # a service/empty remnant with no content
        messages.append({"sender": sender or "?", "dt": dt, "text": text,
                         "media": media, "forwarded": forwarded, "file_name": fname})
    return messages


def name_color(name):
    return NAME_COLORS[sum(map(ord, name or "")) % len(NAME_COLORS)]


def media_html(kind, href, file_name=None):
    src = html.escape(href)
    if kind == "photo":
        return f'<a href="{src}" target="_blank"><img class="ph" loading="lazy" decoding="async" src="{src}"></a>'
    if kind == "video":
        return f'<video class="vid" controls preload="none" src="{src}"></video>'
    if kind == "round":
        return f'<video class="vid rnd" controls preload="none" src="{src}"></video>'
    if kind == "voice":
        return (f'<span class="voice"><audio controls preload="none" src="{src}"></audio>'
                f'<button type="button" class="spd">1×</button></span>')
    if kind == "sticker":
        if href.lower().endswith(".webp"):
            return f'<img class="stk" loading="lazy" decoding="async" src="{src}">'
        return '<div class="chip">🎨 стикер (анимированный)</div>'  # .tgs — не играется
    if kind == "file":
        label = html.escape(file_name or os.path.basename(href))
        return f'<a class="chip" href="{src}" target="_blank">📎 {label}</a>'
    return ""


def message_html(m, owner):
    mine = m["sender"] == owner
    cls = "msg mine" if mine else "msg theirs"
    parts = []
    if not mine:
        c = name_color(m["sender"])
        parts.append(f'<div class="who" style="color:{c}">{html.escape(m["sender"])}</div>')
    if m["forwarded"]:
        parts.append(f'<div class="fwd">↩ Переслано от <b>{html.escape(m["forwarded"])}</b></div>')
    for kind, href in m["media"]:
        parts.append(media_html(kind, href, m["file_name"]))
    if m["text"]:
        parts.append(f'<div class="txt">{html.escape(m["text"])}</div>')
    elif not m["media"]:
        parts.append('<div class="txt empty">(без текста)</div>')
    dt = m["dt"]
    tm = dt.strftime("%d.%m.%Y %H:%M") if dt else ""
    dk = dt.strftime("%Y-%m-%d") if dt else ""
    parts.append(f'<div class="meta">{tm}</div>')
    return f'<div class="{cls}" data-date="{dk}">{"".join(parts)}</div>'


def day_label(dt):
    return f"{dt.day} {MONTHS_NOM[dt.month - 1]} {dt.year}"


def feed_body(messages, owner):
    out, prev = [], None
    for m in messages:
        dt = m["dt"]
        dk = dt.strftime("%Y-%m-%d") if dt else ""
        if dk != prev:
            lbl = day_label(dt) if dt else "—"
            out.append(f'<div class="daysep" data-date="{dk}">{html.escape(lbl)}</div>')
            prev = dk
        out.append(message_html(m, owner))
    return "".join(out)


PAGE_CSS = """
:root{--bg:#0e1621;--panel:#17212b;--mine:#2b5278;--theirs:#182533;--ink:#e9eef3;
--dim:#7d8b99;--rust:#c65b3f;--line:#0b1219;--accent:#8ecdf0}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--ink);display:flex;flex-direction:column;height:100vh;overflow:hidden}
#head{padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:12px;flex:none}
#head .title{font-weight:600;font-size:16px;white-space:nowrap}
#head .title small{color:var(--dim);font-weight:400;font-size:13px;margin-left:8px}
.tools{margin-left:auto;display:flex;align-items:center;gap:8px;flex:none}
#search{background:#0e1621;color:var(--ink);border:1px solid var(--line);border-radius:8px;
padding:6px 10px;font-size:13px;width:220px}
#search::placeholder{color:var(--dim)}
#datepick{background:#0e1621;color:var(--ink);border:1px solid var(--line);border-radius:8px;
padding:5px 8px;font-size:13px;color-scheme:dark}
.sbtn{background:#0e1621;color:var(--ink);border:1px solid var(--line);border-radius:8px;
padding:5px 9px;font-size:13px;cursor:pointer}
.sbtn:hover{background:#1c2b3a}
#scount{color:var(--dim);font-size:12px;min-width:52px;text-align:center}
#feed{flex:1;overflow-y:auto;padding:20px 16px;display:flex;flex-direction:column;gap:3px}
.daysep{align-self:center;background:#0e1621;color:var(--dim);font-size:12px;
padding:3px 12px;border-radius:10px;margin:10px 0;position:sticky;top:4px;z-index:1}
.msg{max-width:64%;padding:6px 11px;border-radius:14px;word-wrap:break-word;margin-bottom:1px;
content-visibility:auto;contain-intrinsic-size:0 48px}
.msg.theirs{background:var(--theirs);align-self:flex-start;border-bottom-left-radius:4px}
.msg.mine{background:var(--mine);align-self:flex-end;border-bottom-right-radius:4px}
.msg.hit{outline:2px solid var(--accent)}
.msg.cur{outline:2px solid #ffd54a;box-shadow:0 0 0 3px rgba(255,213,74,.25)}
.who{font-size:12px;font-weight:600;margin-bottom:2px}
.fwd{font-size:12px;color:var(--dim);border-left:2px solid #4a5b6c;padding-left:7px;margin-bottom:4px}
.fwd b{color:var(--ink)}
.txt{white-space:pre-wrap}
.txt.empty{color:var(--dim);font-style:italic}
.txt mark{background:#ffd54a;color:#000;border-radius:2px}
.meta{font-size:11px;color:var(--dim);margin-top:3px;text-align:right}
img.ph{max-width:260px;max-height:320px;border-radius:8px;display:block}
video.vid{max-width:260px;border-radius:8px;display:block}
video.vid.rnd{width:200px;height:200px;object-fit:cover;border-radius:50%}
img.stk{max-width:160px;max-height:160px;display:block}
audio{width:240px;margin:2px 0;vertical-align:middle}
.voice{display:inline-flex;align-items:center;gap:6px}
.spd{background:#0e1621;color:var(--ink);border:1px solid var(--line);border-radius:6px;
padding:3px 7px;font-size:12px;cursor:pointer;flex:none}
.spd:hover{background:#1c2b3a}
.chip{display:inline-block;background:#20303f;color:var(--dim);padding:5px 9px;
border-radius:8px;font-size:12px;text-decoration:none}
"""

PAGE_JS = """
const feed=document.getElementById('feed');
const pick=document.getElementById('datepick');
const search=document.getElementById('search');
const scount=document.getElementById('scount');
const prevBtn=document.getElementById('sprev');
const nextBtn=document.getElementById('snext');

// Date jump: scroll to the first message on or after the chosen day.
const anchors=[...feed.querySelectorAll('[data-date]')];
const allDates=anchors.map(e=>e.dataset.date).filter(Boolean).sort();
pick.min=allDates[0]||''; pick.max=allDates[allDates.length-1]||'';
pick.addEventListener('change',()=>{
  if(!pick.value) return;
  const t=anchors.find(e=>e.dataset.date>=pick.value)||anchors[anchors.length-1];
  if(t) t.scrollIntoView({block:'start'});
});

// Voice/audio playback-speed toggle 1x -> 1.5x -> 2x.
const SPEEDS=[1,1.5,2];
document.addEventListener('click',e=>{
  const b=e.target.closest('.spd'); if(!b) return;
  const a=b.parentElement.querySelector('audio'); if(!a) return;
  const n=SPEEDS[(SPEEDS.indexOf(a.playbackRate)+1)%SPEEDS.length];
  a.playbackRate=n; b.textContent=(Number.isInteger(n)?n:n.toFixed(1))+'×';
});

// Full-text search: highlight matches in message text, jump between them.
// Reads each .txt's textContent as the source of truth (it ignores any <mark>
// tags already there), so re-searching is clean without a separate "raw" copy.
const msgs=[...feed.querySelectorAll('.msg')];
const txts=msgs.map(m=>m.querySelector('.txt:not(.empty)'));
let hits=[], cur=-1, timer=null;
function escapeHtml(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function highlight(el,q){
  const text=el.textContent;
  if(!q){ el.textContent=text; return false; }
  const low=text.toLowerCase(), ql=q.toLowerCase();
  if(low.indexOf(ql)<0){ el.textContent=text; return false; }
  let out='',i=0,idx;
  while((idx=low.indexOf(ql,i))>=0){
    out+=escapeHtml(text.slice(i,idx))+'<mark>'+escapeHtml(text.slice(idx,idx+q.length))+'</mark>';
    i=idx+q.length;
  }
  out+=escapeHtml(text.slice(i));
  el.innerHTML=out; return true;
}
function run(q){
  q=q.trim(); hits=[]; cur=-1;
  msgs.forEach((m,i)=>{
    const matched=txts[i]?highlight(txts[i],q):false;
    m.classList.toggle('hit',matched); m.classList.remove('cur');
    if(matched) hits.push(m);
  });
  if(!q){ scount.textContent=''; return; }
  scount.textContent=hits.length?('1/'+hits.length):'нет';
  if(hits.length){ cur=0; focusHit(); }
}
function focusHit(){
  msgs.forEach(m=>m.classList.remove('cur'));
  const m=hits[cur]; if(!m) return;
  m.classList.add('cur'); m.scrollIntoView({block:'center'});
  scount.textContent=(cur+1)+'/'+hits.length;
}
function step(d){ if(!hits.length) return; cur=(cur+d+hits.length)%hits.length; focusHit(); }
search.addEventListener('input',()=>{ clearTimeout(timer); timer=setTimeout(()=>run(search.value),200); });
search.addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); step(e.shiftKey?-1:1); }});
prevBtn.addEventListener('click',()=>step(-1));
nextBtn.addEventListener('click',()=>step(1));
"""


def render(messages, owner, title):
    body = feed_body(messages, owner)
    total = len(messages)
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body>
<div id="head"><span class="title">{html.escape(title)} <small>{total} сообщений</small></span>
<span class="tools">
<input type="search" id="search" placeholder="поиск по сообщениям…" autocomplete="off">
<button type="button" class="sbtn" id="sprev" title="предыдущее">‹</button>
<span id="scount"></span>
<button type="button" class="sbtn" id="snext" title="следующее">›</button>
<input type="date" id="datepick" title="перейти к дате">
</span></div>
<div id="feed">{body}</div>
<script>{PAGE_JS}</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir")
    ap.add_argument("--owner", default="дашк",
                    help='sender shown on the right (the export owner); default "дашк"')
    ap.add_argument("--out", default="viewer.html")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()
    export_dir = os.path.abspath(args.export_dir)
    messages = parse_messages(export_dir)
    if not messages:
        raise SystemExit("no messages parsed")
    title = args.title or os.path.basename(export_dir).replace("ChatExport_", "")
    out = os.path.join(export_dir, args.out)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(messages, args.owner, title))

    kinds = {}
    for m in messages:
        for k, _ in m["media"]:
            kinds[k] = kinds.get(k, 0) + 1
    dts = [m["dt"] for m in messages if m["dt"]]
    print(f"messages: {len(messages)}")
    if dts:
        print(f"range: {min(dts):%d.%m.%Y} .. {max(dts):%d.%m.%Y}")
    print(f"media: {kinds}")
    senders = {}
    for m in messages:
        senders[m["sender"]] = senders.get(m["sender"], 0) + 1
    print(f"senders: {senders}")
    print(f"owner (right side): {args.owner!r}")
    print(f"wrote: {out}")


if __name__ == "__main__":
    main()
