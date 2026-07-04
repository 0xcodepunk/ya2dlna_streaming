class ClientNotRunningError(Exception):
    """Исключение, если попытка отправить команду при остановленном клиенте."""

    pass


class StationNotFoundError(Exception):
    """Исключение, если Яндекс Станция не найдена в сети."""

    pass
