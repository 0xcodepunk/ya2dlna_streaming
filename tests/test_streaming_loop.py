import asyncio
import itertools
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config.settings import settings
from main_stream_service.main_stream_manager import (
    MainStreamManager,
    _CycleContext,
)
from main_stream_service.yandex_music_api import TrackSource, YandexMusicAPI
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from yandex_station.constants import (
    RUARK_IDLE_VOLUME,
    RUARK_MIN_VOICE_VOLUME,
    RUARK_VOLUME_PER_STATION_STEP,
    SILENT_RESEND_LIMIT,
    STREAM_POLL_INTERVAL,
    TRACK_SOURCE_TTL,
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
    controls.working_volume = 0.5
    # Гашение пропущено: станция уже заглушена
    controls.fade_out_alice_volume.return_value = None
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
    music_api.get_track_source.return_value = (
        TrackSource(url=track_url, codec="mp3") if track_url else None
    )
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


def set_source_by_id(manager):
    """Источник с уникальной ссылкой по id — чтобы отличать треки."""

    async def resolver(track_id, quality=None):
        return TrackSource(url=f"http://track/{track_id}", codec="mp3")

    manager._yandex_music_api.get_track_source.side_effect = resolver


def source_calls_for(manager, track_id):
    """Вызовы get_track_source для конкретного трека."""
    return [
        call
        for call in manager._yandex_music_api.get_track_source.call_args_list
        if call.kwargs.get("track_id") == track_id
    ]


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

    manager._yandex_music_api.get_track_source.assert_called()
    call = manager._yandex_music_api.get_track_source.call_args
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


async def test_silent_radio_not_respammed_within_grace(fast_sleep):
    """Молчащий Ruark на радио не бомбардируется перепривязками."""
    # Прогресс радио тикает каждую итерацию — как на живой станции
    tracks = [
        make_track(
            id="fm_jazz", type="FmRadio", duration=0.0, progress=float(p)
        )
        for p in range(1, 60)
    ]
    station = make_station_controls(tracks=tracks)
    manager = make_manager(station, make_ruark(is_playing=False))
    send = manager._send_track_to_stream_server

    await drive_streaming(manager, until=lambda: False, timeout=0.3)

    # Только первоначальный свитч: грейс не даёт слать каждую итерацию
    assert send.call_count == 1


async def test_silent_radio_resent_after_grace(fast_sleep, monkeypatch):
    """После грейса радио пересылается, если Ruark так и молчит."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_RESEND_GRACE", 0.0
    )
    tracks = [
        make_track(
            id="fm_jazz", type="FmRadio", duration=0.0, progress=float(p)
        )
        for p in range(1, 60)
    ]
    station = make_station_controls(tracks=tracks)
    manager = make_manager(station, make_ruark(is_playing=False))
    send = manager._send_track_to_stream_server

    await drive_streaming(manager, until=lambda: send.call_count >= 2)

    assert send.call_count >= 2
    resume_call = send.call_args_list[1]
    assert resume_call.kwargs["radio"] is True


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


async def test_resync_detects_real_jump_with_fresh_snapshot():
    manager = make_manager(make_station_controls(make_track()), make_ruark())
    ctx = _CycleContext(
        last_track=make_track(progress=10.0),
        last_alice_state="IDLE",
        last_track_progress=10.0,
        last_progress_at=time.monotonic() - 0.5,
        last_track_playing=True,
    )

    await manager._resync_after_progress_jump(make_track(progress=120.0), ctx)

    send = manager._send_track_to_stream_server
    send.assert_called_once()
    assert send.call_args.kwargs["start_position"] == pytest.approx(120.0)


async def test_long_reply_iteration_does_not_resync(fast_sleep, monkeypatch):
    """Итерация с гашением дольше порога скачка не пересылает поток."""
    # Ожидание Ruark и гашение после реплики заняли 7 с, а станция
    # за это время честно проиграла те же 7 с трека
    clock = [1000.0]
    fake_time = MagicMock()
    fake_time.monotonic.side_effect = lambda: clock[0]
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.time", fake_time
    )
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.5},
    )
    fades = []

    async def track_by_clock():
        return make_track(progress=10.0 + clock[0] - 1000.0)

    async def slow_fade():
        fades.append(1)
        if len(fades) == 1:
            clock[0] += 7.0

    station.get_current_track.side_effect = track_by_clock
    station.fade_out_alice_volume.side_effect = slow_fade
    manager = make_voice_manager(station, make_ruark())

    await drive_streaming(manager, until=lambda: False, timeout=0.2)

    # Только отправка нового трека, без ресинка «разрыва прогресса»
    assert manager._send_track_to_stream_server.call_count == 1


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


async def test_silent_ruark_stops_streaming_after_resend_limit(
    fast_sleep, monkeypatch
):
    """Пересылки без звука не длятся вечно — стриминг останавливается."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_RESEND_GRACE", 0.0
    )
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_CHECK_INTERVAL", 0.0
    )
    track = make_track(progress=50.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark(is_playing=False))
    manager._stop_stream_on_stream_server = AsyncMock()
    send = manager._send_track_to_stream_server

    await drive_streaming(
        manager, until=lambda: not manager._stream_state_running
    )
    if manager._shutdown_task:
        await manager._shutdown_task

    assert manager._stream_state_running is False
    # Первоначальный свитч плюс лимит пересылок-страховок
    assert send.call_count == SILENT_RESEND_LIMIT + 1
    station.say.assert_called()


