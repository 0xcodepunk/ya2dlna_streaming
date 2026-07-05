from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config.settings import settings
from ruark_audio_system.exceptions import RuarkDeviceNotFoundError
from ruark_audio_system.location_store import RuarkLocationStore
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from yandex_station.exceptions import StationNotFoundError
from yandex_station.station_store import StationDeviceStore
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
    client._device_store = MagicMock()
    client._device_store.load.return_value = None

    with pytest.raises(StationNotFoundError):
        await client._ensure_device()


async def test_ensure_device_uses_reachable_cached_station(
    mock_station_client,
):
    """Кеш с доступной станцией избавляет от mDNS-поиска."""
    mock_station_client._device_store.load.return_value = {
        "device_id": "cached_id",
        "platform": "cached_platform",
        "host": "10.0.0.3",
        "port": 1961,
    }
    mock_station_client._is_station_reachable = AsyncMock(return_value=True)

    await mock_station_client._ensure_device()

    assert mock_station_client.device_id == "cached_id"
    assert mock_station_client.uri == "wss://10.0.0.3:1961"
    mock_station_client.device_finder.find_devices.assert_not_called()


async def test_ensure_device_ignores_unreachable_cache(mock_station_client):
    """Протухший кеш станции — фолбэк на mDNS с сохранением результата."""
    mock_station_client._device_store.load.return_value = {
        "device_id": "cached_id",
        "platform": "cached_platform",
        "host": "10.0.0.3",
        "port": 1961,
    }
    mock_station_client._is_station_reachable = AsyncMock(return_value=False)

    await mock_station_client._ensure_device()

    assert mock_station_client.device_id == "dummy_id"
    mock_station_client.device_finder.find_devices.assert_called_once()
    mock_station_client._device_store.save.assert_called_once_with(
        mock_station_client.device_finder.device
    )


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


def make_fake_device(name="Ruark R5", location="http://10.0.0.5:8080/dd.xml"):
    """Фейковое UPnP-устройство для быстрого пути подключения."""
    device = MagicMock()
    device.friendly_name = name
    device.location = location
    device.services = []
    return device


def make_fast_path_controller(monkeypatch, cached_location=None):
    """Контроллер с изолированным кешем и без APP_RUARK_HOST."""
    monkeypatch.setattr(settings, "ruark_host", None)
    controller = RuarkR5Controller()
    controller._location_store = MagicMock()
    controller._location_store.load.return_value = cached_location
    return controller


def test_refresh_uses_cached_location_without_scan(monkeypatch):
    """Кешированный адрес избавляет от SSDP-скана всей сети."""
    fake_device = make_fake_device()
    controller = make_fast_path_controller(
        monkeypatch, cached_location=fake_device.location
    )
    monkeypatch.setattr(
        controller, "_is_location_reachable", lambda location: True
    )
    monkeypatch.setattr(
        "ruark_audio_system.ruark_r5_controller.upnpclient.Device",
        MagicMock(return_value=fake_device),
    )
    controller.find_device = MagicMock()

    controller.refresh_device()

    assert controller.device is fake_device
    controller.find_device.assert_not_called()
    controller._location_store.save.assert_called_once_with(
        fake_device.location
    )


def test_refresh_falls_back_to_scan_when_cache_stale(monkeypatch):
    """Недоступный кешированный адрес — фолбэк на полный поиск."""
    fake_device = make_fake_device(location="http://10.0.0.7:8080/dd.xml")
    controller = make_fast_path_controller(
        monkeypatch, cached_location="http://10.0.0.5:8080/dd.xml"
    )
    monkeypatch.setattr(
        controller, "_is_location_reachable", lambda location: False
    )
    controller.find_device = MagicMock(return_value=fake_device)

    controller.refresh_device()

    assert controller.device is fake_device
    controller.find_device.assert_called_once()
    controller._location_store.save.assert_called_once_with(
        fake_device.location
    )


def test_refresh_rejects_wrong_device_at_cached_location(monkeypatch):
    """Чужое устройство по кешированному адресу не принимается."""
    stranger = make_fake_device(name="Serviio")
    ruark = make_fake_device(location="http://10.0.0.7:8080/dd.xml")
    controller = make_fast_path_controller(
        monkeypatch, cached_location=stranger.location
    )
    monkeypatch.setattr(
        controller, "_is_location_reachable", lambda location: True
    )
    monkeypatch.setattr(
        "ruark_audio_system.ruark_r5_controller.upnpclient.Device",
        MagicMock(return_value=stranger),
    )
    controller.find_device = MagicMock(return_value=ruark)

    controller.refresh_device()

    assert controller.device is ruark
    controller.find_device.assert_called_once()


def test_env_host_connects_via_unicast_msearch(monkeypatch):
    """IP из настроек резолвится юникастовым M-SEARCH без скана."""
    fake_device = make_fake_device(location="http://10.0.0.9:8080/dd.xml")
    monkeypatch.setattr(settings, "ruark_host", "10.0.0.9")
    controller = RuarkR5Controller()
    controller._location_store = MagicMock()
    controller._location_store.load.return_value = None
    locate_mock = MagicMock(return_value=fake_device.location)
    monkeypatch.setattr(
        "ruark_audio_system.ruark_r5_controller.ssdp_locate", locate_mock
    )
    monkeypatch.setattr(
        controller, "_is_location_reachable", lambda location: True
    )
    monkeypatch.setattr(
        "ruark_audio_system.ruark_r5_controller.upnpclient.Device",
        MagicMock(return_value=fake_device),
    )
    controller.find_device = MagicMock()

    controller.refresh_device()

    assert controller.device is fake_device
    locate_mock.assert_called_once_with("10.0.0.9")
    controller.find_device.assert_not_called()


def test_location_store_roundtrip(tmp_path):
    store = RuarkLocationStore(path=tmp_path / "location.txt")

    assert store.load() is None
    store.save("http://10.0.0.5:8080/dd.xml")
    assert store.load() == "http://10.0.0.5:8080/dd.xml"


def test_station_store_roundtrip(tmp_path):
    store = StationDeviceStore(path=tmp_path / "station.json")
    device = {
        "device_id": "id1",
        "platform": "platform1",
        "host": "10.0.0.3",
        "port": 1961,
    }

    assert store.load() is None
    store.save(device)
    assert store.load() == device


def test_station_store_ignores_corrupted_file(tmp_path):
    path = tmp_path / "station.json"
    path.write_text("не json")
    store = StationDeviceStore(path=path)

    assert store.load() is None
