import asyncio
import importlib
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from injector import Injector
from upnpclient.soap import SOAPError

from core.enums import Codec
from dlna_stream_server.handlers import ffmpeg_supervisor
from dlna_stream_server.handlers import stream_handler as stream_handler_module
from dlna_stream_server.handlers.constants import FFMPEG_MP3_PARAMS
from dlna_stream_server.handlers.exceptions import UnsupportedCodecError
from dlna_stream_server.handlers.ffmpeg_supervisor import FfmpegSupervisor
from dlna_stream_server.handlers.stream_handler import (
    StreamHandler,
    _insert_start_position,
)
from ruark_audio_system.exceptions import RuarkDeviceNotFoundError
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from timeline import Timeline, coroutine_methods

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
    handler._get_ffmpeg_params = lambda codec, **kwargs: params


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


# Строки заголовка входа FFmpeg 7.1: MP3-192 и живое HLS-радио
INPUT_192_LINE = "  Duration: 00:03:00.04, start: 0.025057, bitrate: 192 kb/s"
INPUT_NA_LINE = "  Duration: N/A, start: 1.0, bitrate: N/A"


def stderr_script(line):
    """Команда, которая пишет строку в stderr и ждёт, как живой FFmpeg."""
    # exec: иначе осиротевший sleep держит stderr и стоп ждёт таймаута
    return ["sh", "-c", f"echo '{line}' >&2; exec sleep 30"]


def pacing_warned(handler, kbps, caplog):
    """Сообщает, дало ли окно замера с темпом kbps предупреждение."""
    with caplog.at_level(logging.WARNING):
        handler._log_stream_pacing(
            # Окно 30 с: kbps * 30 * 1000 / 8 байт
            window_bytes=int(kbps * 3750),
            elapsed=30.0,
            source_wait=0.0,
            client_wait=0.0,
        )
    return any(
        "ниже реального времени" in record.message for record in caplog.records
    )


async def test_start_launches_process_and_stop_terminates():
    handler, _ = make_handler()
    set_params(handler, ["sleep", "30"])

    await handler.start_ffmpeg_stream("http://example/track", Codec.MP3)
    proc = get_process(handler)
    assert proc is not None
    assert proc.returncode is None

    await handler.stop_ffmpeg()
    assert get_process(handler) is None
    assert proc.returncode is not None


async def test_new_start_replaces_old_process():
    handler, _ = make_handler()
    set_params(handler, ["sleep", "30"])

    await handler.start_ffmpeg_stream("http://example/one", Codec.MP3)
    first = get_process(handler)
    await handler.start_ffmpeg_stream("http://example/two", Codec.MP3)
    second = get_process(handler)

    assert second is not first
    # Старый процесс гасится фоновой задачей
    assert await wait_for_condition(lambda: first.returncode is not None)
    await handler.stop_ffmpeg()


async def test_track_error_exit_restarts_and_reattaches_ruark(fast_sleep):
    handler, ruark = make_handler()
    set_params(handler, ["sh", "-c", "exit 1"])

    await handler.start_ffmpeg_stream("http://example/track", Codec.MP3)

    # После аварийного выхода поток перезапускается и Ruark
    # заново получает URL стрима и команду play
    assert await wait_for_condition(lambda: ruark.play.called)
    ruark.set_av_transport_uri.assert_called()
    await handler.stop_ffmpeg()


async def test_track_natural_end_does_not_restart():
    handler, ruark = make_handler()
    set_params(handler, ["true"])

    await handler.start_ffmpeg_stream("http://example/track", Codec.MP3)
    await wait_for_condition(lambda: False, timeout=0.2)

    ruark.set_av_transport_uri.assert_not_called()
    ruark.play.assert_not_called()
    await handler.stop_ffmpeg()


