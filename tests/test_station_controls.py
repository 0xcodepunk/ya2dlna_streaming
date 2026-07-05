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
