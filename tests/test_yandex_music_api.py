from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


async def test_get_file_info_returns_exact_quality():
    api = make_api(
        [
            info("mp3", 128, "link-128"),
            info("mp3", 192, "link-192"),
            info("mp3", 320, "link-320"),
        ]
    )
    assert await api.get_file_info(1, quality="192") == "link-192"


async def test_get_file_info_falls_back_to_best_quality():
    api = make_api(
        [
            info("mp3", 128, "link-128"),
            info("mp3", 320, "link-320"),
        ]
    )
    assert await api.get_file_info(1, quality="192") == "link-320"


async def test_get_file_info_filters_by_codec():
    api = make_api(
        [
            info("aac", 256, "link-aac"),
            info("mp3", 128, "link-mp3"),
        ]
    )
    assert await api.get_file_info(1, codecs="mp3") == "link-mp3"


async def test_get_file_info_returns_none_without_track():
    client = MagicMock()
    client.tracks = AsyncMock(return_value=[])
    api = YandexMusicAPI(client=client)
    assert await api.get_file_info(1) is None
