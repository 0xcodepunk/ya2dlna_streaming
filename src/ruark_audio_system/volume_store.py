from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

DEFAULT_STORE_PATH = Path("cache") / "ruark_volume.txt"


class RuarkVolumeStore:
    """Хранит последнюю громкость Ruark между сеансами стриминга."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_STORE_PATH

    def load(self) -> int | None:
        """Возвращает сохранённую громкость или None, если её нет."""
        try:
            return int(self._path.read_text().strip())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning(
                f"⚠️ Не удалось прочитать сохранённую громкость Ruark: {e}"
            )
            return None

    def save(self, volume: int) -> None:
        """Сохраняет громкость для следующего сеанса."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(str(volume))
            logger.info(f"💾 Громкость Ruark сохранена: {volume}")
        except OSError as e:
            logger.warning(f"⚠️ Не удалось сохранить громкость Ruark: {e}")
