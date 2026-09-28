#!/usr/bin/env python3
"""Rename a Telegram Desktop export's media files to carry the message date/time.

photo/video/round/voice/sticker -> <kind>_YYYY-MM-DD_HH-MM-SS.<ext>
document (files/)               -> YYYY-MM-DD_HH-MM-SS_<original name>  (keeps the
                                   meaningful filename, strips the leading id_)

Renames in place, rewrites the media links in messages*.html so the Telegram
export keeps working, and writes rename_map.json (old -> new) so it is reversible.
Dry run by default; pass --apply to actually rename. Re-run scripts/render_tg_export.py
afterwards to refresh viewer.html.

Usage:
    python scripts/rename_export_media.py "<export-dir>" [--apply]
"""
import argparse
import glob
import importlib.util
import json
import os
import re

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("rtg", os.path.join(_here, "render_tg_export.py"))
rtg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rtg)

PREFIX = {"photo": "photo", "video": "video", "round": "round",
          "voice": "voice", "sticker": "sticker"}
LEADING_ID_RE = re.compile(r"^\d+_")  # Telegram prefixes files with a numeric id


def build_map(export_dir):
    """old_rel -> new_rel for every dated media file, collisions de-duped."""
    messages = rtg.parse_messages(export_dir)
    rename, used = {}, set()
    for m in messages:
        dt = m["dt"]
        if not dt:
            continue  # can't date it -> leave the original name
        stamp = dt.strftime("%Y-%m-%d_%H-%M-%S")
        for kind, href in m["media"]:
            if href in rename:
                continue
            folder, old_name = os.path.split(href)
            ext = os.path.splitext(old_name)[1]
            if kind == "file":
                human = LEADING_ID_RE.sub("", old_name)          # drop "6033_"
                base, ext = os.path.splitext(human)
                stem = f"{stamp}_{base}"
            else:
                stem = f"{PREFIX.get(kind, 'media')}_{stamp}"
            cand = f"{stem}{ext}"
            n = 2
            while f"{folder}/{cand}" in used:
                cand = f"{stem}-{n}{ext}"
                n += 1
            new_rel = f"{folder}/{cand}"
            used.add(new_rel)
            if new_rel != href:
                rename[href] = new_rel
    return rename


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_dir")
    ap.add_argument("--apply", action="store_true", help="actually rename (default: dry run)")
    args = ap.parse_args()
    export_dir = os.path.abspath(args.export_dir)

    rename = build_map(export_dir)
    present = {o: n for o, n in rename.items()
              if os.path.exists(os.path.join(export_dir, o))}
    missing = len(rename) - len(present)

    print(f"файлов к переименованию: {len(present)}" + (f" (нет на диске: {missing})" if missing else ""))
    for o, n in list(present.items())[:6]:
        print(f"  {o}\n    -> {n}")

    if not args.apply:
        print("\n[DRY RUN] ничего не тронуто. Запусти с --apply чтобы применить.")
        return

    # 1) rename files
    done = 0
    for o, n in present.items():
        src, dst = os.path.join(export_dir, o), os.path.join(export_dir, n)
        if os.path.exists(dst):
            continue
        os.rename(src, dst)
        done += 1
    # 2) rewrite links in the Telegram HTML (exact, quoted, so no partial matches)
    for path in glob.glob(os.path.join(export_dir, "messages*.html")):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for o, n in present.items():
            text = text.replace(f'"{o}"', f'"{n}"')
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    # 3) reversible map
    with open(os.path.join(export_dir, "rename_map.json"), "w", encoding="utf-8") as fh:
        json.dump(present, fh, ensure_ascii=False, indent=1)

    print(f"\nпереименовано: {done}")
    print("messages*.html обновлены, rename_map.json записан (для отката).")
    print("Теперь перегенери viewer.html: python scripts/render_tg_export.py <export-dir>")


if __name__ == "__main__":
    main()
