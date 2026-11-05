from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

DEFAULT_STORE_PATH = Path("cache") / "station_volume.txt"


class StationVolumeStore:
    """Хранит рабочий уровень станции между перезапусками процесса."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_STORE_PATH

    def load(self) -> float | None:
        """Возвращает сохранённый уровень или None, если его нет."""
        try:
            volume = float(self._path.read_text().strip())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning(
                f"⚠️ Не удалось прочитать рабочий уровень станции: {e}"
            )
            return None
        return volume if 0 < volume <= 1 else None

    def save(self, volume: float) -> None:
        """Сохраняет уровень для следующего запуска."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(str(volume))
            logger.info(f"💾 Рабочий уровень станции сохранён: {volume}")
        except OSError as e:
            logger.warning(
                f"⚠️ Не удалось сохранить рабочий уровень станции: {e}"
            )
