class ClientNotRunningError(Exception):
    """Исключение, если попытка отправить команду при остановленном клиенте."""

    pass


class StationNotFoundError(Exception):
    """Исключение, если Яндекс Станция не найдена в сети."""

    pass


class StationAuthError(Exception):
    """Исключение, если станция не подтвердила авторизацию по токену."""

    pass
