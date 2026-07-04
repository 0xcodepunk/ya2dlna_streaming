import asyncio
from logging import getLogger

import aiohttp
from injector import inject

from core.config.settings import settings
from main_stream_service.yandex_music_api import YandexMusicAPI
from ruark_audio_system.exceptions import RuarkDeviceNotFoundError
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from yandex_station.constants import (
    ALICE_ACTIVE_STATES,
    RUARK_IDLE_VOLUME,
    STREAMING_RESTART_DELAY,
)
from yandex_station.models import Track
from yandex_station.station_controls import YandexStationControls
from yandex_station.station_ws_control import YandexStationClient

logger = getLogger(__name__)


class MainStreamManager:
    """Класс для управления стримингом."""

    _ws_client: YandexStationClient
    _station_controls: YandexStationControls
    _ruark_controls: RuarkR5Controller
    _yandex_music_api: YandexMusicAPI
    _stream_state_running: bool
    _stream_server_url: str
    _ruark_volume: int
    _tasks: list[asyncio.Task]

    @inject
    def __init__(
        self,
        station_ws_client: YandexStationClient,
        station_controls: YandexStationControls,
        ruark_controls: RuarkR5Controller,
        yandex_music_api: YandexMusicAPI,
    ):

        self._ws_client = station_ws_client
        self._station_controls = station_controls
        self._ruark_controls = ruark_controls
        self._yandex_music_api = yandex_music_api
        self._stream_server_url = settings.local_server_host
        self._ruark_volume = 0
        self._stream_state_running = False
        self._tasks = []  # Хранение фоновых задач

    async def start(self):
        """Запуск всех стриминговых процессов."""
        if self._stream_state_running or self._tasks:
            logger.info("⚠️ Стриминг уже запущен")
            return

        logger.info("🎵 Запуск стриминга")
        try:
            # Поиск устройств выполняется здесь, а не при старте процесса
            if not await self._ruark_controls.connect():
                raise RuarkDeviceNotFoundError(
                    f"Устройство "
                    f"'{self._ruark_controls.device_name}' "
                    f"не найдено в сети"
                )
            logger.info("🔄 Запуск WebSocket клиента")
            await self._station_controls.start_ws_client()
        except Exception as e:
            logger.error(f"❌ Не удалось запустить стриминг: {e}")
            return

        self._stream_state_running = True
        logger.info("🎬 Запуск обёртки стриминга")
        stream_task = asyncio.create_task(self._wrap_streaming())
        logger.info("✅ WebSocket клиент запущен")
        self._tasks.extend([stream_task])

    async def stop(self):
        """Остановка всех стриминговых процессов."""
        logger.info("🛑 Остановка стриминга...")
        self._stream_state_running = False
        await self._ruark_controls.stop()
        await self._stop_stream_on_stream_server()
        await self._ruark_controls.set_volume(self._ruark_volume)
        await self._ruark_controls.turn_power_off()
        await self._station_controls.unmute()
        # Остановка WebSocket-клиента
        await self._station_controls.stop_ws_client()

        # Отмена всех активных задач
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("✅ Стриминг остановлен")

    async def streaming(self):
        """Основной поток управления стримингом."""
        try:
            logger.info("📡 Поток streaming() стартовал")
            await self._prepare_devices()

            last_alice_state = await self._station_controls.get_alice_state()
            last_track = Track(
                id="0",
                type="",
                artist="",
                title="",
                duration=0,
                progress=0,
                playing=False,
            )
            stuck_track_count = 0
            last_track_progress = 0
            volume_set_count = 0
            speak_count = 0
            track_url: str | None = None

            while self._stream_state_running:
                track = await self._station_controls.get_current_track()
                if track is None:
                    logger.warning(
                        "⚠️ Нет данных о треке, пропускаем итерацию"
                    )
                    await asyncio.sleep(1.0)
                    continue
                current_alice_state = (
                    await self._station_controls.get_alice_state()
                )

                if current_alice_state != last_alice_state:
                    current_volume = await self._station_controls.get_volume()
                    if (
                        current_alice_state in ALICE_ACTIVE_STATES
                        and volume_set_count < 1
                    ):
                        volume_set_count += 1
                        speak_count += 1

                        self._ruark_volume = (
                            await self._ruark_controls.get_volume()
                        )
                        await self._ruark_controls.set_volume(
                            RUARK_IDLE_VOLUME
                        )

                        if current_volume == 0:
                            await self._station_controls.unmute()

                if current_alice_state == "IDLE":
                    if not track.playing:
                        if last_track_progress == track.progress:
                            await self._ruark_controls.stop()
                        else:
                            stuck_track_count += 1
                            if stuck_track_count > 2:
                                logger.warning(
                                    "⚠️ Трек застрял, перезапускаем"
                                )
                                if not await self._recover_stuck_track(
                                    track, last_track_progress
                                ):
                                    logger.warning(
                                        "⚠️ Не удалось перезапустить трек "
                                        "через stop/play"
                                    )
                                stuck_track_count = 0

                    if (
                        last_track_progress != track.progress
                        and track.type == "FmRadio"
                        and not await self._ruark_controls.is_playing()
                    ):
                        logger.info("🔁 Возобновляем воспроизведение радио")
                        await self._send_track_to_stream_server(
                            track_url=await self._station_controls.get_radio_url(),
                            radio=True,
                        )
                        await asyncio.sleep(1)

                    if track.id == last_track.id:
                        track = (
                            await self._station_controls.get_current_track()
                        )

                    if last_track.id != track.id and track.playing:
                        if track.type == "FmRadio":
                            track_url = (
                                await self._station_controls.get_radio_url()
                            )
                            logger.info(f"🎵 URL радиостанции: {track_url}")
                        else:
                            track_url = (
                                await self._yandex_music_api.get_file_info(
                                    track_id=track.id,
                                    quality=settings.stream_quality,
                                )
                            )
                        await self._send_track_to_stream_server(
                            track_url,
                            radio=True if track.type == "FmRadio" else False,
                        )
                        last_track = track

                    if speak_count > 0 and track.playing:
                        logger.info("🔁 Возвращаем громкость Ruark")
                        await self._ruark_controls.set_volume(
                            self._ruark_volume
                        )

                        for _ in range(30):
                            if await self._ruark_controls.is_playing():
                                logger.info("▶️ Ruark начал играть")
                                await self._station_controls.fade_out_alice_volume()
                                speak_count = 0
                                break
                            await asyncio.sleep(0.1)
                        else:
                            logger.warning(
                                "⚠️ Ruark так и не начал играть, "
                                "перезапуск трека на стрим сервере"
                            )
                            if track_url:
                                await self._send_track_to_stream_server(
                                    track_url,
                                    radio=track.type == "FmRadio",
                                )
                            else:
                                logger.warning(
                                    "⚠️ Нет сохранённого URL трека "
                                    "для перезапуска"
                                )
                            await self._station_controls.fade_out_alice_volume()
                            speak_count = 0

                    if speak_count > 0 and not track.playing:
                        await self._ruark_controls.set_volume(
                            self._ruark_volume
                        )

                    current_volume = await self._station_controls.get_volume()

                    if (
                        (
                            current_volume > 0
                            and track.duration - track.progress > 10
                            and track.type != "FmRadio"
                        )
                        or (track.type == "FmRadio" and track.playing)
                    ) and track.playing:
                        await self._station_controls.fade_out_alice_volume()

                    volume_set_count = 0

                if (
                    track.duration - track.progress < 1
                    and current_alice_state == "IDLE"
                    and track.playing
                    and track.type != "FmRadio"
                ):
                    await self._station_controls.unmute()

                self._log_current_track(
                    track, current_alice_state, last_alice_state
                )
                last_track_progress = track.progress
                last_alice_state = current_alice_state
                logger.debug("💤 Цикл стриминга работает")
                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            logger.info("🛑 Стриминг завершён по команде остановки")
        except Exception as e:
            logger.error(f"❌ Ошибка в стриминге: {e}")
            raise

    async def _wrap_streaming(self):
        """Следит за потоком стриминга и перезапускает его при падении."""
        while self._stream_state_running:
            try:
                logger.info("🚀 Запуск потока стриминга")
                await self.streaming()
            except asyncio.CancelledError:
                logger.info("🛑 Поток стриминга остановлен")
                break
            except Exception as e:
                logger.error(f"❌ Поток стриминга упал с ошибкой: {e}")
                logger.info(
                    f"🔁 Перезапуск стриминга через "
                    f"{STREAMING_RESTART_DELAY} секунд..."
                )
                await asyncio.sleep(STREAMING_RESTART_DELAY)
                logger.debug("🔄 Перезапуск потока после падения")

    async def _prepare_devices(self):
        logger.info("🔧 Подготовка устройств к стримингу...")
        await asyncio.sleep(1)
        await self._station_controls.set_default_volume()
        await self._ruark_controls.get_session_id()
        if await self._ruark_controls.get_power_status() == "0":
            await self._ruark_controls.turn_power_on()
        self._ruark_volume = await self._ruark_controls.get_volume()

    async def _send_track_to_stream_server(
        self, track_url: str, radio: bool = False
    ):
        """Отправляет ссылку на трек на стрим сервер."""

        try:
            async with aiohttp.ClientSession() as session:
                logger.info(f"🎵 Отправляем трек на стрим сервер: {track_url}")
                async with session.post(
                    f"http://{self._stream_server_url}:"
                    f"{settings.local_server_port_dlna}/set_stream",
                    params={
                        "yandex_url": track_url,
                        "radio": str(radio).lower(),
                    },
                ) as resp:
                    response = await resp.json()
                    logger.debug(f"Ответ от Ruark API: {response}")
                    return response
        except aiohttp.ClientError as e:
            logger.error(f"Ошибка при отправке трека на Ruark: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Непредвиденная ошибка при отправке трека: {e}")
            return None

    async def _stop_stream_on_stream_server(self):
        """Останавливает стрим на стрим сервере."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://{self._stream_server_url}:"
                f"{settings.local_server_port_dlna}/stop_stream"
            ) as resp:
                response = await resp.json()
                logger.info(
                    f"Ответ от стрим сервера: {response.get('message')}"
                )
                return response

    async def _recover_stuck_track(
        self, track: Track, last_progress: int
    ) -> bool:
        logger.warning(
            "⚠️ Track.playing=False при IDLE, но прогресс меняется — "
            "пробуем перезапустить"
        )
        for _ in range(3):
            await self._station_controls.stop()
            await asyncio.sleep(0.3)
            await self._station_controls.play()
            await asyncio.sleep(0.7)

            updated_track = await self._station_controls.get_current_track()
            if (
                updated_track.id == track.id
                and updated_track.progress > last_progress
            ):
                logger.info("✅ Трек успешно перезапущен")
                return True
        return False

    def _log_current_track(self, track: Track, state: str, last_state: str):
        logger.info(
            f"🎵 Сейчас играет: {track.id} - {track.artist} - "
            f"{track.title} - {track.progress}/{track.duration}, "
            f"статус Алисы: {state}, "
            f"предыдущий статус Алисы: {last_state}, "
            f"проигрывание: {track.playing}"
        )
