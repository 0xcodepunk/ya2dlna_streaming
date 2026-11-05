import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from yandex_station.protobuf_parser import Protobuf
from yandex_station.station_controls import YandexStationControls
from yandex_station.station_ws_control import YandexStationClient
from yandex_station.volume_store import StationVolumeStore

BASE_PLAYER_STATE = {
    "id": "42",
    "title": "title",
    "type": "Track",
    "subtitle": "artist",
    "duration": 200,
    "progress": 10,
}


def make_controls(player_state) -> YandexStationControls:
    """Контролы станции с замоканным состоянием плеера."""
    ws_client = AsyncMock(spec=YandexStationClient)
    ws_client.get_latest_message.return_value = {
        "state": {"playerState": player_state, "playing": True}
    }
    return YandexStationControls(
        ws_client=ws_client,
        protobuf=MagicMock(spec=Protobuf),
    )


async def test_current_track_carries_neighbor_ids():
    controls = make_controls(
        {
            **BASE_PLAYER_STATE,
            "entityInfo": {
                "next": {"id": "43", "type": "Track"},
                "prev": {"id": "41", "type": "Track"},
            },
        }
    )

    track = await controls.get_current_track()

    assert track is not None
    assert track.next_id == "43"
    assert track.prev_id == "41"


async def test_non_track_neighbors_are_ignored():
    controls = make_controls(
        {
            **BASE_PLAYER_STATE,
            "entityInfo": {
                "next": {"id": "gen", "type": "Generative"},
                "prev": "мусор",
            },
        }
    )

    track = await controls.get_current_track()

    assert track is not None
    assert track.next_id is None
    assert track.prev_id is None


async def test_missing_entity_info_is_safe():
    controls = make_controls(dict(BASE_PLAYER_STATE))

    track = await controls.get_current_track()

    assert track is not None
    assert track.next_id is None
    assert track.prev_id is None


async def test_say_sends_repeat_command_to_station():
    """say() превращает фразу в команду локального TTS."""
    controls = make_controls(dict(BASE_PLAYER_STATE))

    await controls.say("тестовая фраза")

    controls._ws_client.send_command.assert_awaited_once_with(
        {"command": "sendText", "text": "Повтори за мной тестовая фраза"}
    )


async def test_fade_returns_volume_seen_before_fade(monkeypatch):
    """Гашение отдаёт громкость, с которой начало, и уводит в ноль."""

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    ws_client = AsyncMock(spec=YandexStationClient)
    ws_client.get_latest_message.return_value = {"state": {"volume": 0.7}}
    controls = YandexStationControls(
        ws_client=ws_client,
        protobuf=MagicMock(spec=Protobuf),
    )

    assert await controls.fade_out_alice_volume() == 0.7
    ws_client.send_command.assert_awaited_with(
        {"command": "setVolume", "volume": 0.0}
    )


def make_fading_controls(volume, alice_states):
    """Контролы станции, где состояние Алисы меняется после команд.

    Станция сообщает громкость volume. alice_states выдаются по одному
    на каждую отправленную команду, последний элемент повторяется.
    """
    states = list(alice_states)

    async def latest():
        return {"state": {"volume": volume, "aliceState": states[0]}}

    async def send(command):
        if len(states) > 1:
            states.pop(0)
        return {}

    ws_client = AsyncMock(spec=YandexStationClient)
    ws_client.get_latest_message.side_effect = latest
    ws_client.send_command.side_effect = send
    controls = YandexStationControls(
        ws_client=ws_client,
        protobuf=MagicMock(spec=Protobuf),
    )
    return controls


def sent_volumes(controls):
    """Громкости, отправленные станции командами setVolume."""
    return [
        call.args[0]["volume"]
        for call in controls._ws_client.send_command.await_args_list
    ]


