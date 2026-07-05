import asyncio
import json
from logging import getLogger
from typing import Any

from injector import inject

from yandex_station.constants import ALICE_ACTIVE_STATES, FADE_TIME
from yandex_station.models import Track
from yandex_station.protobuf_parser import Protobuf
from yandex_station.station_ws_control import YandexStationClient

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
        self._was_muted = False

    async def start_ws_client(self):
        """Запуск WebSocket-клиента."""
        logger.info("🔄 Запуск WebSocket-клиента")
        await self._ws_client.run_once()

    async def stop_ws_client(self):
        if not self._ws_client.running:
            logger.info("⚠️ WebSocket-клиент уже полностью остановлен")
            return

        logger.info("🔄 Остановка WebSocket-клиента")
        await self._ws_client.close()

    async def play(self):
        """Запуск воспроизведения."""
        await self._ws_client.send_command({"command": "play"})

    async def stop(self):
        """Остановка воспроизведения."""
        await self._ws_client.send_command({"command": "stop"})

    async def send_text(self, text: str):
        """Отправка текстового сообщения."""
        logger.info(f"🔊 Отправка текстового сообщения: {text}")
        try:
            await self._ws_client.send_command(
                {"command": "sendText", "text": text}
            )
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке текстового сообщения: {e}")

    async def get_current_state(self) -> dict[str, Any] | None:
        """Получение текущего состояния станции."""
        try:
            state = await self._ws_client.get_latest_message()
            if state:
                return dict(state.get("state", {}))
            return None
        except Exception as e:
            logger.error(
                f"❌ Ошибка при получении текущего состояния станции: {e}"
            )
            return None

    async def get_radio_url(self) -> str | None:
        """Получение URL радиостанции."""
        try:
            data = await self._ws_client.get_latest_message()
            if not data:
                return None
            # Навигация по reverse-engineered структуре protobuf —
            # технический шов, наружу уходит только str | None
            state: Any = self._protobuf.loads(data["extra"]["appState"])
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

    async def get_alice_state(self) -> str | None:
        """Получение состояния Алисы (IDLE, LISTENING, SPEAKING...)."""
        try:
            state = await self._ws_client.get_latest_message()
            if not state:
                return None
            alice_state = state.get("state", {}).get("aliceState")
            return str(alice_state) if alice_state else None
        except Exception as e:
            logger.error(f"❌ Ошибка при получении состояния Алиса: {e}")
            return None

    async def get_player_status(self) -> dict[str, Any] | None:
        """Получение состояния плеера с флагом playing."""
        try:
            state = await self.get_current_state()
            if state is None:
                return None
            player_state = dict(state.get("playerState", {}))
            player_state["playing"] = state.get("playing", False)
            return player_state
        except Exception as e:
            logger.error(f"❌ Ошибка при получении статуса плеера: {e}")
            return None

    async def get_current_track(self) -> Track | None:
        """Получение текущего трека."""
        try:
            player_state = await self.get_player_status()
            if not player_state:
                return None
            entity = player_state.get("entityInfo") or {}
            return Track(
                id=str(player_state.get("id", "0")),
                title=str(player_state.get("title", "")),
                type=str(player_state.get("type", "")),
                artist=str(player_state.get("subtitle", "")),
                duration=float(player_state.get("duration") or 0),
                progress=float(player_state.get("progress") or 0),
                playing=bool(player_state.get("playing", False)),
                next_id=self._neighbor_track_id(entity, "next"),
                prev_id=self._neighbor_track_id(entity, "prev"),
            )
        except Exception as e:
            logger.error(f"❌ Ошибка при получении текущего трека: {e}")
            return None

    @staticmethod
    def _neighbor_track_id(entity: Any, key: str) -> str | None:
        """Извлекает id соседнего трека из entityInfo станции.

        Args:
            entity (Any): Словарь entityInfo из playerState.
            key (str): Ключ соседа: next или prev.
        Returns:
            str | None: Идентификатор, если сосед — трек.
        """
        if not isinstance(entity, dict):
            return None
        neighbor = entity.get(key)
        if not isinstance(neighbor, dict) or neighbor.get("type") != "Track":
            return None
        neighbor_id = neighbor.get("id")
        return str(neighbor_id) if neighbor_id else None

    async def get_volume(self) -> float | None:
        """Получение текущего уровня громкости (0.0–1.0)."""
        try:
            state = await self._ws_client.get_latest_message()
            if not state:
                return None
            volume = state.get("state", {}).get("volume")
            if volume is None:
                return None
            logger.debug(
                f"🔊 Получение текущего уровня громкости Алиcы: {volume}"
            )
            return float(volume)
        except Exception as e:
            logger.error(f"❌ Ошибка при получении громкости: {e}")
            return None

    async def set_default_volume(self):
        """Установка громкости по умолчанию."""
        logger.info("🔊 Установка громкости по умолчанию")
        volume = await self.get_volume()
        if volume is not None:
            self._volume = volume
        logger.info(f"Громкость по умолчанию: {self._volume}")

    async def set_volume(self, volume: float):
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
                self._was_muted = False
        except Exception as e:
            logger.error(f"❌ Ошибка при установке громкости: {e}")

    async def mute(self):
        """Безопасное выключение звука — только если Алиса молчит."""
        if self._was_muted:
            return

        state = await self.get_alice_state()

        if state not in ALICE_ACTIVE_STATES:
            volume = await self.get_volume()
            if volume is not None:
                self._volume = volume
            await self._ws_client.send_command(
                {"command": "setVolume", "volume": 0}
            )
            self._was_muted = True
            logger.info("🔇 Станция замьючена безопасно")

    async def unmute(self):
        if not self._was_muted:
            return
        logger.info("🔊 Включение громкости")
        try:
            await self._ws_client.send_command(
                {
                    "command": "setVolume",
                    "volume": self._volume,
                }
            )
            self._was_muted = False
        except Exception as e:
            logger.error(f"❌ Ошибка при включении громкости: {e}")

    async def fade_out_station(self):
        """Плавное отключение звука станции с задержкой."""
        if self._was_muted:
            return
        logger.info(f"🎧 Ждём {FADE_TIME}s перед mute станции")
        await asyncio.sleep(FADE_TIME)
        await self.mute()

    async def fade_out_alice_volume(
        self, min_volume: float = 0.0, step: float = 0.1, delay: float = 0.3
    ):
        """Плавное уменьшение громкости Алисы в несколько шагов."""
        if self._was_muted:
            return
        logger.info(f"🎧 Ждём {FADE_TIME}s перед fade out громкости")
        await asyncio.sleep(FADE_TIME)
        start_volume = await self.get_volume()
        if start_volume is None:
            logger.warning(
                "⚠️ Громкость станции неизвестна, fade out пропущен"
            )
            return
        self._volume = start_volume
        volume = round(start_volume - (start_volume % step), 1)

        logger.info(
            f"🔉 Плавное снижение громкости Алисы: "
            f"{volume:.1f} ➝ {min_volume:.1f} шагом {step}"
        )

        try:
            v = volume
            while v > min_volume:
                await self.set_volume(round(v, 1))
                logger.info(f"  ➤ Устанавливаем громкость: {v:.1f}")
                v -= step
                await asyncio.sleep(delay)

            await self.set_volume(round(min_volume, 1))
            self._was_muted = True
            logger.info("✅ Плавное снижение громкости Алисы завершено")
        except Exception as e:
            logger.error(f"❌ Ошибка при снижении громкости Алисы: {e}")
