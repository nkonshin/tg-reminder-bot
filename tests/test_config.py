from src.config import Config


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
