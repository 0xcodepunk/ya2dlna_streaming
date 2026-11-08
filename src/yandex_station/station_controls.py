import asyncio
import json
from logging import getLogger
from typing import Any

from injector import inject

from core.enums import AliceState
from yandex_station.constants import (
    ALICE_ACTIVE_STATES,
    FADE_TIME,
    STATION_VOLUME_STEP,
    TTS_REPEAT_PREFIX,
)
from yandex_station.models import StationState, Track
from yandex_station.protobuf_parser import Protobuf
from yandex_station.station_ws_control import YandexStationClient
from yandex_station.volume_store import StationVolumeStore

logger = getLogger(__name__)


class YandexStationControls:
    """Класс управления станцией через WebSocket."""

    _ws_client: YandexStationClient
    _protobuf: Protobuf
    _volume: float

    @inject
    def __init__(
        self,
        ws_client: YandexStationClient,
        protobuf: Protobuf,
    ):
        self._ws_client = ws_client
        self._protobuf = protobuf
        self._volume = 0
        self._volume_store = StationVolumeStore()
        self._was_muted = False
        # Заглушка не наша, а приписана прошлому сеансу по нулю или молчанию
        # станции на старте: тот ноль мог быть устаревшим
        self._mute_assumed = False
        # Уровень отправлен unmute, но станция ещё сообщает прежнюю громкость
        self._level_unconfirmed = False

    async def start_ws_client(self) -> None:
        """Запуск WebSocket-клиента."""
        logger.info("🔄 Запуск WebSocket-клиента")
        await self._ws_client.run_once()

    async def stop_ws_client(self) -> None:
        """Остановка WebSocket-клиента."""
        if not self._ws_client.running:
            logger.info("⚠️ WebSocket-клиент уже полностью остановлен")
            return

        logger.info("🔄 Остановка WebSocket-клиента")
        await self._ws_client.close()

    async def play(self) -> None:
        """Запуск воспроизведения."""
        await self._ws_client.send_command({"command": "play"})

    async def stop(self) -> None:
        """Остановка воспроизведения."""
        await self._ws_client.send_command({"command": "stop"})

    async def send_text(self, text: str) -> None:
        """Отправка текстового сообщения."""
        logger.info(f"🔊 Отправка текстового сообщения: {text}")
        try:
            await self._ws_client.send_command(
                {"command": "sendText", "text": text}
            )
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке текстового сообщения: {e}")

    async def say(self, phrase: str) -> None:
        """Произносит фразу голосом Алисы через локальный TTS."""
        await self.send_text(f"{TTS_REPEAT_PREFIX} {phrase}")

    async def get_current_state(self) -> StationState | None:
        """Получение текущего состояния станции."""
        try:
            message = await self._ws_client.get_latest_message()
            return message.state if message is not None else None
        except Exception as e:
            logger.error(
                f"❌ Ошибка при получении текущего состояния станции: {e}"
            )
            return None

    async def get_radio_url(self) -> str | None:
        """Получение URL радиостанции."""
        try:
            message = await self._ws_client.get_latest_message()
            if message is None or message.extra.app_state is None:
                return None
            # Навигация по reverse-engineered структуре protobuf —
            # технический шов, наружу уходит только str | None
            state: Any = self._protobuf.loads(message.extra.app_state)
            metaw = json.loads(state[6][3][7])
            item: Any = self._protobuf.loads(
                metaw["scenario_meta"]["queue_item"]
            )
            return str(item[7][1].decode())

        except Exception as e:
            logger.error(
                f"❌ Ошибка при получении текущего состояния станции через "
                f"Protobuf: {e}"
            )
            return None

    async def get_alice_state(self) -> AliceState | None:
        """Получение состояния Алисы (IDLE, LISTENING, SPEAKING...)."""
        state = await self.get_current_state()
        return state.alice_state if state is not None else None

    async def get_current_track(self) -> Track | None:
        """Получение текущего трека, None — сообщений станции ещё нет."""
        state = await self.get_current_state()
        # Сообщение без плеера даёт трек по умолчанию с id "0", и цикл
        # работает дальше, а не пропускает виток
        return state.to_track() if state is not None else None

    async def get_volume(self) -> float | None:
        """Получение текущего уровня громкости (0.0–1.0)."""
        try:
            state = await self.get_current_state()
            level = state.volume if state is not None else None
            if level is None:
                return None
            logger.debug(
                f"🔊 Получение текущего уровня громкости Алиcы: {level}"
            )
            if level > 0:
                self._level_unconfirmed = False
                if self._mute_assumed:
                    logger.info(f"🔊 Станция на {level}, она не заглушена")
                    self._forget_mute()
            return level
        except Exception as e:
            logger.error(f"❌ Ошибка при получении громкости: {e}")
            return None

    @property
    def working_volume(self) -> float:
        """Рабочий уровень громкости, который возвращает unmute()."""
        return self._volume

    @working_volume.setter
    def working_volume(self, volume: float) -> None:
        """Задаёт рабочий уровень и сохраняет его для следующего процесса."""
        # Побывав на нуле, станция сообщает шаг над ним: это не выбор
        # слушателя, и прежний уровень он затирать не должен
        if volume > 0 and self._is_mute_trace(volume, self._volume):
            logger.info(
                f"🔇 Уровень {volume} не принят, рабочий {self._volume}"
            )
            return
        # Ноль в файле стёр бы настоящий уровень
        if volume > 0 and volume != self._volume:
            self._volume_store.save(volume)
        self._volume = volume

    @staticmethod
    def _is_mute_trace(volume: float, level: float) -> bool:
        """Проверяет, что громкость — след заглушения, а не выбор слушателя.

        Args:
            volume (float): Прочитанная громкость станции.
            level (float): Известный рабочий уровень.
        Returns:
            bool: True, если громкость не выше шага, а уровень выше него.
        """
        return volume <= STATION_VOLUME_STEP < level

    @property
    def level_unconfirmed(self) -> bool:
        """Уровень отправлен unmute, но станция ещё его не сообщила."""
        return self._level_unconfirmed

    async def set_default_volume(self) -> None:
        """Установка громкости по умолчанию."""
        logger.info("🔊 Установка громкости по умолчанию")
        # Громкость заглушённой стримом станции — след гашения, а не уровень
        if self._was_muted:
            logger.info(f"Станция заглушена, рабочий уровень: {self._volume}")
            return
        volume = await self.get_volume()
        saved = self._volume_store.load()
        if saved is not None and (
            not volume or self._is_mute_trace(volume, saved)
        ):
            self._volume = saved
            self._was_muted = True
            self._mute_assumed = True
            logger.info(
                f"🔇 Громкость станции на старте: {volume}, считаем её "
                f"заглушённой прошлым сеансом, рабочий уровень {saved}"
            )
        elif volume is not None:
            self.working_volume = volume
        logger.info(f"Громкость по умолчанию: {self._volume}")

    def _forget_mute(self) -> None:
        """Сбрасывает признак заглушки вместе с предположением о ней."""
        self._was_muted = False
        # Станция сообщает отправленный ей уровень с задержкой: оставшееся
        # предположение сняло бы по этому чтению настоящую заглушку гашения
        self._mute_assumed = False

    async def set_volume(self, volume: float) -> None:
        """Установка уровня громкости."""
        logger.info(f"🔊 Установка громкости на {volume}")
        try:
            await self._ws_client.send_command(
                {
                    "command": "setVolume",
                    "volume": volume,
                }
            )
            if volume > 0:
                self._forget_mute()
        except Exception as e:
            logger.error(f"❌ Ошибка при установке громкости: {e}")

    async def mute(self) -> None:
        """Безопасное выключение звука — только если Алиса молчит."""
        if self._was_muted:
            return

        state = await self.get_alice_state()

        if state not in ALICE_ACTIVE_STATES:
            volume = await self.get_volume()
            if volume is not None:
                self.working_volume = volume
            await self._ws_client.send_command(
                {"command": "setVolume", "volume": 0}
            )
            self._was_muted = True
            logger.info("🔇 Станция замьючена безопасно")

    async def unmute(self) -> bool:
        """Возвращает станции рабочий уровень громкости.

        Returns:
            bool: True, если уровень отправлен, а станция ещё не сообщила
                его: прочитанная громкость может быть прежней.
        """
        if not self._was_muted:
            return self._level_unconfirmed
        logger.info("🔊 Включение громкости")
        try:
            await self._ws_client.send_command(
                {
                    "command": "setVolume",
                    "volume": self._volume,
                }
            )
            self._forget_mute()
            self._level_unconfirmed = True
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка при включении громкости: {e}")
            return False

    async def is_alice_active(self) -> bool:
        """Проверяет, слушает или говорит ли Алиса."""
        return await self.get_alice_state() in ALICE_ACTIVE_STATES

    async def fade_out_station(self) -> None:
        """Плавное отключение звука станции с задержкой."""
        if self._was_muted:
            return
        logger.info(f"🎧 Ждём {FADE_TIME}s перед mute станции")
        await asyncio.sleep(FADE_TIME)
        await self.mute()

    async def fade_out_alice_volume(
        self,
        min_volume: float = 0.0,
        step: float = STATION_VOLUME_STEP,
        delay: float = 0.3,
    ) -> float | None:
        """Плавное уменьшение громкости Алисы в несколько шагов.

        Если Алиса снова слушает или говорит, оставшиеся ступени
        пропускаются: станция уходит в ноль, а рабочий уровень ей вернёт
        unmute новой реплики.

        Returns:
            float | None: Громкость перед гашением или None без гашения.
        """
        if self._was_muted:
            return None
        logger.info(f"🎧 Ждём {FADE_TIME}s перед fade out громкости")
        await asyncio.sleep(FADE_TIME)
        start_volume = await self.get_volume()
        if start_volume is None:
            logger.warning(
                "⚠️ Громкость станции неизвестна, fade out пропущен"
            )
            return None
        self.working_volume = start_volume
        volume = round(start_volume, 1)

        logger.info(
            f"🔉 Плавное снижение громкости Алисы: "
            f"{volume:.1f} ➝ {min_volume:.1f} шагом {step}"
        )

        try:
            v = volume
            # Новая реплика Алисы прерывает гашение: оставшиеся ступени
            # перезаписали бы громкость, которую меняет её команда
            while v > min_volume and not await self.is_alice_active():
                await self.set_volume(round(v, 1))
                logger.info(f"  ➤ Устанавливаем громкость: {v:.1f}")
                v = round(v - step, 1)
                await asyncio.sleep(delay)
            if v > min_volume:
                logger.info("🎙 Алиса снова активна — гашение прервано")

            await self.set_volume(round(min_volume, 1))
            self._was_muted = True
            logger.info("✅ Плавное снижение громкости Алисы завершено")
        except Exception as e:
            logger.error(f"❌ Ошибка при снижении громкости Алисы: {e}")
        return start_volume