async def test_silent_ruark_stops_streaming_once(fast_sleep, monkeypatch):
    """Витки цикла после отсечки не запускают вторую остановку."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_RESEND_GRACE", 0.0
    )
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_CHECK_INTERVAL", 0.0
    )
    track = make_track(progress=50.0)
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark(is_playing=False))
    manager._stop_stream_on_stream_server = AsyncMock()
    stop_calls = 0
    real_stop = manager.stop

    async def counting_stop():
        nonlocal stop_calls
        stop_calls += 1
        await REAL_SLEEP(0.05)
        await real_stop()

    manager.stop = counting_stop

    await drive_streaming(
        manager,
        until=lambda: False,
        timeout=0.3,
    )
    if manager._shutdown_task:
        await manager._shutdown_task

    assert stop_calls == 1
    assert station.say.call_count == 1


async def test_playing_ruark_resets_silent_resend_streak(
    fast_sleep, monkeypatch
):
    """Заигравший Ruark обнуляет серию — стриминг не останавливается."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_RESEND_GRACE", 0.0
    )
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_CHECK_INTERVAL", 0.0
    )
    track = make_track(progress=50.0)
    station = make_station_controls(track)
    ruark = make_ruark()
    # Тишина, пересылка, снова звук — и так по кругу
    states = itertools.cycle([False, False, True])

    async def next_state(*args, **kwargs):
        return next(states)

    ruark.is_playing.side_effect = next_state
    manager = make_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: manager._shutdown_task is not None,
        timeout=0.5,
    )

    assert manager._shutdown_task is None
    station.say.assert_not_called()


