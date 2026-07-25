from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str
    her_user_id: int
    admin_user_id: int
    # Defense in depth on top of the Telegram-side "only these chats" Business
    # toggle: when set, business_message is ignored unless it also arrives in
    # this exact chat. 0 disables the check (scoping relies on Telegram alone).
    dialog_chat_id: int = 0
    timezone: str = "Asia/Yekaterinburg"

    apple_id: str = ""
    apple_app_password: str = ""
    caldav_url: str = "https://caldav.icloud.com"
    calendar_name: str = "Домашний"
    calendar_url: str = ""
    caldav_timeout_seconds: int = 15

    trigger_stems: str = "напомн,не забудь,поставь напоминание"
    morning_hour: int = 9
    day_hour: int = 14
    evening_hour: int = 20
    event_duration_minutes: int = 60

    tick_seconds: int = 30
    reping_minutes: int = 30
    max_repings: int = 3
    snooze_minutes: int = 60
    # Consecutive failed ping *deliveries* (send_message raising, not just an
    # unanswered ping) before a reminder is given up on — see flow._ping.
    max_delivery_failures: int = 3
    # How long an "awaiting manual time" flag stays valid — see
    # flow.on_her_private_text / db.get_awaiting_manual.
    manual_time_timeout_minutes: int = 30
    # Backoff for the scheduler's calendar-repair retries — see
    # flow._calendar_retry_due.
    calendar_retry_base_minutes: int = 2
    calendar_retry_max_minutes: int = 30

    db_path: str = "data/reminders.sqlite3"

    @property
    def stems(self) -> list[str]:
        return [s.strip().lower() for s in self.trigger_stems.split(",") if s.strip()]