async def test_radio_zero_exit_triggers_restart(fast_sleep):
    handler, ruark = make_handler()
    set_params(handler, ["true"])

    await handler.start_ffmpeg_stream(
        "http://example/master.m3u8", Codec.AAC, radio=True
    )

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

    await handler.play_stream("http://example/track", Codec.MP3)

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
        "http://example/track", Codec.MP3, title="Песня", artist="Артист"
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
    await handler.start_ffmpeg_stream("http://example/one", Codec.MP3)

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


async def test_replaced_process_window_not_judged(monkeypatch, caplog):
    """Хвост сменённого трека не сравнивается с порогом нового потока."""
    monkeypatch.setattr(stream_handler_module, "STREAM_RATE_WINDOW", 0.0)
    monkeypatch.setattr(ffmpeg_supervisor, "FFMPEG_TERM_TIMEOUT", 0.5)
    handler, _ = make_handler()
    # Старый процесс глух к SIGTERM: его хвост ещё читается после смены
    set_params(
        handler,
        [
            "sh",
            "-c",
            f"echo '{INPUT_192_LINE}' >&2; trap '' TERM; "
            "while :; do printf A; sleep 0.05; done",
        ],
    )
    await handler.start_ffmpeg_stream("http://example/one", Codec.MP3)
    old_client = (await handler.stream_audio()).body_iterator
    assert await read_chunk(old_client)

    set_params(handler, ["sleep", "30"])
    await handler.start_ffmpeg_stream("http://example/two", Codec.MP3)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await read_chunk(old_client)

    # Битрейт уже сброшен новым стартом: окно старого трека сравнилось
    # бы с запасным порогом и дало ложное предупреждение
    assert not [
        record
        for record in caplog.records
        if "ниже реального времени" in record.message
    ]
    await old_client.aclose()
    await handler.stop_ffmpeg()


def test_slow_flac_pacing_logged_as_warning(caplog):
    """FLAC на ~320 кбит/с — втрое ниже реального времени, WARNING."""
    handler, _ = make_handler()
    handler._stream_codec = Codec.FLAC

    with caplog.at_level(logging.WARNING):
        handler._log_stream_pacing(
            window_bytes=1_200_000,
            elapsed=30.0,
            source_wait=25.0,
            client_wait=1.0,
        )

    assert any(
        "ниже реального времени" in record.message for record in caplog.records
    )


def test_healthy_mp3_pacing_stays_quiet(caplog):
    """Тот же темп ~320 кбит/с для MP3 — норма, без WARNING."""
    handler, _ = make_handler()
    handler._stream_codec = Codec.MP3

    with caplog.at_level(logging.WARNING):
        handler._log_stream_pacing(
            window_bytes=1_200_000,
            elapsed=30.0,
            source_wait=0.5,
            client_wait=28.0,
        )

    assert not caplog.records


@pytest.mark.parametrize(
    ("kbps", "warned"), [(192, False), (155, False), (150, True), (100, True)]
)
async def test_pacing_threshold_follows_source_bitrate(kbps, warned, caplog):
    """Порог 192-кбит потока — 80 % битрейта (~154 кбит/с), а не 250."""
    handler, _ = make_handler()
    set_params(handler, stderr_script(INPUT_192_LINE))
    await handler.start_ffmpeg_stream("http://example/track", Codec.MP3)
    assert await wait_for_condition(
        lambda: handler._ffmpeg.source_bitrate_kbps == 192
    )

    assert pacing_warned(handler, kbps, caplog) is warned
    await handler.stop_ffmpeg()


@pytest.mark.parametrize(("kbps", "warned"), [(95, False), (80, True)])
async def test_radio_without_bitrate_keeps_codec_threshold(
    kbps, warned, caplog
):
    """Радио с `bitrate: N/A`: порог — прежняя константа AAC 90 кбит/с."""
    handler, _ = make_handler()
    set_params(handler, stderr_script(INPUT_NA_LINE))
    await handler.start_ffmpeg_stream(
        "http://example/master.m3u8", Codec.AAC, radio=True
    )
    await REAL_SLEEP(0.2)

    assert handler._ffmpeg.source_bitrate_kbps is None
    assert pacing_warned(handler, kbps, caplog) is warned
    await handler.stop_ffmpeg()


