from unittest.mock import MagicMock

import pytest
import requests
from upnpclient.soap import SOAPError

from ruark_audio_system.exceptions import RuarkUnavailableError
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller


def make_action(name: str, error: Exception) -> MagicMock:
    """UPnP-команда, которая падает с указанной ошибкой."""
    action = MagicMock(side_effect=error)
    action.name = name
    return action


async def test_set_volume_timeout_becomes_unavailable():
    """Таймаут SetVolume — признак зависания, а не поломки цикла."""
    controller = RuarkR5Controller()
    controller._rendering_control = MagicMock(
        SetVolume=make_action(
            "SetVolume", requests.exceptions.ReadTimeout("Read timed out")
        )
    )

    with pytest.raises(RuarkUnavailableError, match="SetVolume.*Read timed"):
        await controller.set_volume(20)


async def test_get_volume_refused_connection_becomes_unavailable():
    """Отказ в соединении — Ruark перезагружается."""
    controller = RuarkR5Controller()
    controller._rendering_control = MagicMock(
        GetVolume=make_action(
            "GetVolume", requests.exceptions.ConnectionError("refused")
        )
    )

    with pytest.raises(RuarkUnavailableError, match="GetVolume"):
        await controller.get_volume()


async def test_stop_timeout_becomes_unavailable():
    """Stop зависшего Ruark не роняет цикл на паузе станции."""
    controller = RuarkR5Controller()
    controller._av_transport = MagicMock(
        GetTransportInfo=MagicMock(
            return_value={"CurrentTransportState": "PLAYING"}
        ),
        Stop=make_action(
            "Stop", requests.exceptions.ReadTimeout("Read timed out")
        ),
    )

    with pytest.raises(RuarkUnavailableError, match="Stop"):
        await controller.stop()


async def test_soap_error_is_not_treated_as_outage():
    """Отказ самого устройства остаётся ошибкой: Ruark ответил."""
    controller = RuarkR5Controller()
    controller._rendering_control = MagicMock(
        SetVolume=make_action("SetVolume", SOAPError(402, "Invalid Args"))
    )

    with pytest.raises(SOAPError):
        await controller.set_volume(20)