async def test_fade_stops_when_alice_listens_again(monkeypatch):
    """Новая реплика прерывает гашение: станция сразу уходит в ноль."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    # Пока шёл первый шаг гашения, слушатель снова сказал «Алиса»
    controls = make_fading_controls(0.6, ("IDLE", "LISTENING"))

    await controls.fade_out_alice_volume()

    # После первого шага — сразу ноль, без оставшихся ступеней
    assert sent_volumes(controls)[1:] == [0.0]
    # Реплика вернёт станции уровень, с которого шло гашение
    assert await controls.unmute()
    assert sent_volumes(controls)[-1] == 0.6


async def test_fade_reads_late_volume_when_alice_already_listens(monkeypatch):
    """Алиса уже слушает — гашение всё равно ждёт запоздавшую громкость."""
    controls = make_fading_controls(0.6, ("LISTENING",))
    ws_client = controls._ws_client

    async def late_command(delay):
        # К концу паузы станция сообщила команду прошлой реплики
        ws_client.get_latest_message.side_effect = None
        ws_client.get_latest_message.return_value = {
            "state": {"volume": 0.7, "aliceState": "LISTENING"}
        }

    monkeypatch.setattr(asyncio, "sleep", late_command)

    assert await controls.fade_out_alice_volume() == 0.7
    assert sent_volumes(controls) == [0.0]


@pytest.mark.parametrize(
    "volume", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
)
async def test_fade_starts_from_current_volume(monkeypatch, volume):
    """Гашение начинается с текущей громкости, а не на шаг ниже."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    controls = make_fading_controls(volume, ("IDLE",))

    await controls.fade_out_alice_volume()

    assert sent_volumes(controls)[0] == volume


@pytest.mark.parametrize(
    ("volume", "levels"),
    [
        (0.3, [0.3, 0.2, 0.1, 0.0]),
        (0.4, [0.4, 0.3, 0.2, 0.1, 0.0]),
        (0.6, [0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0]),
        (1.0, [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0]),
    ],
)
async def test_fade_sends_each_level_once(monkeypatch, volume, levels):
    """Гашение проходит каждый уровень один раз, ноль не повторяется."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    controls = make_fading_controls(volume, ("IDLE",))

    await controls.fade_out_alice_volume()

    assert sent_volumes(controls) == levels


async def test_unmute_reports_level_until_station_confirms(monkeypatch):
    """Пока станция не подтвердила уровень, unmute считает его своим."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    controls = make_fading_controls(0.6, ("IDLE",))
    ws_client = controls._ws_client
    await controls.fade_out_alice_volume()
    assert await controls.unmute()
    assert controls.level_unconfirmed
    sent = len(sent_volumes(controls))

    # Станция ещё сообщает прежний ноль: уровень в пути, второй раз не шлём
    ws_client.get_latest_message.side_effect = None
    ws_client.get_latest_message.return_value = {"state": {"volume": 0.0}}
    await controls.get_volume()
    assert await controls.unmute()
    assert len(sent_volumes(controls)) == sent

    # Станция подтвердила уровень: новый ноль уже не наш, а с кнопки
    ws_client.get_latest_message.return_value = {"state": {"volume": 0.6}}
    await controls.get_volume()
    ws_client.get_latest_message.return_value = {"state": {"volume": 0.0}}
    await controls.get_volume()
    assert not await controls.unmute()
    assert not controls.level_unconfirmed


def report_volume(controls, volume):
    """Станция сообщает новую громкость; None — сообщений ещё нет."""
    ws_client = controls._ws_client
    ws_client.get_latest_message.side_effect = None
    ws_client.get_latest_message.return_value = (
        None if volume is None else {"state": {"volume": volume}}
    )


@pytest.mark.parametrize("volume", [0.0, 0.1])
async def test_loop_restart_keeps_working_volume_of_muted_station(
    monkeypatch, volume
):
    """Перезапуск цикла не принимает след гашения за рабочий уровень."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    controls = make_fading_controls(0.6, ("IDLE",))
    await controls.fade_out_alice_volume()
    report_volume(controls, volume)

    await controls.set_default_volume()

    assert controls.working_volume == 0.6
    assert await controls.unmute()
    assert sent_volumes(controls)[-1] == 0.6


def test_volume_store_roundtrip(tmp_path: Path):
    """Сохранённый уровень читается обратно, без файла его нет."""
    store = StationVolumeStore(path=tmp_path / "volume.txt")

    assert store.load() is None
    store.save(0.6)
    assert store.load() == 0.6


@pytest.mark.parametrize("content", ["мусор", "0.0", "-0.3", "1.5"])
def test_volume_store_ignores_impossible_level(tmp_path: Path, content: str):
    """Мусор и значения вне диапазона считаются отсутствием уровня."""
    path = tmp_path / "volume.txt"
    path.write_text(content)

    assert StationVolumeStore(path=path).load() is None


def make_reporting_controls(volume):
    """Контролы станции, сообщающей volume; None — сообщений ещё нет."""
    controls = make_fading_controls(volume, ("IDLE",))
    report_volume(controls, volume)
    return controls


@pytest.mark.parametrize("volume", [0.0, None])
async def test_new_session_takes_saved_level_from_silent_station(
    station_volume_file, volume
):
    """Ноль или молчание на старте: станцию заглушил прошлый сеанс."""
    station_volume_file.write_text("0.6")
    controls = make_reporting_controls(volume)

    await controls.set_default_volume()

    assert controls.working_volume == 0.6
    assert await controls.unmute()
    assert sent_volumes(controls) == [0.6]


async def test_next_process_gets_level_of_previous(monkeypatch):
    """Ctrl+C посреди стрима: новый процесс вернёт прежний уровень."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    previous = make_reporting_controls(0.6)
    await previous.set_default_volume()
    await previous.fade_out_alice_volume()

    restarted = make_reporting_controls(0.0)
    await restarted.set_default_volume()

    assert await restarted.unmute()
    assert sent_volumes(restarted) == [0.6]