async def test_powered_off_ruark_stops_streaming(fast_sleep, monkeypatch):
    """Ruark выключен пользователем — стриминг останавливается сам."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_RESEND_GRACE", 0.0
    )
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.SILENCE_CHECK_INTERVAL", 0.0
    )
    track = make_track(progress=50.0)
    station = make_station_controls(track)
    ruark = make_ruark(is_playing=False)
    ruark.get_power_status.return_value = "0"
    manager = make_manager(station, ruark)
    manager._stop_stream_on_stream_server = AsyncMock()
    send = manager._send_track_to_stream_server

    await drive_streaming(
        manager, until=lambda: not manager._stream_state_running
    )

    assert manager._stream_state_running is False
    # Поток не пересылался выключенному устройству
    assert send.call_count == 1  # только первоначальный свитч
    station.unmute.assert_called()


async def test_next_track_prefetched_in_background(fast_sleep):
    """Ссылка следующего трека резолвится фоном, пока играет текущий."""
    track = make_track(next_id="43")
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark())
    set_source_by_id(manager)

    await drive_streaming(
        manager, until=lambda: len(source_calls_for(manager, "43")) > 0
    )

    assert len(source_calls_for(manager, "43")) == 1
    # Предзагрузка ничего не отправляет — сосед ещё не играет
    for call in manager._send_track_to_stream_server.call_args_list:
        assert call.args[0] == "http://track/42"


async def test_switch_uses_prefetched_link(fast_sleep):
    """Переключение вперёд берёт ссылку из кеша, а не резолвит заново."""
    first = make_track(id="42", next_id="43")
    second = make_track(id="43", prev_id="42", progress=0.5)
    station = make_station_controls(tracks=[first] * 8 + [second])
    manager = make_manager(station, make_ruark())
    set_source_by_id(manager)
    send = manager._send_track_to_stream_server

    await drive_streaming(
        manager,
        until=lambda: any(
            call.args[0] == "http://track/43" for call in send.call_args_list
        ),
    )

    # Единственный резолв — фоновый префетч, свитч попал в кеш
    assert len(source_calls_for(manager, "43")) == 1


async def test_return_to_previous_track_uses_cache(fast_sleep):
    """Возврат к прошлому треку не ждёт резолва — ссылка уже в кеше."""
    first = make_track(id="42", next_id="43")
    second = make_track(id="43", prev_id="42", progress=0.5)
    back = make_track(id="42", next_id="43", progress=1.0)
    station = make_station_controls(tracks=[first] * 6 + [second] * 6 + [back])
    manager = make_manager(station, make_ruark())
    set_source_by_id(manager)
    send = manager._send_track_to_stream_server

    def returned_to_first():
        sent = [call.args[0] for call in send.call_args_list]
        return sent.count("http://track/42") >= 2

    await drive_streaming(manager, until=returned_to_first)

    # Трек 42 резолвился один раз — при первом свитче
    assert len(source_calls_for(manager, "42")) == 1


async def test_resync_reuses_cached_source(fast_sleep):
    """Ресинк после перемотки не резолвит ссылку заново."""
    tracks = [make_track(progress=10.0)] * 4 + [make_track(progress=120.0)]
    station = make_station_controls(tracks=tracks)
    manager = make_manager(station, make_ruark())
    set_source_by_id(manager)
    send = manager._send_track_to_stream_server

    def resync_happened():
        return any(
            call.kwargs.get("start_position") == pytest.approx(120.0)
            for call in send.call_args_list
        )

    await drive_streaming(manager, until=resync_happened)

    # Свитч плюс ресинк — а резолв ссылки только один
    assert len(source_calls_for(manager, "42")) == 1


async def test_radio_neighbors_not_prefetched(fast_sleep):
    """Для радио предзагрузка соседних треков не запускается."""
    track = make_track(
        id="fm_jazz", type="FmRadio", duration=0.0, next_id="43"
    )
    station = make_station_controls(track)
    manager = make_manager(station, make_ruark(is_playing=False))

    await drive_streaming(manager, until=lambda: False, timeout=0.15)

    manager._yandex_music_api.get_track_source.assert_not_called()


async def test_cached_source_expires_after_ttl():
    """Протухшая запись кеша не используется и удаляется."""
    manager = make_manager(make_station_controls(make_track()), make_ruark())
    stale = TrackSource(url="http://track/old", codec="mp3")
    manager._source_cache["42"] = (
        stale,
        time.monotonic() - TRACK_SOURCE_TTL - 1.0,
    )

    assert manager._get_cached_source("42") is None
    assert "42" not in manager._source_cache


async def test_start_stops_ws_client_when_ruark_not_found():
    """Ruark не найден при параллельном старте — WebSocket гасится."""
    station = make_station_controls(make_track())
    ruark = make_ruark()
    ruark.connect.return_value = False
    ruark.device_name = "Ruark R5"
    manager = make_manager(station, ruark)

    await manager.start()

    assert manager._stream_state_running is False
    station.start_ws_client.assert_called_once()
    station.stop_ws_client.assert_called_once()


async def test_start_announces_missing_ruark_by_voice():
    """Ruark не найден — станция голосом объясняет причину."""
    station = make_station_controls(make_track())
    ruark = make_ruark()
    ruark.connect.return_value = False
    ruark.device_name = "Ruark R5"
    manager = make_manager(station, ruark)

    await manager.start()

    station.say.assert_awaited_once()
    phrase = station.say.call_args.args[0]
    assert "Руарк" in phrase


async def test_start_stays_silent_when_station_unreachable():
    """Недоступна сама станция — озвучивать ошибку нечем."""
    station = make_station_controls(make_track())
    station.start_ws_client.side_effect = RuntimeError("станция не найдена")
    manager = make_manager(station, make_ruark())

    await manager.start()

    assert manager._stream_state_running is False
    station.say.assert_not_called()


async def test_repeated_crashes_announce_error_once(fast_sleep, monkeypatch):
    """Серия падений цикла — одно голосовое уведомление, без спама."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.STREAMING_RESTART_DELAY",
        0.0,
    )
    station = make_station_controls(make_track())
    manager = make_manager(station, make_ruark())
    manager.streaming = AsyncMock(side_effect=RuntimeError("бум"))
    manager._stream_state_running = True

    task = asyncio.create_task(manager._wrap_streaming())
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not station.say.called:
        await REAL_SLEEP(0.005)
    # Даём циклу упасть ещё несколько раз после объявления
    await REAL_SLEEP(0.05)
    manager._stream_state_running = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    station.say.assert_awaited_once_with(
        "Стрим сломался и сам не восстанавливается, загляни в логи"
    )
    station.unmute.assert_called()


