import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from main_stream_service.main_stream_manager import (
    MainStreamManager,
    _CycleContext,
)
from main_stream_service.yandex_music_api import YandexMusicAPI
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from yandex_station.constants import (
    RUARK_IDLE_VOLUME,
    STREAM_POLL_INTERVAL,
)
from yandex_station.models import Track
from yandex_station.station_controls import YandexStationControls

# Настоящий sleep сохраняется до подмены в фикстуре fast_sleep
REAL_SLEEP = asyncio.sleep


@pytest.fixture
def fast_sleep(monkeypatch):
    """Подменяет asyncio.sleep, чтобы цикл крутился без задержек."""

    async def fake_sleep(delay, *args, **kwargs):
        await REAL_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def make_track(**kwargs) -> Track:
    defaults = dict(
        id="42",
        title="title",
        type="Track",
        artist="artist",
        duration=200.0,
        progress=10.0,
        playing=True,
    )
    defaults.update(kwargs)
    return Track(**defaults)


def make_station_controls(track=None, alice_states=("IDLE",), tracks=None):
    """Фейк станции.

    alice_states и tracks выдаются по очереди, последний элемент
    повторяется бесконечно.
    """
    controls = AsyncMock(spec=YandexStationControls)
    states = list(alice_states)

    async def next_state():
        return states.pop(0) if len(states) > 1 else states[0]

    controls.get_alice_state.side_effect = next_state

    if tracks is not None:
        queue = list(tracks)

        async def next_track():
            return queue.pop(0) if len(queue) > 1 else queue[0]

        controls.get_current_track.side_effect = next_track
    else:
        controls.get_current_track.return_value = track

    controls.get_volume.return_value = 0.5
    controls.get_radio_url.return_value = "http://radio/master.m3u8"
    return controls


def make_ruark(is_playing=True, volume=20):
    ruark = AsyncMock(spec=RuarkR5Controller)
    ruark.get_volume.return_value = volume
    ruark.is_playing.return_value = is_playing
    ruark.get_power_status.return_value = "1"
    ruark.get_session_id.return_value = "sid"
    return ruark


def make_manager(station_controls, ruark, track_url="http://track/url"):
    music_api = AsyncMock(spec=YandexMusicAPI)
    music_api.get_file_info.return_value = track_url
    ws_client = MagicMock()

    # Без событий цикл живёт на heartbeat; передача управления
    # обязательна, иначе цикл монополизирует event loop в тестах
    async def fake_wait(timeout):
        await REAL_SLEEP(0)
        return False

    ws_client.wait_for_state_update = AsyncMock(side_effect=fake_wait)
    manager = MainStreamManager(
        station_ws_client=ws_client,
        station_controls=station_controls,
        ruark_controls=ruark,
        yandex_music_api=music_api,
    )
    manager._send_track_to_stream_server = AsyncMock()
    return manager


async def drive_streaming(manager, until, timeout=1.0):
    """Крутит streaming() до выполнения условия или таймаута."""
    manager._stream_state_running = True
    task = asyncio.create_task(manager.streaming())
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not until():
            await REAL_SLEEP(0.005)
    finally:
        manager._stream_state_running = False
        await asyncio.wait_for(task, timeout=1.0)


async def test_new_track_sent_to_stream_server(fast_sleep):
    track = make_track(progress=2.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark())

    await drive_streaming(
        manager, until=lambda: manager._send_track_to_stream_server.called
    )

    manager._yandex_music_api.get_file_info.assert_called()
    call = manager._yandex_music_api.get_file_info.call_args
    assert call.kwargs["track_id"] == "42"
    send_call = manager._send_track_to_stream_server.call_args_list[0]
    assert send_call.args[0] == "http://track/url"
    assert send_call.kwargs["radio"] is False
    # Свежий трек (прогресс ниже порога) стартует с начала
    assert send_call.kwargs["start_position"] == pytest.approx(0.0)
    # Метаданные трека уезжают на стрим сервер для дисплея Ruark
    assert send_call.kwargs["title"] == "title"
    assert send_call.kwargs["artist"] == "artist"


