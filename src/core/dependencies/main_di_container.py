from typing import Optional, Sequence

from injector import Injector, Module

from core.dependencies.di_modules import (
    DeviceFinderModule,
    MainStreamManagerModule,
    RuarkR5ControllerModule,
    StreamHandlerModule,
    YandexMusicAPIModule,
    YandexStationClientModule,
    YandexStationControlsModule,
)

ModuleSpec = type[Module] | Module


class MainDIContainer:
    """Контейнер со всеми зависимостями (Singleton)."""

    _instance: Optional["MainDIContainer"] = None
    _container: Injector | None = None  # DI-контейнер

    BASE_MODULES: list[ModuleSpec] = [
        YandexStationClientModule,
        YandexStationControlsModule,
        RuarkR5ControllerModule,
        YandexMusicAPIModule,
        MainStreamManagerModule,
        StreamHandlerModule,
        DeviceFinderModule,
    ]

    def __new__(
        cls, additional_modules: Sequence[ModuleSpec] | None = None
    ) -> "MainDIContainer":
        """Гарантирует, что контейнер создаётся один раз."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)

            # Объединяем базовые и дополнительные модули
            modules = list(cls.BASE_MODULES)
            if additional_modules:
                modules.extend(additional_modules)

            cls._instance._container = Injector(modules)

        return cls._instance

    def get_container(self) -> Injector:
        """Возвращает общий DI-контейнер.

        Raises:
            RuntimeError: Если контейнер не был создан.
        """
        if self._container is None:
            raise RuntimeError("DI-контейнер не инициализирован")
        return self._container
