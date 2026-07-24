"""Печатает все календари, видимые в CalDAV-профиле. Запуск: python scripts/list_calendars.py"""
import caldav

from src.config import Config

cfg = Config()
client = caldav.DAVClient(url=cfg.caldav_url, username=cfg.apple_id,
                          password=cfg.apple_app_password)
for cal in client.principal().calendars():
    print(f"{cal.name!r:40} {cal.url}")
