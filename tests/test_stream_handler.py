import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from dlna_stream_server.handlers.constants import FFMPEG_MP3_PARAMS
from dlna_stream_server.handlers.stream_handler import (
    StreamHandler,
    _insert_start_position,
)
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller

# Настоящий sleep сохраняется до подмены в фикстуре fast_sleep
REAL_SLEEP = asyncio.sleep


@pytest.fixture
def fast_sleep(monkeypatch):
    """Подменяет asyncio.sleep, чтобы задержки рестартов не тормозили тест."""

    async def fake_sleep(delay, *args, **kwargs):
        await REAL_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def make_handler():
    ruark = AsyncMock(spec=RuarkR5Controller)
    return StreamHandler(ruark), ruark


def set_params(handler, params):
    """Подменяет выбор параметров FFmpeg на тестовую команду."""
    handler._get_ffmpeg_params = lambda codec, is_local_file=False: params


def get_process(handler):
    """Возвращает текущий процесс независимо от внутренней структуры."""
    supervisor = getattr(handler, "_ffmpeg", None)
    if supervisor is not None and hasattr(supervisor, "process"):
        return supervisor.process
    return handler._ffmpeg_process


async def wait_for_condition(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await REAL_SLEEP(0.01)
    return condition()


async def test_start_launches_process_and_stop_terminates():
    handler, _ = make_handler()
    set_params(handler, ["sleep", "30"])

    await handler.start_ffmpeg_stream("http://example/track")
    proc = get_process(handler)
    assert proc is not None
    assert proc.returncode is None

    await handler.stop_ffmpeg()
    assert get_process(handler) is None
    assert proc.returncode is not None


async def test_new_start_replaces_old_process():
    handler, _ = make_handler()
    set_params(handler, ["sleep", "30"])

    await handler.start_ffmpeg_stream("http://example/one")
    first = get_process(handler)
    await handler.start_ffmpeg_stream("http://example/two")
    second = get_process(handler)

    assert second is not first
    # Старый процесс гасится фоновой задачей
    assert await wait_for_condition(lambda: first.returncode is not None)
    await handler.stop_ffmpeg()


async def test_track_error_exit_restarts_and_reattaches_ruark(fast_sleep):
    handler, ruark = make_handler()
    set_params(handler, ["sh", "-c", "exit 1"])

    await handler.start_ffmpeg_stream("http://example/track")

    # После аварийного выхода поток перезапускается и Ruark
    # заново получает URL стрима и команду play
    assert await wait_for_condition(lambda: ruark.play.called)
    ruark.set_av_transport_uri.assert_called()
    await handler.stop_ffmpeg()


async def test_track_natural_end_does_not_restart():
    handler, ruark = make_handler()
    set_params(handler, ["true"])

    await handler.start_ffmpeg_stream("http://example/track")
    await wait_for_condition(lambda: False, timeout=0.2)

    ruark.set_av_transport_uri.assert_not_called()
    ruark.play.assert_not_called()
    await handler.stop_ffmpeg()


async def test_radio_zero_exit_triggers_restart(fast_sleep):
    handler, ruark = make_handler()
    set_params(handler, ["true"])

    await handler.start_ffmpeg_stream("http://example/master.m3u8", radio=True)

    # Радио перезапускается даже при нормальном коде выхода
    assert await wait_for_condition(lambda: ruark.play.called)
    await handler.stop_ffmpeg()


async def test_stream_audio_without_process_returns_404():
    handler, _ = make_handler()

    with pytest.raises(HTTPException) as exc_info:
        await handler.stream_audio()

    assert exc_info.value.status_code == 404


async def test_play_stream_points_ruark_at_local_stream():
    handler, ruark = make_handler()
    set_params(handler, ["sleep", "30"])

    await handler.play_stream("http://example/track")

    call = ruark.set_av_transport_uri.call_args
    stream_url = call.args[0] if call.args else call.kwargs["uri"]
    assert "/live_stream.mp3" in stream_url
    assert "radio=false" in stream_url
    ruark.play.assert_called()
    await handler.stop_ffmpeg()


def test_insert_start_position_places_ss_before_input():
    params = _insert_start_position(FFMPEG_MP3_PARAMS, 42.5)
    ss_index = params.index("-ss")
    assert params[ss_index + 1] == "42.5"
    assert params[ss_index + 2] == "-i"


def test_insert_start_position_zero_keeps_params_unchanged():
    assert _insert_start_position(FFMPEG_MP3_PARAMS, 0.0) == list(
        FFMPEG_MP3_PARAMS
    )


async def test_play_stream_passes_metadata_to_ruark():
    handler, ruark = make_handler()
    set_params(handler, ["sleep", "30"])

    await handler.play_stream(
        "http://example/track", title="Песня", artist="Артист"
    )

    call = ruark.set_av_transport_uri.call_args
    assert call.kwargs["title"] == "Песня"
    assert call.kwargs["artist"] == "Артист"
    await handler.stop_ffmpeg()


async def read_chunk(agen, timeout=3.0):
    """Читает следующий чанк из генератора стрима с таймаутом."""
    return await asyncio.wait_for(agen.__anext__(), timeout)


async def test_new_client_displaces_previous_stream():
    handler, _ = make_handler()
    set_params(handler, ["sh", "-c", "while :; do printf A; sleep 0.05; done"])
    await handler.start_ffmpeg_stream("http://example/one")

    first = (await handler.stream_audio()).body_iterator
    assert await read_chunk(first)

    # Второй клиент (переподключение Ruark) забирает поток себе
    second = (await handler.stream_audio()).body_iterator
    assert await read_chunk(second)

    # Первый закрывается и больше не ворует байты из pipe
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(first.__anext__(), timeout=3.0)

    await second.aclose()
    await handler.stop_ffmpeg()


async def test_flac_codec_selects_flac_params_and_mime():
    handler, ruark = make_handler()
    # Фейковая команда: на CI-раннере нет ffmpeg
    set_params(handler, ["sleep", "30"])

    await handler.play_stream(
        "http://example/track.flac", codec="flac", title="Т", artist="А"
    )

    assert handler.stream_media_type == "audio/flac"
    call = ruark.set_av_transport_uri.call_args
    assert call.kwargs["mime_type"] == "audio/flac"
    await handler.stop_ffmpeg()


def test_ffmpeg_params_for_flac_remux_without_reencode():
    from dlna_stream_server.handlers.constants import FFMPEG_FLAC_PARAMS

    handler, _ = make_handler()
    params = handler._get_ffmpeg_params(codec="flac")

    assert params is FFMPEG_FLAC_PARAMS
    assert "copy" in params
    assert "flac" in params