async def test_working_volume_saved_on_every_change(monkeypatch):
    """Каждое изменение рабочего уровня переживает перезапуск."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    store = StationVolumeStore()
    controls = make_reporting_controls(0.6)

    await controls.set_default_volume()
    assert store.load() == 0.6

    report_volume(controls, 0.7)
    await controls.mute()
    assert store.load() == 0.7

    await controls.unmute()
    report_volume(controls, 0.5)
    await controls.fade_out_alice_volume()
    assert store.load() == 0.5

    controls.working_volume = 0.4
    assert store.load() == 0.4


async def test_zero_does_not_erase_saved_level(monkeypatch):
    """Ноль в рабочем уровне не стирает настоящий уровень в файле."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    store = StationVolumeStore()
    controls = make_reporting_controls(0.0)
    controls.working_volume = 0.6

    controls.working_volume = 0.0
    assert store.load() == 0.6

    await controls.mute()
    assert store.load() == 0.6

    await controls.unmute()
    await controls.fade_out_alice_volume()
    assert store.load() == 0.6


async def test_nonzero_read_drops_assumed_mute(
    monkeypatch, station_volume_file
):
    """Ноль на старте был устаревшим: станцию гасим, иначе играла бы с Ruark."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    station_volume_file.write_text("0.6")
    controls = make_reporting_controls(0.0)
    await controls.set_default_volume()
    assert await controls.fade_out_alice_volume() is None

    report_volume(controls, 0.6)
    await controls.get_volume()

    assert await controls.fade_out_alice_volume() == 0.6
    assert sent_volumes(controls)[-1] == 0.0


async def test_nonzero_read_keeps_real_mute(monkeypatch):
    """Устаревшее чтение сразу после гашения не снимает заглушку стрима."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    controls = make_reporting_controls(0.6)
    await controls.fade_out_alice_volume()

    await controls.get_volume()

    assert await controls.fade_out_alice_volume() is None
    assert await controls.unmute()
    assert sent_volumes(controls)[-1] == 0.6


@pytest.mark.parametrize(
    "raise_level",
    [
        pytest.param(lambda controls: controls.unmute(), id="unmute"),
        pytest.param(
            lambda controls: controls.set_volume(0.6), id="set_volume"
        ),
    ],
)
async def test_late_push_keeps_mute_after_assumption_ended(
    monkeypatch, station_volume_file, raise_level
):
    """Команда сняла предположение: поздний пуш её уровня заглушку не снимет."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    station_volume_file.write_text("0.6")
    controls = make_reporting_controls(0.0)
    await controls.set_default_volume()
    await raise_level(controls)
    # Станция ещё сообщает ноль, а гашение уже заглушило её по-настоящему
    await controls.fade_out_alice_volume()

    report_volume(controls, 0.6)
    await controls.get_volume()

    assert await controls.fade_out_alice_volume() is None


@pytest.mark.parametrize(
    "volume, expected", [(0.0, 0.0), (None, 0), (0.7, 0.7)]
)
async def test_without_saved_level_volume_comes_from_station(volume, expected):
    """Сохранённого уровня нет: станция не считается заглушённой."""
    controls = make_reporting_controls(volume)

    await controls.set_default_volume()

    assert controls.working_volume == expected
    assert not await controls.unmute()
    assert sent_volumes(controls) == []


async def test_station_volume_replaces_saved_level(station_volume_file):
    """Станция сообщила уровень: рабочим становится он, а не сохранённый."""
    station_volume_file.write_text("0.6")
    controls = make_reporting_controls(0.4)

    await controls.set_default_volume()

    assert controls.working_volume == 0.4
    assert StationVolumeStore().load() == 0.4
    assert not await controls.unmute()
