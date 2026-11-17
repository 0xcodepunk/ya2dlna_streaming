from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from injector import Injector
from pydantic import SecretStr, ValidationError

from core.authorization.yandex_tokens import get_device_token
from core.config.settings import Settings, settings
from core.dependencies.di_modules import YandexMusicAPIModule
from core.dependencies.main_di_container import MainDIContainer
from dlna_stream_server.handlers.stream_handler import StreamHandler
from main_stream_service.yandex_music_api import YandexMusicAPI
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller

TOKEN = "dummy-token"
PIN = "dummy-pin"


def fake_http_get(monkeypatch, body: dict) -> MagicMock:
    """Подменяет GET в aiohttp: мок отдаёт body и хранит заголовки запроса."""
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value=body)
    get = MagicMock()
    get.return_value.__aenter__.return_value = response
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)
    return get


def test_repr_hides_token_and_pin():
    config = Settings(
        _env_file=None,
        ya_music_token=TOKEN,
        ruark_pin=PIN,
        local_server_host="127.0.0.1",
        local_server_port_dlna=8000,
        local_server_port_api=8001,
    )

    for text in (repr(config), str(config)):
        assert TOKEN not in text
        assert PIN not in text


def test_validation_error_hides_token_and_pin(monkeypatch):
    monkeypatch.delenv("APP_LOCAL_SERVER_HOST", raising=False)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, ya_music_token=TOKEN, ruark_pin=PIN)

    text = str(error.value)
    assert "local_server_host" in text
    # Длинный ввод pydantic усекает, поэтому ищем общее начало значений
    assert "dummy" not in text


async def test_device_token_request_sends_real_token(monkeypatch):
    monkeypatch.setattr(settings, "ya_music_token", SecretStr(TOKEN))
    get = fake_http_get(monkeypatch, {"status": "ok", "token": "device"})

    await get_device_token("device-id", "platform")

    headers = get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "OAuth dummy-token"


async def test_lossless_request_sends_real_token(monkeypatch):
    monkeypatch.setattr(settings, "ya_music_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "prefer_lossless", True)
    get = fake_http_get(
        monkeypatch,
        {"result": {"downloadInfo": {"codec": "flac", "urls": ["link"]}}},
    )

    await YandexMusicAPI(client=MagicMock()).get_track_source(1)

    headers = get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "OAuth dummy-token"


def test_music_client_gets_real_token(monkeypatch):
    monkeypatch.setattr(settings, "ya_music_token", SecretStr(TOKEN))
    client_class = MagicMock()
    monkeypatch.setattr(
        "core.dependencies.di_modules.ClientAsync", client_class
    )

    Injector([YandexMusicAPIModule()]).get(YandexMusicAPI)

    client_class.assert_called_once_with("dummy-token")


# --- Настройки и сборка Ruark ----------------------------------------------
#
# Фиксируют то, что при выносе Ruark в плагин должно сохранить имена и
# смысл: APP_RUARK_PIN и APP_RUARK_HOST из .env, обязательный PIN и один
# экземпляр контроллера на процесс.


def write_env(path, **values) -> str:
    """Файл .env с обязательными настройками ядра и values поверх."""
    base = {
        "APP_YA_MUSIC_TOKEN": TOKEN,
        "APP_LOCAL_SERVER_HOST": "127.0.0.1",
        "APP_LOCAL_SERVER_PORT_DLNA": "8080",
        "APP_LOCAL_SERVER_PORT_API": "8001",
    }
    base.update(values)
    env = path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in base.items()))
    return str(env)


@pytest.fixture
def no_ruark_env(monkeypatch):
    """Настройки Ruark только из .env теста, не из окружения CI."""
    monkeypatch.delenv("APP_RUARK_PIN", raising=False)
    monkeypatch.delenv("APP_RUARK_HOST", raising=False)


def test_ruark_pin_and_host_from_env_file(no_ruark_env, tmp_path):
    env = write_env(tmp_path, APP_RUARK_PIN="4321", APP_RUARK_HOST="10.0.0.9")

    config = Settings(_env_file=env)

    assert config.ruark_pin.get_secret_value() == "4321"
    assert config.ruark_host == "10.0.0.9"


def test_ruark_pin_reaches_fsapi_client(monkeypatch):
    monkeypatch.setattr(
        "ruark_audio_system.ruark_r5_controller.settings.ruark_pin",
        SecretStr("4321"),
    )

    controller = RuarkR5Controller()

    assert controller._fsapi._pin == "4321"


def test_missing_ruark_pin_is_settings_error(no_ruark_env, tmp_path):
    env = write_env(tmp_path)

    with pytest.raises(ValidationError, match="ruark_pin"):
        Settings(_env_file=env)


def test_container_shares_one_ruark_per_process():
    """DLNA-обработчик получает тот же Ruark, что отдаёт контейнер."""
    container = MainDIContainer().get_container()

    ruark = container.get(RuarkR5Controller)

    assert type(ruark) is RuarkR5Controller
    assert container.get(StreamHandler)._ruark_controls is ruark
