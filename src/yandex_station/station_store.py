import json
from logging import getLogger
from pathlib import Path

from yandex_station.mdns_device_finder import StationDevice

logger = getLogger(__name__)

DEFAULT_STORE_PATH = Path("cache") / "station_device.json"


class StationDeviceStore:
    """Хранит параметры найденной станции между запусками.

    device_id и platform у станции постоянные, host почти всегда
    закреплён DHCP — кеш позволяет пропустить mDNS-поиск при старте.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_STORE_PATH

    def load(self) -> StationDevice | None:
        """Возвращает сохранённые параметры станции или None."""
        try:
            data = json.loads(self._path.read_text())
            return StationDevice(
                device_id=str(data["device_id"]),
                platform=str(data["platform"]),
                host=str(data["host"]),
                port=int(data["port"]),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning(
                f"⚠️ Не удалось прочитать сохранённые параметры станции: {e}"
            )
            return None

    def save(self, device: StationDevice) -> None:
        """Сохраняет параметры станции для следующего запуска."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(dict(device)))
            logger.debug(f"💾 Параметры станции сохранены: {device['host']}")
        except OSError as e:
            logger.warning(f"⚠️ Не удалось сохранить параметры станции: {e}")