async def test_short_failure_streak_stays_silent(fast_sleep, monkeypatch):
    """Пара падений с восстановлением — голосовое уведомление не нужно."""
    monkeypatch.setattr(
        "main_stream_service.main_stream_manager.STREAMING_RESTART_DELAY",
        0.0,
    )
    station = make_station_controls(make_track())
    manager = make_manager(station, make_ruark())
    calls = {"count": 0}

    async def flaky_streaming():
        calls["count"] += 1
        if calls["count"] <= 2:
            raise RuntimeError("бум")
        manager._stream_state_running = False

    manager.streaming = flaky_streaming
    manager._stream_state_running = True

    await manager._wrap_streaming()

    station.say.assert_not_called()


async def test_stop_survives_unreachable_ruark():
    """Недоступный Ruark не прерывает остановку стриминга."""
    station = make_station_controls(make_track())
    ruark = make_ruark()
    ruark.stop.side_effect = RuntimeError("нет связи")
    ruark.set_volume.side_effect = RuntimeError("нет связи")
    ruark.turn_power_off.side_effect = RuntimeError("нет связи")
    manager = make_manager(station, ruark)
    manager._stop_stream_on_stream_server = AsyncMock()
    manager._volume_store = MagicMock()

    await manager.stop()

    # Главные шаги дошли до конца, несмотря на мёртвый Ruark
    station.unmute.assert_called()
    station.stop_ws_client.assert_called()


def make_voice_station(alice_states, volume_by_state, track=None):
    """Фейк станции, громкость которой зависит от состояния Алисы.

    Рабочий уровень станции, который возвращает unmute, — 0.5. Как
    настоящая станция, ответ на unmute сообщает прежнюю громкость:
    новый уровень виден только в следующих состояниях.
    """
    station = make_station_controls(
        track or make_track(), alice_states=alice_states
    )
    current = {"state": alice_states[0]}
    next_state = station.get_alice_state.side_effect

    async def tracked_state():
        current["state"] = await next_state()
        return current["state"]

    async def volume():
        return volume_by_state[current["state"]]

    station.get_alice_state.side_effect = tracked_state
    station.get_volume.side_effect = volume
    station.unmute.return_value = True
    return station


def make_voice_manager(station, ruark):
    """Менеджер без сохранённой громкости: база Ruark — снимок дакинга."""
    manager = make_manager(station, ruark)
    manager._volume_store = MagicMock()
    manager._volume_store.load.return_value = None
    return manager


