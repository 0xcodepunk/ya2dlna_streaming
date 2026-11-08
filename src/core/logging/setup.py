import logging
import os
from logging.config import dictConfig

from core.config.settings import settings

# Определяем путь к каталогу logs и создаем его, если он не существует
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging(service_name: str = "app") -> None:
    """Настройка логирования.

    Args:
        service_name (str): Имя лог-файла сервиса. У каждого процесса
            должен быть свой файл: два RotatingFileHandler на одном
            файле ломают ротацию и теряют записи.
    """
    logging_config = {
        "version": 1,
        # Логгеры модулей создаются при импорте, до этого вызова: без
        # False они отключаются и оживают только под uvicorn
        "disable_existing_loggers": False,
        "formatters": {
            # Трейсбеки Formatter добавляет сам при наличии exc_info,
            # явный %(exc_info)s печатал None в каждой строке
            "detailed": {
                "format": (
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                )
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "detailed",
                "level": "DEBUG" if settings.debug else "INFO",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": os.path.join(LOG_DIR, f"{service_name}.log"),
                "formatter": "detailed",
                "level": "INFO",
                "maxBytes": 5 * 1024 * 1024,
                "backupCount": 3,
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": "DEBUG" if settings.debug else "INFO",
        },
    }
    dictConfig(logging_config)


if __name__ == "__main__":
    setup_logging()
    logger = logging.getLogger(__name__)

    try:
        1 / 0
    except Exception as e:
        logger.error(f"Ошибка в коде! {e}", exc_info=True)
