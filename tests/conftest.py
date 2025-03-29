from unittest.mock import MagicMock

import pytest

from yandex_station.station_ws_control import YandexStationClient


@pytest.fixture
def mock_station_client():
    mock_finder = MagicMock()
    mock_finder.find_devices.side_effect = lambda: setattr(
        mock_finder,
        "device",
        {
            "device_id": "dummy_id",
            "platform": "dummy_platform",
            "host": "localhost",
            "port": 1234,
        },
    )
    client = YandexStationClient(device_finder=mock_finder)
    return client
