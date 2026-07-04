import pytest

from dlna_stream_server.handlers.utils import extract_index_url

PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=128000\n"
    "/kal/fm_rock/ysign1=abc,pfx/index-a1.m3u8?vsid=123&uuid=456\n"
)


def test_extract_index_url_joins_with_playlist_host():
    url = extract_index_url(
        "https://strm-m9-82.strm.yandex.net/kal/fm_rock/master.m3u8?x=1",
        PLAYLIST,
    )
    assert url == (
        "https://strm-m9-82.strm.yandex.net"
        "/kal/fm_rock/ysign1=abc,pfx/index-a1.m3u8?vsid=123&uuid=456"
    )


def test_extract_index_url_raises_without_index():
    with pytest.raises(ValueError):
        extract_index_url("https://example.com/master.m3u8", "#EXTM3U\n")
