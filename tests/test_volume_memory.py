from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from main_stream_service.main_stream_manager import MainStreamManager
from main_stream_service.yandex_music_api import YandexMusicAPI
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from ruark_audio_system.volume_store import RuarkVolumeStore
from yandex_station.constants import RUARK_IDLE_VOLUME
from yandex_station.station_controls import YandexStationControls


def test_store_roundtrip(tmp_path: Path):
    store = RuarkVolumeStore(path=tmp_path / "volume.txt")

    assert store.load() is None
    store.save(27)
    assert store.load() == 27


def test_store_survives_corrupted_file(tmp_path: Path):
    path = tmp_path / "volume.txt"
    path.write_text("мусор")

    assert RuarkVolumeStore(path=path).load() is None


def make_manager(ruark_volume=20):
    ruark = AsyncMock(spec=RuarkR5Controller)
    ruark.get_volume.return_value = ruark_volume
    ruark.get_power_status.return_value = "1"
    ruark.get_session_id.return_value = "sid"
    station = AsyncMock(spec=YandexStationControls)
    ws_client = MagicMock()
    ws_client.wait_for_state_update = AsyncMock(return_value=True)
    manager = MainStreamManager(
        station_ws_client=ws_client,
        station_controls=station,
        ruark_controls=ruark,
        yandex_music_api=AsyncMock(spec=YandexMusicAPI),
    )
    manager._volume_store = MagicMock(spec=RuarkVolumeStore)
    manager._stop_stream_on_stream_server = AsyncMock()
    return manager, ruark


async def test_prepare_devices_restores_saved_volume():
    manager, ruark = make_manager()
    manager._volume_store.load.return_value = 33

    await manager._prepare_devices()

    ruark.set_volume.assert_awaited_with(33)
    assert manager._ruark_volume == 33


async def test_prepare_devices_reads_current_volume_without_saved():
    manager, ruark = make_manager(ruark_volume=18)
    manager._volume_store.load.return_value = None

    await manager._prepare_devices()

    ruark.set_volume.assert_not_awaited()
    assert manager._ruark_volume == 18


async def test_stop_saves_current_user_volume():
    manager, ruark = make_manager(ruark_volume=44)
    manager._ruark_volume = 20

    await manager.stop()

    manager._volume_store.save.assert_called_once_with(44)


async def test_stop_keeps_snapshot_when_ruark_is_ducked():
    # Ruark приглушён речью Алисы — запоминаем громкость до приглушения
    manager, ruark = make_manager(ruark_volume=RUARK_IDLE_VOLUME)
    manager._ruark_volume = 25

    await manager.stop()

    manager._volume_store.save.assert_called_once_with(25)


async def test_stop_saves_last_known_volume_when_ruark_unreachable():
    manager, ruark = make_manager()
    ruark.get_volume.side_effect = RuntimeError("нет связи")
    manager._ruark_volume = 30

    await manager.stop()

    manager._volume_store.save.assert_called_once_with(30)