async def test_voice_volume_up_moves_to_ruark(fast_sleep):
    """«Алиса, громче» во время трека прибавляет Ruark, а не станцию."""
    # Станция замьючена стримом, unmute перед репликой вернул ей 0.5,
    # а к IDLE пришло 0.7 — два шага «громче»
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.7},
    )
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)
    louder = 20 + 2 * RUARK_VOLUME_PER_STATION_STEP

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((louder,),),
    ]
    # Станция сразу, без плавного снижения, возвращается на свой уровень
    station.set_volume.assert_awaited_once_with(0.5)


async def test_late_voice_volume_moved_after_fade(fast_sleep):
    """Прибавка, пришедшая после первой проверки, уходит на Ruark."""
    # К первой IDLE-итерации станция ещё на рабочем уровне, а гашение
    # через секунду читает уже 0.7 и, как настоящее, принимает его
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.5},
    )
    fades = []

    async def late_fade():
        fades.append(1)
        if len(fades) > 1:
            return None
        station.working_volume = 0.7
        return 0.7

    station.fade_out_alice_volume.side_effect = late_fade
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: len(fades) > 0)

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
        ((20 + 2 * RUARK_VOLUME_PER_STATION_STEP,),),
    ]
    # Голос Алисы не громчает: рабочий уровень прежний
    assert station.working_volume == 0.5


async def test_stale_raise_in_fade_after_move_keeps_working_volume(
    fast_sleep,
):
    """Перенесённая прибавка, прочитанная гашением ещё раз, не копится."""
    # Первая проверка перенесла 0.5 ➝ 0.6 и вернула станцию на 0.5,
    # но гашение прочитало устаревшее 0.6 и, как настоящее, приняло его
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.6},
    )
    fades = []

    async def stale_fade():
        fades.append(1)
        if len(fades) > 1:
            return None
        station.working_volume = 0.6
        return 0.6

    station.fade_out_alice_volume.side_effect = stale_fade
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: len(fades) > 0)

    # Прибавка перенесена один раз, а не повторно после гашения
    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20 + RUARK_VOLUME_PER_STATION_STEP,),),
    ]
    assert station.working_volume == 0.5


async def test_move_restores_working_level_from_before_reply(fast_sleep):
    """После переноса рабочий уровень — громкость перед репликой."""
    # Сеанс прочитал устаревший ноль, гашения ещё не было: unmute ничего
    # не отправил, и базой стали прочитанные 0.6, а гашение прочитало 0.7
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.6, "IDLE": 0.7},
    )
    station.working_volume = 0.0
    station.unmute.return_value = False
    fades = []

    async def stale_fade():
        fades.append(1)
        if len(fades) > 1:
            return None
        station.working_volume = 0.7
        return 0.7

    station.fade_out_alice_volume.side_effect = stale_fade
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: len(fades) > 0)

    station.set_volume.assert_awaited_once_with(0.6)
    # Устаревший ноль сделал бы Алису немой до конца сеанса
    assert station.working_volume == 0.6


async def test_late_move_restores_working_level_from_before_reply(fast_sleep):
    """Поздний перенос тоже возвращает громкость перед репликой."""
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.6, "IDLE": 0.6},
    )
    station.working_volume = 0.0
    station.unmute.return_value = False
    fades = []

    async def late_fade():
        fades.append(1)
        if len(fades) > 1:
            return None
        station.working_volume = 0.7
        return 0.7

    station.fade_out_alice_volume.side_effect = late_fade
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: len(fades) > 0)

    assert ruark.set_volume.call_args_list[-1] == (
        (20 + RUARK_VOLUME_PER_STATION_STEP,),
    )
    assert station.working_volume == 0.6


async def test_voice_volume_capped_at_ruark_maximum(fast_sleep):
    """Прибавка не выводит Ruark за верхнюю границу громкости."""
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 1.0},
    )
    ruark = make_ruark(volume=98)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list[-1] == ((100,),)


async def test_raise_from_zero_working_volume_is_not_louder(fast_sleep):
    """При нулевом рабочем уровне подъём станции не уходит на Ruark."""
    # Сеанс начался с замьюченной станции: unmute возвращает ноль,
    # Алису не слышно, и слушатель сам поднимает станцию до 0.6
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.6},
    )
    station.working_volume = 0.0
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
    ]
    station.set_volume.assert_not_awaited()