async def test_radio_track_sent_with_radio_flag(fast_sleep):
    track = make_track(id="fm_jazz", type="FmRadio", duration=0.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark(is_playing=False))

    await drive_streaming(
        manager, until=lambda: manager._send_track_to_stream_server.called
    )

    station.get_radio_url.assert_called()
    assert manager._send_track_to_stream_server.called
    for call in manager._send_track_to_stream_server.call_args_list:
        url = call.args[0] if call.args else call.kwargs["track_url"]
        assert url == "http://radio/master.m3u8"
        assert call.kwargs["radio"] is True


async def test_alice_speech_ducks_ruark_volume(fast_sleep):
    track = make_track()
    # Первый вызов — инициализация last_alice_state, затем смена состояния
    station = make_station_controls(
        track, alice_states=("IDLE", "SPEAKING", "SPEAKING")
    )
    ruark = make_ruark()
    manager = make_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: any(
            call.args == (RUARK_IDLE_VOLUME,)
            for call in ruark.set_volume.call_args_list
        ),
    )

    ruark.set_volume.assert_any_call(RUARK_IDLE_VOLUME)


async def test_unmute_called_before_track_end(fast_sleep):
    track = make_track(duration=100.0, progress=99.5)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark())

    await drive_streaming(manager, until=lambda: station.unmute.called)

    station.unmute.assert_called()


async def test_track_without_url_is_not_sent(fast_sleep):
    track = make_track()
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark(), track_url=None)

    await drive_streaming(manager, until=lambda: False, timeout=0.15)

    manager._send_track_to_stream_server.assert_not_called()


async def test_missing_track_data_keeps_loop_alive(fast_sleep):
    station = make_station_controls(track=None)
    manager = make_manager(station, make_ruark())

    await drive_streaming(manager, until=lambda: False, timeout=0.15)

    manager._send_track_to_stream_server.assert_not_called()
    manager._ruark_controls.stop.assert_not_called()


async def test_track_repeat_resyncs_stream_from_start(fast_sleep):
    # Повтор трека: id не меняется, прогресс скачет к началу
    tracks = [make_track(progress=100.0)] * 4 + [make_track(progress=2.0)]
    station = make_station_controls(tracks=tracks)
    manager = make_manager(station, make_ruark())
    send = manager._send_track_to_stream_server

    def resync_happened():
        return any(
            call.kwargs.get("start_position") == pytest.approx(2.0)
            for call in send.call_args_list
        )

    await drive_streaming(manager, until=resync_happened)

    assert resync_happened(), "ресинк не сработал"


async def test_seek_forward_resyncs_stream_at_new_position(fast_sleep):
    tracks = [make_track(progress=10.0)] * 4 + [make_track(progress=120.0)]
    station = make_station_controls(tracks=tracks)
    manager = make_manager(station, make_ruark())
    send = manager._send_track_to_stream_server

    def resync_happened():
        return any(
            call.kwargs.get("start_position") == pytest.approx(120.0)
            for call in send.call_args_list
        )

    await drive_streaming(manager, until=resync_happened)

    assert resync_happened(), "ресинк не сработал"


async def test_resume_after_pause_resyncs_stream(fast_sleep):
    tracks = (
        [make_track(progress=50.0)] * 4
        + [make_track(progress=50.0, playing=False)] * 6
        + [make_track(progress=51.0)]
    )
    station = make_station_controls(tracks=tracks)
    manager = make_manager(station, make_ruark())
    send = manager._send_track_to_stream_server

    def resync_happened():
        return any(
            call.kwargs.get("start_position") == pytest.approx(51.0)
            for call in send.call_args_list
        )

    await drive_streaming(manager, until=resync_happened)

    assert resync_happened(), "ресинк не сработал"


async def test_normal_playback_does_not_trigger_resync(fast_sleep):
    tracks = [make_track(progress=float(p)) for p in range(10, 40)]
    station = make_station_controls(tracks=tracks)
    manager = make_manager(station, make_ruark())
    send = manager._send_track_to_stream_server

    await drive_streaming(manager, until=lambda: False, timeout=0.2)

    # Только первоначальная отправка трека, без ресинков
    assert send.call_count == 1


