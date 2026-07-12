from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Hue Bridge
    hue_bridge_host: str
    hue_bridge_app_key: str

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: list[int] = []

    # Rooms that should never take part in a dance — a strobing hallway at
    # midnight helps nobody. Matched case-insensitively, as a substring.
    dance_exclude_rooms: list[str] = []

    # General
    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]
