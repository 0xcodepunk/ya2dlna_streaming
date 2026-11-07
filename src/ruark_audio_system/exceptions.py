class RuarkDeviceNotFoundError(Exception):
    """Исключение, если устройство Ruark не найдено в сети."""

    pass


class RuarkApiError(Exception):
    """Исключение при ошибке HTTP API (fsapi) устройства Ruark."""

    pass


class RuarkUnavailableError(Exception):
    """Исключение, если Ruark завис или недоступен для UPnP-команды."""

    pass
