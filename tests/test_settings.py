from unittest.mock import AsyncMock, MagicMock

import aiohttp
from injector import Injector
from pydantic import SecretStr

from core.authorization.yandex_tokens import get_device_token
from core.config.settings import Settings, settings
from core.dependencies.di_modules import YandexMusicAPIModule
from main_stream_service.yandex_music_api import YandexMusicAPI

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
