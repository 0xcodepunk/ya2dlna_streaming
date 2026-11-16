"""Журнал вызовов моков с отметками управляемого времени.

Общая запись «команда, пауза, команда» для тестов, фиксирующих
поведение устройств: что получает Ruark, в каком порядке и когда.
Время — список-часы, как у freeze_clock в test_streaming_loop; его
двигают только паузы asyncio.sleep.
"""

import asyncio

from main_stream_service.yandex_music_api import TrackSource

# Настоящий sleep сохраняется до подмены
REAL_SLEEP = asyncio.sleep


def shown(value) -> str:
    """Короткая запись аргумента или ответа для журнала."""
    if isinstance(value, TrackSource):
        return value.url
    if isinstance(value, float):
        return f"{value:g}"
    return repr(value)


def coroutine_methods(cls, skip=frozenset()) -> list[str]:
    """Публичные async-методы класса, кроме skip."""
    return [
        name
        for name in dir(cls)
        if not name.startswith("_")
        and name not in skip
        and asyncio.iscoroutinefunction(getattr(cls, name))
    ]


class Timeline:
    """Журнал «время от начала команда(аргументы) -> ответ».

    Ошибка вызова пишется как «!Класс». Ответы моков по умолчанию
    (сами моки) в журнал не идут.
    """

    def __init__(self, clock: list[float]) -> None:
        self._clock = clock
        self._start = clock[0]
        self.entries: list[str] = []

    def note(self, entry: str) -> int:
        """Добавляет запись с текущим временем, возвращает её номер."""
        self.entries.append(f"{self._clock[0] - self._start:6.1f} {entry}")
        return len(self.entries) - 1

    def spy(self, owner, labels: dict[str, str]) -> None:
        """Пишет вызовы методов owner, не меняя их поведения.

        Args:
            owner: Мок или объект с async-методами.
            labels: Имя метода → подпись в журнале.
        """
        for name, label in labels.items():
            setattr(
                owner, name, self._spied(getattr(owner, name), name, label)
            )

    def _spied(self, method, name: str, label: str):
        async def spy(*args, **kwargs):
            arguments = ", ".join(
                [shown(arg) for arg in args]
                + [f"{key}={shown(arg)}" for key, arg in kwargs.items()]
            )
            index = self.note(f"{label}({arguments})")
            try:
                result = await method(*args, **kwargs)
            except Exception as e:
                self.entries[index] += f" !{type(e).__name__}"
                raise
            if isinstance(result, (bool, int, float, str, TrackSource)):
                self.entries[index] += f" -> {shown(result)}"
            return result

        # Код пишет имя вызова в лог (_safe_stop_step, execute_with_lock)
        spy.__name__ = name
        return spy

    def patch_sleep(self, monkeypatch) -> None:
        """Паузы asyncio.sleep идут в журнал и двигают часы."""

        async def fake_sleep(delay, *args, **kwargs):
            self.note(f"sleep({shown(delay)})")
            self._clock[0] += delay
            await REAL_SLEEP(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
