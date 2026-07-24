from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str
    her_user_id: int
    admin_user_id: int
    timezone: str = "Asia/Yekaterinburg"

    apple_id: str = ""
    apple_app_password: str = ""
    caldav_url: str = "https://caldav.icloud.com"
    calendar_name: str = "Домашний"
    calendar_url: str = ""

    trigger_stems: str = "напомн,не забудь,поставь напоминание"
    morning_hour: int = 9
    day_hour: int = 14
    evening_hour: int = 20
    event_duration_minutes: int = 60

    tick_seconds: int = 30
    reping_minutes: int = 30
    max_repings: int = 3
    snooze_minutes: int = 60

    db_path: str = "data/reminders.sqlite3"

    @property
    def stems(self) -> list[str]:
        return [s.strip().lower() for s in self.trigger_stems.split(",") if s.strip()]