async def test_radio_declared_as_mpeg_in_didl(fast_sleep):
    """Радио: Content-Type остаётся aac, но в DIDL — audio/mpeg.

    Ресурс с protocolInfo audio/aac Ruark качает, но не воспроизводит —
    регрессия радио после перехода на mime по кодеку.
    """
    handler, ruark = make_handler()
    set_params(handler, ["sleep", "30"])

    await handler.play_stream(
        "http://example/master.m3u8", Codec.AAC, radio=True, title="ROCK FM"
    )

    assert handler.stream_media_type == "audio/aac"
    call = ruark.set_av_transport_uri.call_args
    assert call.kwargs["mime_type"] == "audio/mpeg"
    await handler.stop_ffmpeg()


async def test_radio_copied_to_adts_without_reencode(monkeypatch):
    """Радио: FFmpeg копирует AAC вещателя в ADTS без перекодирования."""
    launched = []
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*command, **kwargs):
        # Настоящего FFmpeg на CI нет: запоминаем команду, запускаем заглушку
        launched.append(list(command))
        return await real_exec("sleep", "30", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    handler, _ = make_handler()

    await handler.start_ffmpeg_stream(
        "http://example/master.m3u8", Codec.AAC, radio=True
    )

    command = launched[-1]
    assert command[command.index("-c:a") + 1] == "copy"
    assert command[command.index("-f") + 1] == "adts"
    await handler.stop_ffmpeg()


async def test_flac_codec_selects_flac_params_and_mime():
    handler, ruark = make_handler()
    # Фейковая команда: на CI-раннере нет ffmpeg
    set_params(handler, ["sleep", "30"])

    await handler.play_stream(
        "http://example/track.flac", Codec.FLAC, title="Т", artist="А"
    )

    assert handler.stream_media_type == "audio/flac"
    call = ruark.set_av_transport_uri.call_args
    assert call.kwargs["mime_type"] == "audio/flac"
    await handler.stop_ffmpeg()


async def test_terminate_closes_pipes_of_unread_process(monkeypatch, caplog):
    """Процесс без читателя pipe гасится, без зависшего wait().

    Переполненные stdout/stderr не отдают EOF: без закрытия каналов
    wait() не возвращается даже после kill(), процесс остаётся зомби.
    """
    monkeypatch.setattr(ffmpeg_supervisor, "FFMPEG_TERM_TIMEOUT", 0.5)
    supervisor = FfmpegSupervisor()
    # Игнорирует SIGTERM и заливает оба канала, которые никто не читает
    await supervisor.start(
        "http://example/track",
        ["sh", "-c", "trap '' TERM; yes DATADATA >&2 & yes DATADATA"],
    )
    proc = supervisor.process
    await REAL_SLEEP(0.5)

    started = time.monotonic()
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(supervisor.stop(), timeout=30)

    assert time.monotonic() - started < 3
    assert proc.returncode is not None
    assert not [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ]


async def test_terminate_keeps_graceful_stop_for_healthy_process():
    """Живому FFmpeg дают доиграть завершение, а не расстреливают каналы.

    Процесс закрывается по SIGTERM не мгновенно: закрытие каналов до
    таймаута обернулось бы SIGKILL и кодом -9.
    """
    supervisor = FfmpegSupervisor()
    await supervisor.start(
        "http://example/track",
        [
            "sh",
            "-c",
            "trap 'sleep 0.3; exit 0' TERM; sleep 30 >/dev/null 2>&1 & wait",
        ],
    )
    proc = supervisor.process
    # Ждём установки обработчика сигнала в запущенном процессе
    await REAL_SLEEP(0.3)

    await supervisor.stop()

    assert proc.returncode == 0


async def test_supervisor_publishes_source_bitrate_from_stderr():
    """Строка `bitrate: 192 kb/s` заголовка входа — битрейт источника 192."""
    supervisor = FfmpegSupervisor()

    await supervisor.start(
        "http://example/track", stderr_script(INPUT_192_LINE)
    )

    assert await wait_for_condition(
        lambda: supervisor.source_bitrate_kbps == 192
    )
    await supervisor.stop()


async def test_new_start_forgets_previous_source_bitrate():
    """Радио после трека 192: битрейт неизвестен, а не 192 от трека."""
    supervisor = FfmpegSupervisor()
    await supervisor.start(
        "http://example/track", stderr_script(INPUT_192_LINE)
    )
    assert await wait_for_condition(
        lambda: supervisor.source_bitrate_kbps == 192
    )

    await supervisor.start(
        "http://example/master.m3u8",
        stderr_script(INPUT_NA_LINE),
        radio=True,
    )
    await REAL_SLEEP(0.2)

    # Устаревшие 192 дали бы радио на ~112 кбит/с порог трека
    # и предупреждение на каждом окне замера
    assert supervisor.source_bitrate_kbps is None
    await supervisor.stop()


async def test_old_client_closes_without_touching_new_process():
    """Клиент снятого процесса завершается сам и не глушит новый поток."""
    handler, _ = make_handler()
    set_params(handler, ["sh", "-c", "while :; do printf A; sleep 0.05; done"])
    await handler.start_ffmpeg_stream("http://example/one", Codec.MP3)
    first = (await handler.stream_audio()).body_iterator
    assert await read_chunk(first)

    await handler.start_ffmpeg_stream("http://example/two", Codec.MP3)
    new_process = get_process(handler)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(first.__anext__(), timeout=10)
    assert get_process(handler) is new_process
    await handler.stop_ffmpeg()


async def test_execute_with_lock_reconnects_before_retry(fast_sleep):
    """Потерянный Ruark ищется заново, команда повторяется успешно."""
    handler, ruark = make_handler()
    ruark.play.side_effect = [RuarkDeviceNotFoundError("нет устройства"), None]
    ruark.reconnect.return_value = True

    await handler.execute_with_lock(ruark.play)

    ruark.reconnect.assert_awaited_once()
    assert ruark.play.await_count == 2


async def test_first_recovery_ignores_cooldown_after_boot(
    fast_sleep, monkeypatch
):
    """Малый monotonic() сразу после загрузки не блокирует восстановление."""
    handler, ruark = make_handler()
    ruark.play.side_effect = [RuarkDeviceNotFoundError("нет устройства"), None]
    ruark.reconnect.return_value = True
    monkeypatch.setattr(stream_handler_module.time, "monotonic", lambda: 10.0)

    await handler.execute_with_lock(ruark.play)

    ruark.reconnect.assert_awaited_once()


async def test_execute_with_lock_raises_after_all_attempts(fast_sleep):
    """Недостижимый Ruark — ошибка наружу, а не молчаливый успех."""
    handler, ruark = make_handler()
    ruark.play.side_effect = RuarkDeviceNotFoundError("нет устройства")
    ruark.reconnect.return_value = False

    with pytest.raises(RuarkDeviceNotFoundError):
        await handler.execute_with_lock(ruark.play)


async def test_play_stream_stops_ffmpeg_when_ruark_unreachable(fast_sleep):
    """Провал привязки Ruark гасит FFmpeg вместо холостого потока."""
    handler, ruark = make_handler()
    set_params(handler, ["sleep", "30"])
    ruark.set_av_transport_uri.side_effect = RuarkDeviceNotFoundError(
        "нет устройства"
    )
    ruark.reconnect.return_value = False

    with pytest.raises(RuarkDeviceNotFoundError):
        await handler.play_stream("http://example/track", Codec.MP3)

    assert get_process(handler) is None


async def test_aac_track_rejected_instead_of_mp3_container(monkeypatch):
    """AAC-трек без своего набора FFmpeg — понятная ошибка, а не MP3."""
    launched = []
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*command, **kwargs):
        launched.append(list(command))
        return await real_exec("sleep", "30", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    handler, _ = make_handler()

    with pytest.raises(UnsupportedCodecError):
        await handler.start_ffmpeg_stream(
            "http://example/track.m4a", codec=Codec.AAC
        )

    assert not launched
    assert get_process(handler) is None


def test_ffmpeg_params_for_flac_remux_without_reencode():
    from dlna_stream_server.handlers.constants import FFMPEG_FLAC_PARAMS

    handler, _ = make_handler()
    params = handler._get_ffmpeg_params(codec=Codec.FLAC)

    assert params is FFMPEG_FLAC_PARAMS
    assert "copy" in params
    assert "flac" in params


# --- Журнал команд Ruark с управляемым временем ---------------------------
#
# Что DLNA-сервер шлёт Ruark при привязке потока, в каком порядке и когда.
# FFmpeg заменён моком супервизора: его запуск и остановка — тоже записи
# журнала. Журнал снят с текущего кода: при выносе Ruark в плагин он
# должен остаться прежним.


def make_timeline_handler(monkeypatch):
    """Обработчик с FFmpeg-моком и часами monotonic для журнала."""
    handler, ruark = make_handler()
    handler._ffmpeg = AsyncMock(spec=FfmpegSupervisor)
    clock = [1000.0]
    fake_time = MagicMock(spec=time)
    fake_time.monotonic.side_effect = lambda: clock[0]
    monkeypatch.setattr(stream_handler_module, "time", fake_time)
    return handler, ruark, clock


def record_handler_timeline(monkeypatch, handler, clock):
    """Журнал команд Ruark, запусков FFmpeg и пауз (после настройки моков)."""
    timeline = Timeline(clock)
    timeline.spy(
        handler._ruark_controls,
        {
            name: f"ruark.{name}"
            for name in coroutine_methods(RuarkR5Controller)
        },
    )

    # Параметры FFmpeg проверяют свои тесты: в журнале только источник
    async def start(yandex_url, params, radio):
        timeline.note(f"ffmpeg.start({yandex_url!r}, radio={radio})")

    handler._ffmpeg.start.side_effect = start
    timeline.spy(handler._ffmpeg, {"stop": "ffmpeg.stop"})
    timeline.patch_sleep(monkeypatch)
    return timeline.entries


async def test_timeline_track_attached_and_played(monkeypatch):
    handler, _, clock = make_timeline_handler(monkeypatch)
    journal = record_handler_timeline(monkeypatch, handler, clock)

    await handler.play_stream(
        "http://example/track",
        Codec.MP3,
        start_position=42.5,
        title="Песня",
        artist="Артист",
    )

    assert journal == [
        "   0.0 ffmpeg.start('http://example/track', radio=False)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Песня', artist='Артист', mime_type='audio/mpeg')",
        "   0.0 ruark.play()",
    ]


async def test_timeline_radio_attached_as_mpeg(monkeypatch):
    handler, _, clock = make_timeline_handler(monkeypatch)
    journal = record_handler_timeline(monkeypatch, handler, clock)

    await handler.play_stream(
        "http://radio/master.m3u8", Codec.MP3, radio=True, title="Джаз"
    )

    assert journal == [
        "   0.0 ffmpeg.start('http://radio/master.m3u8', radio=True)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=true', title='Джаз', artist='', mime_type='audio/mpeg')",
        "   0.0 ruark.play()",
    ]


async def test_timeline_flac_attached_as_flac(monkeypatch):
    handler, _, clock = make_timeline_handler(monkeypatch)
    journal = record_handler_timeline(monkeypatch, handler, clock)

    await handler.play_stream(
        "http://example/track.flac", Codec.FLAC, title="Песня"
    )

    assert journal == [
        "   0.0 ffmpeg.start('http://example/track.flac', radio=False)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Песня', artist='', mime_type='audio/flac')",
        "   0.0 ruark.play()",
    ]


async def test_timeline_empty_title_replaced_by_default(monkeypatch):
    handler, _, clock = make_timeline_handler(monkeypatch)
    journal = record_handler_timeline(monkeypatch, handler, clock)

    await handler.play_stream("http://example/track", Codec.MP3)

    assert journal == [
        "   0.0 ffmpeg.start('http://example/track', radio=False)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Internet Radio', artist='', mime_type='audio/mpeg')",
        "   0.0 ruark.play()",
    ]


async def test_timeline_attach_failure_reconnects_and_retries(monkeypatch):
    handler, ruark, clock = make_timeline_handler(monkeypatch)
    ruark.set_av_transport_uri.side_effect = [
        RuarkDeviceNotFoundError("нет устройства"),
        None,
    ]
    ruark.reconnect.return_value = True
    journal = record_handler_timeline(monkeypatch, handler, clock)

    await handler.play_stream("http://example/track", Codec.MP3, title="Т")

    assert journal == [
        "   0.0 ffmpeg.start('http://example/track', radio=False)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Т', artist='', mime_type='audio/mpeg') !RuarkDeviceNotFoundError",
        "   0.0 ruark.reconnect() -> True",
        "   0.0 sleep(1)",
        "   1.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Т', artist='', mime_type='audio/mpeg')",
        "   1.0 ruark.play()",
    ]


async def test_timeline_reconnect_has_cooldown(monkeypatch):
    """Повторный сбой в пределах 30 с не запускает новый поиск Ruark."""
    handler, ruark, clock = make_timeline_handler(monkeypatch)
    ruark.play.side_effect = [
        RuarkDeviceNotFoundError("нет устройства"),
        None,
        RuarkDeviceNotFoundError("нет устройства"),
        None,
        RuarkDeviceNotFoundError("нет устройства"),
        None,
    ]
    ruark.reconnect.return_value = True
    journal = record_handler_timeline(monkeypatch, handler, clock)

    await handler.execute_with_lock(ruark.play)
    clock[0] += 10.0
    await handler.execute_with_lock(ruark.play)
    clock[0] += 30.0
    await handler.execute_with_lock(ruark.play)

    assert journal == [
        "   0.0 ruark.play() !RuarkDeviceNotFoundError",
        "   0.0 ruark.reconnect() -> True",
        "   0.0 sleep(1)",
        "   1.0 ruark.play()",
        "  11.0 ruark.play() !RuarkDeviceNotFoundError",
        "  11.0 sleep(1)",
        "  12.0 ruark.play()",
        "  42.0 ruark.play() !RuarkDeviceNotFoundError",
        "  42.0 ruark.reconnect() -> True",
        "  42.0 sleep(1)",
        "  43.0 ruark.play()",
    ]


async def test_timeline_attach_gives_up_after_three_attempts(monkeypatch):
    handler, ruark, clock = make_timeline_handler(monkeypatch)
    ruark.set_av_transport_uri.side_effect = RuarkDeviceNotFoundError(
        "нет устройства"
    )
    ruark.reconnect.return_value = False
    journal = record_handler_timeline(monkeypatch, handler, clock)

    with pytest.raises(RuarkDeviceNotFoundError):
        await handler.play_stream("http://example/track", Codec.MP3)

    assert journal == [
        "   0.0 ffmpeg.start('http://example/track', radio=False)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Internet Radio', artist='', mime_type='audio/mpeg') !RuarkDeviceNotFoundError",
        "   0.0 ruark.reconnect() -> False",
        "   0.0 sleep(1)",
        "   1.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Internet Radio', artist='', mime_type='audio/mpeg') !RuarkDeviceNotFoundError",
        "   1.0 sleep(1)",
        "   2.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Internet Radio', artist='', mime_type='audio/mpeg') !RuarkDeviceNotFoundError",
        "   2.0 ffmpeg.stop()",
    ]


async def test_timeline_play_failure_retries_only_play(monkeypatch):
    handler, ruark, clock = make_timeline_handler(monkeypatch)
    ruark.play.side_effect = [SOAPError("UPnP 501"), None]
    ruark.reconnect.return_value = True
    journal = record_handler_timeline(monkeypatch, handler, clock)

    await handler.play_stream("http://example/track", Codec.MP3, title="Т")

    assert journal == [
        "   0.0 ffmpeg.start('http://example/track', radio=False)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Т', artist='', mime_type='audio/mpeg')",
        "   0.0 ruark.play() !SOAPError",
        "   0.0 ruark.reconnect() -> True",
        "   0.0 sleep(1)",
        "   1.0 ruark.play()",
    ]


async def test_timeline_ffmpeg_restart_reattaches_with_saved_metadata(
    monkeypatch,
):
    handler, _, clock = make_timeline_handler(monkeypatch)
    journal = record_handler_timeline(monkeypatch, handler, clock)
    await handler.play_stream(
        "http://example/track.flac",
        Codec.FLAC,
        title="Песня",
        artist="Артист",
    )

    # Так супервизор зовёт обработчик после перезапуска FFmpeg
    await handler._reattach_ruark(False)

    assert journal == [
        "   0.0 ffmpeg.start('http://example/track.flac', radio=False)",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Песня', artist='Артист', mime_type='audio/flac')",
        "   0.0 ruark.play()",
        "   0.0 ruark.set_av_transport_uri('http://127.0.0.1:8080/live_stream.mp3?radio=false', title='Песня', artist='Артист', mime_type='audio/flac')",
        "   0.0 ruark.play()",
    ]


async def test_timeline_dlna_start_finds_ruark_in_background(monkeypatch):
    """Сервер стартует, не дожидаясь Ruark; при остановке поиск гасится.

    Ruark мог спать при старте: поиск идёт в фоне, а привязку потока
    потом восстановит execute_with_lock.
    """
    # Импорт модуля сервиса настраивает логи процесса: в тестах не нужно
    monkeypatch.setattr(
        "core.logging.setup.setup_logging", lambda *args, **kwargs: None
    )
    dlna_main = importlib.import_module("dlna_stream_server.main")
    handler, ruark, clock = make_timeline_handler(monkeypatch)
    search_cancelled = asyncio.Event()

    async def endless_search():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            search_cancelled.set()
            raise

    ruark.connect.side_effect = endless_search
    objects = {StreamHandler: handler, RuarkR5Controller: ruark}
    container = MagicMock(spec=Injector)
    container.get.side_effect = objects.__getitem__
    monkeypatch.setattr(
        dlna_main,
        "MainDIContainer",
        lambda: SimpleNamespace(get_container=lambda: container),
    )
    timeline = Timeline(clock)
    timeline.spy(ruark, {"connect": "ruark.connect"})
    timeline.spy(handler, {"stop_ffmpeg": "handler.stop_ffmpeg"})
    timeline.spy(handler._ffmpeg, {"stop": "ffmpeg.stop"})
    journal = timeline.entries
    app = FastAPI()

    lifespan = dlna_main.lifespan(app)
    # Старт, ждущий Ruark, упал бы здесь по таймауту, а не повис
    await asyncio.wait_for(lifespan.__aenter__(), timeout=1.0)
    await REAL_SLEEP(0)
    assert app.state.stream_handler is handler
    assert journal == ["   0.0 ruark.connect()"]

    await lifespan.__aexit__(None, None, None)
    await REAL_SLEEP(0)
    assert search_cancelled.is_set()
    assert journal == [
        "   0.0 ruark.connect()",
        "   0.0 handler.stop_ffmpeg()",
        "   0.0 ffmpeg.stop()",
    ]
