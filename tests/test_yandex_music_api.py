from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config.settings import settings
from main_stream_service.yandex_music_api import YandexMusicAPI


def make_api(download_info):
    """Создаёт YandexMusicAPI с замоканным клиентом Яндекс.Музыки."""
    track = MagicMock()
    track.get_download_info_async = AsyncMock(return_value=download_info)
    client = MagicMock()
    client.tracks = AsyncMock(return_value=[track])
    return YandexMusicAPI(client=client)


def info(codec: str, bitrate: int, link: str) -> SimpleNamespace:
    return SimpleNamespace(
        codec=codec, bitrate_in_kbps=bitrate, direct_link=link
    )


async def test_get_track_source_returns_none_without_track(monkeypatch):
    monkeypatch.setattr(settings, "prefer_lossless", False)
    client = MagicMock()
    client.tracks = AsyncMock(return_value=[])
    api = YandexMusicAPI(client=client)
    monkeypatch.setattr(api, "_get_lossless_url", AsyncMock(return_value=None))

    assert await api.get_track_source(1, quality="320") is None


async def test_get_track_source_prefers_lossless(monkeypatch):
    monkeypatch.setattr(settings, "prefer_lossless", True)
    api = make_api([info("mp3", 320, "link-320")])
    lossless_mock = AsyncMock(return_value="https://cdn/flac-url")
    monkeypatch.setattr(api, "_get_lossless_url", lossless_mock)

    source = await api.get_track_source("42", quality="320")

    assert source is not None
    assert source.codec == "flac"
    assert source.url == "https://cdn/flac-url"


async def test_get_track_source_falls_back_to_mp3(monkeypatch):
    monkeypatch.setattr(settings, "prefer_lossless", True)
    api = make_api([info("mp3", 320, "link-320")])
    monkeypatch.setattr(api, "_get_lossless_url", AsyncMock(return_value=None))

    source = await api.get_track_source("42", quality="320")

    assert source is not None
    assert source.codec == "mp3"
    assert source.url == "link-320"


MP3_320 = info("mp3", 320, "link-mp3-320")
MP3_192 = info("mp3", 192, "link-mp3-192")
AAC_256 = info("aac", 256, "link-aac-256")
AAC_128 = info("aac", 128, "link-aac-128")
FLAC = ("flac", "https://cdn/flac")


@pytest.mark.parametrize(
    (
        "prefer_lossless",
        "lossless_url",
        "download_info",
        "quality",
        "expected",
    ),
    [
        # Lossless выключен: MP3 запрошенного качества, AAC от 256,
        # лучший MP3, разовое lossless-спасение
        (False, FLAC[1], [AAC_256, MP3_320, MP3_192], "320", ("mp3", MP3_320)),
        (False, FLAC[1], [AAC_256, MP3_192], "320", ("aac", AAC_256)),
        (False, FLAC[1], [AAC_128, MP3_192], "320", ("mp3", MP3_192)),
        (False, FLAC[1], [MP3_320, MP3_192, AAC_256], "192", ("mp3", MP3_192)),
        (False, FLAC[1], [AAC_256], "320", ("aac", AAC_256)),
        (False, FLAC[1], [AAC_128], "320", FLAC),
        (False, FLAC[1], [], "320", FLAC),
        (False, None, [AAC_128], "320", None),
        # Lossless включён: он первым, дальше тот же порядок без спасения
        (True, FLAC[1], [AAC_256, MP3_320], "320", FLAC),
        (True, None, [AAC_256, MP3_320, MP3_192], "320", ("mp3", MP3_320)),
        (True, None, [AAC_256, MP3_192], "320", ("aac", AAC_256)),
        (True, None, [AAC_128, MP3_192], "320", ("mp3", MP3_192)),
        (True, None, [AAC_128], "320", None),
    ],
)
async def test_get_track_source_matrix(
    monkeypatch,
    prefer_lossless,
    lossless_url,
    download_info,
    quality,
    expected,
):
    """Матрица «доступные кодеки × настройка lossless» → источник трека."""
    monkeypatch.setattr(settings, "prefer_lossless", prefer_lossless)
    api = make_api(download_info)
    lossless_mock = AsyncMock(return_value=lossless_url)
    monkeypatch.setattr(api, "_get_lossless_url", lossless_mock)

    source = await api.get_track_source("42", quality=quality)

    # Lossless запрашивается не больше одного раза: первым или спасением
    assert lossless_mock.await_count <= 1
    if expected is None:
        assert source is None
        return
    codec, link = expected
    assert source is not None
    assert source.codec == codec
    assert source.url == getattr(link, "direct_link", link)
    if not prefer_lossless and codec != "flac":
        lossless_mock.assert_not_awaited()


async def test_get_track_source_fetches_download_info_once(monkeypatch):
    """Все ступени выбора берутся из одного ответа download_info."""
    monkeypatch.setattr(settings, "prefer_lossless", False)
    api = make_api([AAC_128, MP3_192])

    await api.get_track_source("42", quality="320")

    api._client.tracks.assert_awaited_once()


async def test_get_track_source_skips_lossless_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "prefer_lossless", False)
    api = make_api([info("mp3", 320, "link-320")])
    lossless_mock = AsyncMock(return_value="https://cdn/flac-url")
    monkeypatch.setattr(api, "_get_lossless_url", lossless_mock)

    source = await api.get_track_source("42", quality="320")

    assert source is not None
    assert source.codec == "mp3"
    lossless_mock.assert_not_awaited()
