class ClientNotRunningError(Exception):
    """Исключение, если попытка отправить команду при остановленном клиенте."""
    pass
