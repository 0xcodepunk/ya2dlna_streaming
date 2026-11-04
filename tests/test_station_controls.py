import asyncio
from unittest.mock import AsyncMock, MagicMock

from yandex_station.protobuf_parser import Protobuf
from yandex_station.station_controls import YandexStationControls
from yandex_station.station_ws_control import YandexStationClient

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