async def test_voice_volume_disabled_keeps_old_behavior(
    fast_sleep, monkeypatch
):
    """Ключ выключен — «громче» остаётся на станции, Ruark прежний."""
    monkeypatch.setattr(settings, "voice_volume_to_ruark", False)
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.7},
    )
    # Гашение тоже видит прибавку — поздний перенос не срабатывает
    station.fade_out_alice_volume.return_value = 0.7
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
    ]
    station.set_volume.assert_not_awaited()


async def test_own_unmute_before_reply_is_not_louder(fast_sleep):
    """Unmute стрима перед репликой Алисы не прибавляет Ruark."""
    # Станция на нуле, стрим возвращает ей рабочий уровень 0.5,
    # после реплики громкость так и остаётся рабочей
    station = make_voice_station(
        alice_states=("IDLE", "SPEAKING", "IDLE"),
        volume_by_state={"SPEAKING": 0.0, "IDLE": 0.5},
    )
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    station.unmute.assert_awaited()
    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
    ]
    station.set_volume.assert_not_awaited()


async def test_voice_volume_moved_once_per_reply(fast_sleep):
    """На паузе прибавка переносится один раз, а не каждую итерацию."""
    # Станция всё ещё показывает прибавку (её состояние отстаёт),
    # а трек на паузе — эпизод речи не закрывается гашением
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.7},
        track=make_track(playing=False),
    )
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: False, timeout=0.2)

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20 + 2 * RUARK_VOLUME_PER_STATION_STEP,),),
    ]
    station.set_volume.assert_awaited_once_with(0.5)


async def test_volume_rise_while_alice_active_is_not_command(fast_sleep):
    """Пока Алиса слушает или говорит, рост громкости не переносится."""
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "SPEAKING"),
        volume_by_state={"LISTENING": 0.0, "SPEAKING": 0.7},
    )
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: False, timeout=0.15)

    assert ruark.set_volume.call_args_list == [((RUARK_IDLE_VOLUME,),)]
    station.set_volume.assert_not_awaited()


async def test_volume_rise_after_fade_is_not_command(fast_sleep):
    """После гашения реплика закрыта: рост громкости не переносится."""
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.5},
    )
    by_state = station.get_volume.side_effect

    async def rises_after_fade():
        if station.fade_out_alice_volume.await_count:
            return 0.7
        return await by_state()

    station.get_volume.side_effect = rises_after_fade
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: False, timeout=0.2)

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
    ]
    station.set_volume.assert_not_awaited()


async def test_voice_volume_down_moves_to_ruark(fast_sleep):
    """«Алиса, тише» во время трека убавляет Ruark, а не станцию."""
    # Unmute перед репликой вернул станции 0.5, к IDLE пришло 0.4 —
    # один шаг «тише»
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.4},
    )
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20 - RUARK_VOLUME_PER_STATION_STEP,),),
    ]
    # Станция сразу, без плавного снижения, возвращается на свой уровень
    station.set_volume.assert_awaited_once_with(0.5)


async def test_late_voice_volume_down_moved_after_fade(fast_sleep):
    """«Тише», пришедшее после первой проверки, убавляет Ruark."""
    # К первой IDLE-итерации станция ещё на рабочем уровне, а гашение
    # через секунду читает уже 0.3 и, как настоящее, принимает его
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.5},
    )
    fades = []

    async def late_fade():
        fades.append(1)
        if len(fades) > 1:
            return None
        station.working_volume = 0.3
        return 0.3

    station.fade_out_alice_volume.side_effect = late_fade
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: len(fades) > 0)

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
        ((20 - 2 * RUARK_VOLUME_PER_STATION_STEP,),),
    ]
    # Голос Алисы не тише: рабочий уровень прежний
    assert station.working_volume == 0.5


async def test_voice_volume_down_stops_at_ruark_minimum(fast_sleep):
    """«Громкость 0» опускает Ruark до нижней границы, а не в тишину."""
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.0},
    )
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list[-1] == ((RUARK_MIN_VOICE_VOLUME,),)
    station.set_volume.assert_awaited_once_with(0.5)


