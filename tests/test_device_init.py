from unittest.mock import MagicMock

import pytest

from ruark_audio_system.exceptions import RuarkDeviceNotFoundError
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from yandex_station.exceptions import StationNotFoundError
from yandex_station.station_ws_control import YandexStationClient


def test_station_client_init_does_not_search_network():
    finder = MagicMock()
    client = YandexStationClient(device_finder=finder)

    finder.find_devices.assert_not_called()
    assert client.device_id is None
    assert client.uri is None


async def test_ensure_device_fills_station_params(mock_station_client):
    await mock_station_client._ensure_device()

    assert mock_station_client.device_id == "dummy_id"
    assert mock_station_client.platform == "dummy_platform"
    assert mock_station_client.uri == "wss://localhost:1234"


async def test_ensure_device_searches_only_once(mock_station_client):
    await mock_station_client._ensure_device()
    await mock_station_client._ensure_device()

    mock_station_client.device_finder.find_devices.assert_called_once()


async def test_ensure_device_raises_when_station_not_found():
    finder = MagicMock()
    finder.device = {}
    finder.find_devices.return_value = False
    client = YandexStationClient(device_finder=finder)

    with pytest.raises(StationNotFoundError):
        await client._ensure_device()


def test_ruark_init_does_not_search_network():
    controller = RuarkR5Controller()

    assert controller.device is None
    assert controller.services == {}


def test_ruark_service_property_raises_until_connected():
    controller = RuarkR5Controller()

    with pytest.raises(RuarkDeviceNotFoundError):
        controller.av_transport


async def test_ruark_connect_retries_until_device_found():
    controller = RuarkR5Controller()
    fake_device = MagicMock()
    calls = {"count": 0}

    def refresh_side_effect():
        calls["count"] += 1
        if calls["count"] >= 2:
            controller.device = fake_device

    controller.refresh_device = MagicMock(side_effect=refresh_side_effect)
    controller.print_available_services = MagicMock()

    assert await controller.connect(attempts=3, delay=0) is True
    assert controller.refresh_device.call_count == 2


async def test_ruark_connect_returns_false_after_all_attempts():
    controller = RuarkR5Controller()
    controller.refresh_device = MagicMock()

    assert await controller.connect(attempts=2, delay=0) is False
    assert controller.refresh_device.call_count == 2


async def test_ruark_connect_skips_search_when_already_connected():
    controller = RuarkR5Controller()
    controller.device = MagicMock()
    controller.refresh_device = MagicMock()

    assert await controller.connect() is True
    controller.refresh_device.assert_not_called()