async def test_resync_ignores_stale_progress_snapshot():
    """Долгая итерация не должна выглядеть как скачок прогресса."""
    manager = make_manager(make_station_controls(make_track()), make_ruark())
    ctx = _CycleContext(
        last_track=make_track(progress=0.0),
        last_alice_state="IDLE",
        last_track_progress=0.0,
        last_progress_at=time.monotonic() - 6.0,  # снапшот протух на 6с
        last_track_playing=True,
    )

    # Станция уехала на ~6с за 6с реального времени — это не скачок
    await manager._resync_after_progress_jump(make_track(progress=6.0), ctx)

    manager._send_track_to_stream_server.assert_not_called()


async def test_resync_confirms_real_jump_on_second_tick():
    manager = make_manager(make_station_controls(make_track()), make_ruark())
    ctx = _CycleContext(
        last_track=make_track(progress=10.0),
        last_alice_state="IDLE",
        last_track_progress=10.0,
        last_progress_at=time.monotonic() - 0.5,
        last_track_playing=True,
    )
    send = manager._send_track_to_stream_server

    # Первый тик: скачок замечен, но ресинка нет — ждём подтверждения
    await manager._resync_after_progress_jump(make_track(progress=120.0), ctx)
    send.assert_not_called()
    assert ctx.jump_candidate is not None

    # Второй тик: станция продолжает с новой позиции — скачок настоящий
    await manager._resync_after_progress_jump(make_track(progress=120.4), ctx)
    send.assert_called_once()
    assert send.call_args.kwargs["start_position"] == pytest.approx(120.4)


async def test_resync_ignores_phantom_jump():
    """Станция рапортует ложный прыжок и возвращается к траектории."""
    manager = make_manager(make_station_controls(make_track()), make_ruark())
    ctx = _CycleContext(
        last_track=make_track(progress=79.0),
        last_alice_state="IDLE",
        last_track_progress=79.0,
        last_progress_at=time.monotonic() - 0.5,
        last_track_playing=True,
    )
    send = manager._send_track_to_stream_server

    # Ложный репорт: станция «прыгнула» на 108, реально оставаясь на ~79
    await manager._resync_after_progress_jump(make_track(progress=108.0), ctx)
    send.assert_not_called()

    # Следующий тик: станция продолжает старую траекторию — игнорируем
    await manager._resync_after_progress_jump(make_track(progress=80.0), ctx)
    send.assert_not_called()
    assert ctx.jump_candidate is None


async def test_stream_start_midtrack_continues_from_position(fast_sleep):
    """Стрим включён посреди трека — Ruark продолжает с позиции станции."""
    track = make_track(progress=120.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark())
    send = manager._send_track_to_stream_server

    await drive_streaming(manager, until=lambda: send.called)

    first_call = send.call_args_list[0]
    assert first_call.kwargs["start_position"] == pytest.approx(120.0)


async def test_loop_waits_for_station_events_with_heartbeat(fast_sleep):
    track = make_track(progress=2.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark())

    await drive_streaming(manager, until=lambda: False, timeout=0.1)

    wait_mock = manager._ws_client.wait_for_state_update
    wait_mock.assert_called_with(STREAM_POLL_INTERVAL)


async def test_silent_ruark_triggers_track_resend(fast_sleep, monkeypatch):
    """Станция играет трек, Ruark молчит — поток пересылается."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_RESEND_GRACE", 0.0
    )
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_CHECK_INTERVAL", 0.0
    )
    track = make_track(progress=50.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark(is_playing=False))
    send = manager._send_track_to_stream_server

    # Первая отправка — свитч, вторая — страховка от тишины
    await drive_streaming(manager, until=lambda: send.call_count >= 2)

    assert send.call_count >= 2
    resend_call = send.call_args_list[1]
    assert resend_call.kwargs["start_position"] == pytest.approx(50.0)


async def test_playing_ruark_does_not_trigger_resend(fast_sleep, monkeypatch):
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_RESEND_GRACE", 0.0
    )
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_CHECK_INTERVAL", 0.0
    )
    track = make_track(progress=50.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark(is_playing=True))
    send = manager._send_track_to_stream_server

    await drive_streaming(manager, until=lambda: False, timeout=0.2)

    # Только первоначальный свитч, страховка молчит
    assert send.call_count == 1
