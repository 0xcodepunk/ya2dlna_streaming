from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

DEFAULT_STORE_PATH = Path("cache") / "ruark_location.txt"


class RuarkLocationStore:
    """Хранит location-URL Ruark между запусками для быстрого старта."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_STORE_PATH

    def load(self) -> str | None:
        """Возвращает сохранённый location-URL или None, если его нет."""
        try:
            location = self._path.read_text().strip()
            return location or None
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning(
                f"⚠️ Не удалось прочитать сохранённый адрес Ruark: {e}"
            )
            return None

    def save(self, location: str) -> None:
        """Сохраняет location-URL для следующего запуска."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(location)
            logger.debug(f"💾 Адрес Ruark сохранён: {location}")
        except OSError as e:
            logger.warning(f"⚠️ Не удалось сохранить адрес Ruark: {e}")