async def test_voice_volume_down_never_raises_quiet_ruark(fast_sleep):
    """Ruark ниже границы «тише» не поднимает до неё."""
    # Ручкой Ruark выкручен до 4 — тише границы, но не приглушение
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.4},
    )
    ruark = make_ruark(volume=4)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((4,),),
    ]


async def test_reply_after_cut_fade_counts_from_working_volume(fast_sleep):
    """Реплика после прерванного гашения считается от рабочего уровня."""
    # Гашение прервано и увело станцию в ноль, но чтение ещё помнит
    # ступень 0.3; unmute возвращает рабочие 0.5, «громче» даёт 0.6
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.3, "IDLE": 0.6},
    )
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    station.unmute.assert_awaited()
    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20 + RUARK_VOLUME_PER_STATION_STEP,),),
    ]
    station.set_volume.assert_awaited_once_with(0.5)


async def test_new_reply_interrupts_wait_for_ruark(fast_sleep):
    """Новая реплика не ждёт Ruark и не перезапускает трек."""
    # Ruark после реплики не подтверждает воспроизведение, а слушатель
    # уже снова сказал «Алиса»
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE", "LISTENING"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.5},
    )
    ruark = make_ruark(is_playing=False, volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager, until=lambda: ruark.set_volume.call_count >= 3
    )

    # Новая реплика сразу приглушила Ruark
    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
        ((RUARK_IDLE_VOLUME,),),
    ]
    # Трек отправлен один раз — при переключении, без перезапуска
    assert manager._send_track_to_stream_server.call_count == 1


async def test_voice_volume_down_disabled_lowers_station_only(
    fast_sleep, monkeypatch
):
    """Ключ выключен — «тише» убавляет рабочий уровень станции."""
    monkeypatch.setattr(settings, "voice_volume_to_ruark", False)
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.4},
    )

    async def fade():
        station.working_volume = 0.4
        return 0.4

    station.fade_out_alice_volume.side_effect = fade
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
    ]
    station.set_volume.assert_not_awaited()
    # Голос Алисы тише, как до переноса
    assert station.working_volume == 0.4


async def test_raise_from_button_zero_is_not_quieter(fast_sleep):
    """«Громче» со станции, опущенной кнопкой до нуля, не убавляет Ruark."""
    # На паузе станция стоит на рабочем уровне, её опустили кнопкой
    # до нуля: unmute ничего не отправляет, и «громче» даёт 0.1
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.1},
        track=make_track(playing=False),
    )
    station.unmute.return_value = False
    ruark = make_ruark(volume=20)
    manager = make_voice_manager(station, ruark)

    await drive_streaming(manager, until=lambda: False, timeout=0.2)

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
    ]
    station.set_volume.assert_not_awaited()


async def test_volume_change_on_pause_is_not_voice_command(fast_sleep):
    """Громкость, изменённая на паузе не голосом, не уходит на Ruark."""
    # «Алиса, пауза»: гашения нет, станция ждёт продолжения на рабочем
    # уровне. Кнопкой её убавили до 0.2 и продолжили из приложения
    station = make_voice_station(
        alice_states=("IDLE", "LISTENING", "IDLE"),
        volume_by_state={"LISTENING": 0.0, "IDLE": 0.5},
    )
    ruark = make_ruark(volume=20)
    paused, playing = make_track(playing=False), make_track()
    by_state = station.get_volume.side_effect

    async def resumed_from_app():
        return playing if ruark.set_volume.await_count > 1 else paused

    async def knob_on_pause():
        if ruark.set_volume.await_count > 1:
            return 0.2
        return await by_state()

    async def fade():
        station.working_volume = 0.2
        return 0.2

    station.get_current_track.side_effect = resumed_from_app
    station.get_volume.side_effect = knob_on_pause
    station.fade_out_alice_volume.side_effect = fade
    manager = make_voice_manager(station, ruark)

    await drive_streaming(
        manager,
        until=lambda: station.fade_out_alice_volume.await_count > 0,
    )

    assert ruark.set_volume.call_args_list == [
        ((RUARK_IDLE_VOLUME,),),
        ((20,),),
    ]
    # Голос Алисы остаётся таким, каким его сделали кнопкой
    assert station.working_volume == 0.2
