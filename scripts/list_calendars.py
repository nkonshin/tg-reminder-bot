"""Печатает все календари, видимые в CalDAV-профиле. Запуск: python scripts/list_calendars.py"""
import sys
from pathlib import Path

# Running this file directly (as documented) puts scripts/ on sys.path[0],
# not the project root, so `import src...` fails with ModuleNotFoundError
# unless the root is added explicitly first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import caldav

from src.config import Config

cfg = Config()
client = caldav.DAVClient(url=cfg.caldav_url, username=cfg.apple_id,
                          password=cfg.apple_app_password)
for cal in client.principal().calendars():
    print(f"{cal.name!r:40} {cal.url}")
