from unittest.mock import MagicMock

import pytest

from yandex_station.station_ws_control import YandexStationClient


@pytest.fixture
def mock_finder():
    """Мок DeviceFinder с найденной станцией."""
    finder = MagicMock()
    finder.device = {
        "device_id": "dummy_id",
        "platform": "dummy_platform",
        "host": "localhost",
        "port": 1234,
    }
    finder.find_devices.return_value = True
    return finder


@pytest.fixture
def mock_station_client(mock_finder):
    """Клиент станции с замоканным DeviceFinder и пустым кешем."""
    client = YandexStationClient(device_finder=mock_finder)
    client._device_store = MagicMock()
    client._device_store.load.return_value = None
    return client
