import logging

import pytest

from core.config.settings import settings
from core.logging.setup import setup_logging
from ruark_audio_system import ruark_r5_controller


@pytest.fixture
def restore_logging():
    """Возвращает корневой логгер и флаги disabled после dictConfig."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    disabled = {
        name: logger.disabled
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, was_disabled in disabled.items():
        logging.getLogger(name).disabled = was_disabled


def test_setup_logging_keeps_existing_loggers_at_root_level(
    monkeypatch, tmp_path, restore_logging
):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr("core.logging.setup.LOG_DIR", str(tmp_path))
    early_logger = logging.getLogger("tests.created_before_setup_logging")

    setup_logging("test")

    for logger in (early_logger, ruark_r5_controller.logger):
        assert not logger.disabled
        assert logger.getEffectiveLevel() == logging.DEBUG
