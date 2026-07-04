class RuarkDeviceNotFoundError(Exception):
    """Исключение, если устройство Ruark не найдено в сети."""

    pass


class RuarkApiError(Exception):
    """Исключение при ошибке HTTP API (fsapi) устройства Ruark."""

    pass
