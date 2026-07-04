import asyncio
import json
from logging import getLogger

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

    async def get_current_state(self):
        """Получение текущего состояния станции."""
        try:
            state = await self._ws_client.get_latest_message()
            # logger.info(f"🎵 Состояние станции: {state}")
            if state:
                return state.get("state", {})
            else:
                return None
        except Exception as e:
            logger.error(
                f"❌ Ошибка при получении текущего состояния станции: {e}"
            )

    async def get_radio_url(self):
        """Получение URL радиостанции."""
        try:
            data = await self._ws_client.get_latest_message()
            state = self._protobuf.loads(data["extra"]["appState"])
            metaw = json.loads(state[6][3][7])
            item = self._protobuf.loads(metaw["scenario_meta"]["queue_item"])
            url = item[7][1].decode()
            return url

        except Exception as e:
            logger.error(
                f"❌ Ошибка при получении текущего состояния станции через "
                f"Protobuf: {e}"
            )
            return None

    async def get_alice_state(self):
        """Получение состояния Алиса."""
        try:
            state = await self._ws_client.get_latest_message()
            if state:
                return state.get("state", {}).get("aliceState", {})
        except Exception as e:
            logger.error(f"❌ Ошибка при получении состояния Алиса: {e}")
            return None

    async def get_player_status(self) -> bool:
        """Получение статуса плеера."""
        try:
            state = await self.get_current_state()
            play_status = state.get("playing", {})
            player_state = state.get("playerState", {})
            player_state["playing"] = play_status
            return player_state
        except Exception as e:
            logger.error(f"❌ Ошибка при получении статуса плеера: {e}")
            return False

    async def get_current_track(self) -> Track | None:
        """Получение текущего трека."""
        try:
            player_state = await self.get_player_status()
            # logger.info(f"🎵 Состояние плеера: {player_state}") # TODO: remove
            if player_state:
                return Track(
                    id=player_state.get("id", 0),
                    title=player_state.get("title", ""),
                    type=player_state.get("type", ""),
                    artist=player_state.get("subtitle", ""),
                    duration=player_state.get("duration", 0),
                    progress=player_state.get("progress", 0),
                    playing=player_state.get("playing", False),
                )
            else:
                return None
        except Exception as e:
            logger.error(f"❌ Ошибка при получении текущего трека: {e}")

    async def get_volume(self):
        """Получение текущего уровня громкости."""
        try:
            state = await self._ws_client.get_latest_message()
            if state:
                logger.info(
                    f"🔊 Получение текущего уровня громкости Алиcы: "
                    f"{state.get('state', {}).get('volume', {})}"
                )
                return state.get("state", {}).get("volume", {})
        except Exception as e:
            logger.error(f"❌ Ошибка при получении громкости: {e}")
            return None

    async def set_default_volume(self):
        """Установка громкости по умолчанию."""
        logger.info("🔊 Установка громкости по умолчанию")
        try:
            self._volume = await self.get_volume()
            logger.info(f"Громкость по умолчанию: {self._volume}")
        except Exception as e:
            logger.error(
                f"❌ Ошибка при установке громкости по умолчанию: {e}"
            )

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
            self._volume = await self.get_volume()
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
        self._volume = await self.get_volume()
        start_volume = self._volume
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
