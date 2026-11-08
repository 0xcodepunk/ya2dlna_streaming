import pytest

from core.enums import AliceState, TrackType
from yandex_station.models import StationMessage

PLAYER_STATE = {
    "id": "42",
    "title": "title",
    "type": "Track",
    "subtitle": "artist",
    "duration": 200,
    "progress": 10.5,
    "entityInfo": {
        "next": {"id": "43", "type": "Track"},
        "prev": {"id": "41", "type": "Track"},
    },
}


def test_station_message_parsed_from_camel_case_keys():
    message = StationMessage.model_validate(
        {
            "state": {
                "aliceState": "SPEAKING",
                "volume": 0.4,
                "playing": True,
                "playerState": PLAYER_STATE,
            },
            "extra": {"appState": "raw"},
        }
    )

    state = message.state
    player = state.player_state
    assert state.alice_state is AliceState.SPEAKING
    assert state.volume == 0.4
    assert state.playing is True
    assert player.id == "42"
    assert player.type is TrackType.TRACK
    assert player.subtitle == "artist"
    assert player.duration == 200
    assert player.progress == 10.5
    assert player.entity_info.next.track_id == "43"
    assert player.entity_info.prev.track_id == "41"
    assert message.extra.app_state == "raw"


def test_unknown_station_values_become_other():
    """Обновление прошивки не роняет разбор сообщения."""
    message = StationMessage.model_validate(
        {
            "state": {
                "aliceState": "NEW_FIRMWARE_STATE",
                "playerState": {**PLAYER_STATE, "type": "Generative"},
            }
        }
    )

    assert message.state.alice_state is AliceState.OTHER
    assert message.state.player_state.type is TrackType.OTHER


def test_numeric_track_id_becomes_string():
    message = StationMessage.model_validate(
        {"state": {"playerState": {**PLAYER_STATE, "id": 42}}}
    )

    assert message.state.player_state.id == "42"


def test_null_from_station_means_missing_field():
    message = StationMessage.model_validate(
        {
            "state": {
                "aliceState": None,
                "volume": None,
                "playing": None,
                "playerState": {
                    "id": None,
                    "title": None,
                    "duration": None,
                    "entityInfo": {"next": None},
                },
            },
            "extra": None,
        }
    )

    state = message.state
    player = state.player_state
    assert state.alice_state is None
    assert state.volume is None
    assert state.playing is False
    assert player.id == "0"
    assert player.title == ""
    assert player.duration == 0
    assert player.entity_info.next.track_id is None
    assert message.extra.app_state is None


def test_message_without_state_has_default_player():
    """Сообщение без плеера даёт трек по умолчанию, а не пустоту."""
    message = StationMessage.model_validate({"requestId": "abc"})

    player = message.state.player_state
    assert message.state.alice_state is None
    assert message.state.volume is None
    assert player.id == "0"
    assert player.type is TrackType.OTHER
    assert player.entity_info.next.track_id is None
    assert player.entity_info.prev.track_id is None


def test_bad_field_gets_default_and_rest_survives():
    """Одно плохое поле не бракует всё сообщение."""
    message, fallbacks = StationMessage.from_station(
        {
            "state": {
                "aliceState": "IDLE",
                "volume": "громко",
                "playerState": "строка вместо объекта",
            }
        }
    )

    assert message.state.alice_state is AliceState.IDLE
    assert message.state.volume is None
    assert message.state.player_state.id == "0"
    assert sorted(fallbacks) == [
        "StationState.player_state",
        "StationState.volume",
    ]


def test_good_message_has_no_fallbacks():
    _, fallbacks = StationMessage.from_station(
        {"state": {"playerState": PLAYER_STATE}}
    )

    assert fallbacks == {}


@pytest.mark.parametrize(
    "neighbor",
    [
        {"id": "43", "type": "FmRadio"},
        {"type": "Track"},
        {"id": "", "type": "Track"},
        "мусор",
    ],
)
def test_neighbor_is_track_id_only_for_tracks(neighbor):
    message = StationMessage.model_validate(
        {
            "state": {
                "playerState": {
                    **PLAYER_STATE,
                    "entityInfo": {"next": neighbor},
                }
            }
        }
    )

    assert message.state.player_state.entity_info.next.track_id is None
