import os

import yaml

from src.config import Config

COMPOSE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docker-compose.yml")


def _compose_bot_service() -> dict:
    with open(COMPOSE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]["bot"]


def make_config(**overrides) -> Config:
    base = dict(bot_token="t", her_user_id=1, admin_user_id=2, _env_file=None)
    base.update(overrides)
    return Config(**base)


def test_defaults():
    cfg = make_config()
    assert cfg.timezone == "Asia/Yekaterinburg"
    assert cfg.evening_hour == 20
    assert cfg.tick_seconds == 30
    assert cfg.max_repings == 3
    assert cfg.dialog_chat_id == 0
    assert cfg.max_delivery_failures == 3
    assert cfg.manual_time_timeout_minutes == 30
    assert cfg.calendar_retry_base_minutes == 2
    assert cfg.calendar_retry_max_minutes == 30
    assert cfg.caldav_timeout_seconds == 6


def test_dialog_chat_id_overridable():
    cfg = make_config(dialog_chat_id=555)
    assert cfg.dialog_chat_id == 555


def test_stems_parsed_from_csv():
    cfg = make_config(trigger_stems=" напомн, не забудь ,")
    assert cfg.stems == ["напомн", "не забудь"]


def test_the_default_db_and_media_dir_share_a_root():
    cfg = make_config()
    assert os.path.dirname(cfg.db_path) == os.path.dirname(cfg.monitor_media_dir.rstrip("/"))


def test_compose_keeps_the_media_dir_in_the_same_volume_as_the_database():
    """docker-compose overrode DB_PATH but not MONITOR_MEDIA_DIR, so media
    landed on /app/data/media -- the container's writable layer, wiped by
    every `docker compose up -d --build`, which is the documented deploy
    command. The rows in messages.media_path kept pointing at files that no
    longer existed, and backup_pull.sh rsynced an always-empty host directory,
    so the Mac archive never received a single file."""
    service = _compose_bot_service()
    env = service["environment"]
    db_root = os.path.dirname(env["DB_PATH"])
    media_root = os.path.dirname(env["MONITOR_MEDIA_DIR"].rstrip("/"))
    assert db_root == media_root, "the database and the media directory must share a root"

    mount_targets = {v.split(":")[1] for v in service["volumes"]}
    assert db_root in mount_targets, (
        f"{db_root} is not a mounted volume, so its contents do not survive a rebuild")
