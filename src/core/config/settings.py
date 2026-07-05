import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_env_path() -> Path:
    """Определяет путь к файлу .env."""
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / ".env"
    return Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    """Конфигурация приложения."""

    model_config = SettingsConfigDict(
        env_file=resolve_env_path(),
        env_prefix="APP_",
        extra="ignore",
    )

    # Yandex Music settings
    ya_music_token: str

    # API server settings
    local_server_host: str
    local_server_port_dlna: int
    local_server_port_api: int

    # Ruark R5 settings
    ruark_pin: str
    # Известный IP Ruark: быстрый старт без SSDP-скана всей сети
    ruark_host: str | None = None

    # Mode settings
    debug: bool = False

    # Stream settings
    stream_quality: str = "192"
    stream_is_local_file: bool = False
    # Предпочитать lossless (FLAC), когда он доступен для трека
    prefer_lossless: bool = True


settings = Settings()
