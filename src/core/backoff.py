class ExponentialBackoff:
    """Пауза между повторами, удваивающаяся от стартовой до потолка.

    Args:
        start (float): Первая пауза и пауза после сброса, секунды.
        ceiling (float): Максимальная пауза, секунды.
    Raises:
        ValueError: Если start не положительна или больше ceiling.
    """

    def __init__(self, start: float, ceiling: float) -> None:
        # Нулевая пауза превратила бы повторы в цикл без передышки
        if not 0 < start <= ceiling:
            raise ValueError(
                f"Нужно 0 < start <= ceiling, получено "
                f"start={start}, ceiling={ceiling}"
            )
        self.start = start
        self.ceiling = ceiling
        self._next = start

    def next_delay(self) -> float:
        """Возвращает очередную паузу и удваивает следующую до потолка."""
        delay = self._next
        self._next = min(delay * 2, self.ceiling)
        return delay

    def reset(self) -> None:
        """Возвращает стартовую паузу после успешной попытки."""
        self._next = self.start
