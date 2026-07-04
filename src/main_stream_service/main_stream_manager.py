import asyncio
import time
from dataclasses import dataclass
from logging import getLogger

import aiohttp
from injector import inject

from core.config.settings import settings
from main_stream_service.yandex_music_api import YandexMusicAPI
from ruark_audio_system.exceptions import RuarkDeviceNotFoundError
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller
from ruark_audio_system.volume_store import RuarkVolumeStore
from yandex_station.constants import (
    ALICE_ACTIVE_STATES,
    PROGRESS_JUMP_THRESHOLD,
    RUARK_IDLE_VOLUME,
    STREAM_POLL_INTERVAL,
    STREAMING_RESTART_DELAY,
)
from yandex_station.models import Track
from yandex_station.station_controls import YandexStationControls
from yandex_station.station_ws_control import YandexStationClient

logger = getLogger(__name__)


@dataclass
class _CycleContext:
    """Изменяемое состояние цикла стриминга между итерациями."""

    last_track: Track
    last_alice_state: str | None
    last_track_progress: float = 0.0
    # Момент снятия снапшота прогресса (time.monotonic)
    last_progress_at: float = 0.0
    last_track_playing: bool = False
    stuck_track_count: int = 0
    volume_set_count: int = 0
    speak_count: int = 0
    track_url: str | None = None
    last_log_signature: tuple[str, bool, str | None] | None = None


class MainStreamManager:
    """Класс для управления стримингом."""

    _ws_client: YandexStationClient
    _station_controls: YandexStationControls
    _ruark_controls: RuarkR5Controller
    _yandex_music_api: YandexMusicAPI
    _stream_state_running: bool
    _stream_server_url: str
    _ruark_volume: int
    _tasks: list[asyncio.Task[None]]

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
        self._volume_store = RuarkVolumeStore()
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
        await self._remember_ruark_volume()
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

            ctx = _CycleContext(
                last_track=Track(
                    id="0",
                    type="",
                    artist="",
                    title="",
                    duration=0,
                    progress=0,
                    playing=False,
                ),
                last_alice_state=(
                    await self._station_controls.get_alice_state()
                ),
            )

            while self._stream_state_running:
                track = await self._station_controls.get_current_track()
                if track is None:
                    logger.warning(
                        "⚠️ Нет данных о треке, пропускаем итерацию"
                    )
                    await self._ws_client.wait_for_state_update(
                        STREAM_POLL_INTERVAL
                    )
                    continue
                alice_state = await self._station_controls.get_alice_state()

                await self._duck_ruark_on_alice_speech(alice_state, ctx)

                if alice_state == "IDLE":
                    track = await self._handle_idle_cycle(track, ctx)

                await self._unmute_before_track_end(track, alice_state)

                self._log_current_track(track, alice_state, ctx)
                ctx.last_track_progress = track.progress
                ctx.last_progress_at = time.monotonic()
                ctx.last_track_playing = track.playing
                ctx.last_alice_state = alice_state
                # Новое сообщение станции будит цикл сразу,
                # таймаут — heartbeat для проверок по времени
                await self._ws_client.wait_for_state_update(
                    STREAM_POLL_INTERVAL
                )

        except asyncio.CancelledError:
            logger.info("🛑 Стриминг завершён по команде остановки")
        except Exception as e:
            logger.error(f"❌ Ошибка в стриминге: {e}")
            raise

    async def _duck_ruark_on_alice_speech(
        self, alice_state: str | None, ctx: "_CycleContext"
    ) -> None:
        """Приглушает Ruark, когда Алиса начинает говорить или слушать."""
        if alice_state == ctx.last_alice_state:
            return

        current_volume = await self._station_controls.get_volume()
        if alice_state in ALICE_ACTIVE_STATES and ctx.volume_set_count < 1:
            ctx.volume_set_count += 1
            ctx.speak_count += 1

            self._ruark_volume = await self._ruark_controls.get_volume()
            await self._ruark_controls.set_volume(RUARK_IDLE_VOLUME)

            if current_volume == 0:
                await self._station_controls.unmute()

    async def _handle_idle_cycle(
        self, track: Track, ctx: "_CycleContext"
    ) -> Track:
        """Обрабатывает итерацию цикла, когда Алиса молчит (IDLE).

        Returns:
            Track: Актуальный трек (мог обновиться при повторном опросе).
        """
        await self._stop_or_recover_paused_track(track, ctx)
        await self._resume_radio_if_silent(track, ctx)
        track = await self._refresh_track_if_unchanged(track, ctx)
        await self._resync_after_progress_jump(track, ctx)
        await self._switch_to_new_track(track, ctx)
        await self._restore_ruark_after_speech(track, ctx)
        await self._fade_alice_if_playing(track)
        ctx.volume_set_count = 0
        return track

    async def _stop_or_recover_paused_track(
        self, track: Track, ctx: "_CycleContext"
    ) -> None:
        """Останавливает Ruark на паузе или перезапускает застрявший трек."""
        if track.playing:
            return

        if ctx.last_track_progress == track.progress:
            await self._ruark_controls.stop()
            return

        # Прогресс меняется при playing=False — трек застрял
        ctx.stuck_track_count += 1
        if ctx.stuck_track_count > 2:
            logger.warning("⚠️ Трек застрял, перезапускаем")
            if not await self._recover_stuck_track(
                track, ctx.last_track_progress
            ):
                logger.warning(
                    "⚠️ Не удалось перезапустить трек через stop/play"
                )
            ctx.stuck_track_count = 0

    async def _resume_radio_if_silent(
        self, track: Track, ctx: "_CycleContext"
    ) -> None:
        """Возобновляет радио, если станция играет, а Ruark молчит."""
        if not (
            ctx.last_track_progress != track.progress
            and track.type == "FmRadio"
            and not await self._ruark_controls.is_playing()
        ):
            return

        logger.info("🔁 Возобновляем воспроизведение радио")
        radio_url = await self._station_controls.get_radio_url()
        if radio_url:
            await self._send_track_to_stream_server(
                track_url=radio_url,
                radio=True,
            )
            await asyncio.sleep(1)
        else:
            logger.warning("⚠️ Не удалось получить URL радиостанции")

    async def _refresh_track_if_unchanged(
        self, track: Track, ctx: "_CycleContext"
    ) -> Track:
        """Повторно опрашивает станцию, если id трека не сменился."""
        if track.id != ctx.last_track.id:
            return track

        refreshed_track = await self._station_controls.get_current_track()
        return refreshed_track if refreshed_track is not None else track

    async def _resync_after_progress_jump(
        self, track: Track, ctx: "_CycleContext"
    ) -> None:
        """Перезапускает локальный стрим с текущей позиции трека.

        Ловит разрывы, при которых id трека не меняется: повтор
        (прогресс к нулю), перемотку (скачок в любую сторону)
        и продолжение после паузы. Прогресс сравнивается с ожидаемым
        значением с учётом времени, прошедшего со снапшота, — иначе
        долгая итерация (fade, запросы) выглядела бы как скачок.
        """
        if track.type == "FmRadio" or not track.playing:
            return
        if track.id != ctx.last_track.id:
            return

        resumed = not ctx.last_track_playing
        elapsed = time.monotonic() - ctx.last_progress_at
        expected_progress = ctx.last_track_progress + elapsed
        jumped = (
            abs(track.progress - expected_progress) > PROGRESS_JUMP_THRESHOLD
        )
        if not (resumed or jumped):
            return

        reason = "продолжение после паузы" if resumed else "разрыв прогресса"
        logger.info(
            f"🔁 Ресинк стрима ({reason}): ожидали "
            f"~{expected_progress:.0f}s, станция на {track.progress:.0f}s"
        )
        resync_started = time.monotonic()
        track_url = await self._yandex_music_api.get_file_info(
            track_id=track.id,
            quality=settings.stream_quality,
        )
        if not track_url:
            logger.warning("⚠️ Не удалось получить URL трека для ресинка")
            return

        ctx.track_url = track_url
        # flush: Ruark обязан сбросить буфер, иначе новая позиция
        # прозвучит только после доигрывания забуференного хвоста
        await self._send_track_to_stream_server(
            track_url,
            radio=False,
            start_position=track.progress,
            flush=True,
        )
        logger.info(
            f"⏱ Ресинк выполнен за "
            f"{time.monotonic() - resync_started:.2f}с"
        )

    async def _switch_to_new_track(
        self, track: Track, ctx: "_CycleContext"
    ) -> None:
        """Отправляет новый трек на стрим сервер при смене id.

        Если станция уже в глубине трека (стрим включили посреди
        проигрывания), поток стартует с её текущей позиции.
        """
        if not (ctx.last_track.id != track.id and track.playing):
            return

        switch_started = time.monotonic()
        if track.type == "FmRadio":
            ctx.track_url = await self._station_controls.get_radio_url()
            logger.info(f"🎵 URL радиостанции: {ctx.track_url}")
        else:
            ctx.track_url = await self._yandex_music_api.get_file_info(
                track_id=track.id,
                quality=settings.stream_quality,
            )
        resolve_seconds = time.monotonic() - switch_started

        if ctx.track_url:
            start_position = 0.0
            if (
                track.type != "FmRadio"
                and track.progress > PROGRESS_JUMP_THRESHOLD
            ):
                start_position = track.progress
                logger.info(
                    f"▶️ Трек уже играет на станции, продолжаем "
                    f"с {start_position:.0f}s"
                )
            send_started = time.monotonic()
            await self._send_track_to_stream_server(
                ctx.track_url,
                radio=track.type == "FmRadio",
                start_position=start_position,
            )
            logger.info(
                f"⏱ Переключение на {track.id}: ссылка "
                f"{resolve_seconds:.2f}с, отправка "
                f"{time.monotonic() - send_started:.2f}с"
            )
            ctx.last_track = track
        else:
            logger.warning(
                f"⚠️ Не удалось получить URL для трека "
                f"{track.id}, повторим на следующей итерации"
            )

    async def _restore_ruark_after_speech(
        self, track: Track, ctx: "_CycleContext"
    ) -> None:
        """Возвращает громкость Ruark после речи Алисы."""
        if ctx.speak_count > 0 and track.playing:
            logger.info("🔁 Возвращаем громкость Ruark")
            await self._ruark_controls.set_volume(self._ruark_volume)

            for _ in range(30):
                if await self._ruark_controls.is_playing():
                    logger.info("▶️ Ruark начал играть")
                    await self._station_controls.fade_out_alice_volume()
                    ctx.speak_count = 0
                    break
                await asyncio.sleep(0.1)
            else:
                logger.warning(
                    "⚠️ Ruark так и не начал играть, "
                    "перезапуск трека на стрим сервере"
                )
                if ctx.track_url:
                    # Для трека продолжаем с текущей позиции станции,
                    # а не с начала — иначе рассинхрон
                    await self._send_track_to_stream_server(
                        ctx.track_url,
                        radio=track.type == "FmRadio",
                        start_position=(
                            track.progress if track.type != "FmRadio" else 0.0
                        ),
                    )
                else:
                    logger.warning(
                        "⚠️ Нет сохранённого URL трека для перезапуска"
                    )
                await self._station_controls.fade_out_alice_volume()
                ctx.speak_count = 0

        if ctx.speak_count > 0 and not track.playing:
            await self._ruark_controls.set_volume(self._ruark_volume)

    async def _fade_alice_if_playing(self, track: Track) -> None:
        """Плавно глушит Алису, пока трек или радио играют на Ruark."""
        current_volume = await self._station_controls.get_volume()

        if (
            (
                current_volume is not None
                and current_volume > 0
                and track.duration - track.progress > 10
                and track.type != "FmRadio"
            )
            or (track.type == "FmRadio" and track.playing)
        ) and track.playing:
            await self._station_controls.fade_out_alice_volume()

    async def _unmute_before_track_end(
        self, track: Track, alice_state: str | None
    ) -> None:
        """Возвращает громкость станции перед самым концом трека."""
        if (
            track.duration - track.progress < 1
            and alice_state == "IDLE"
            and track.playing
            and track.type != "FmRadio"
        ):
            await self._station_controls.unmute()

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

    async def _remember_ruark_volume(self):
        """Запоминает пользовательскую громкость Ruark для нового сеанса."""
        try:
            current_volume = await self._ruark_controls.get_volume()
            # Уровень не выше приглушения — Ruark задакан речью Алисы,
            # пользовательской громкостью остаётся сохранённый снапшот
            if current_volume > RUARK_IDLE_VOLUME:
                self._ruark_volume = current_volume
        except Exception as e:
            logger.warning(
                f"⚠️ Не удалось прочитать громкость Ruark, "
                f"сохраняем последнюю известную: {e}"
            )
        self._volume_store.save(self._ruark_volume)

    async def _prepare_devices(self):
        logger.info("🔧 Подготовка устройств к стримингу...")
        await asyncio.sleep(1)
        await self._station_controls.set_default_volume()
        await self._ruark_controls.get_session_id()
        if await self._ruark_controls.get_power_status() == "0":
            await self._ruark_controls.turn_power_on()

        saved_volume = self._volume_store.load()
        if saved_volume is not None:
            # Восстанавливаем громкость прошлого сеанса
            await self._ruark_controls.set_volume(saved_volume)
            self._ruark_volume = saved_volume
            logger.info(
                f"🔊 Восстановлена громкость Ruark прошлого сеанса: "
                f"{saved_volume}"
            )
        else:
            self._ruark_volume = await self._ruark_controls.get_volume()

    async def _send_track_to_stream_server(
        self,
        track_url: str,
        radio: bool = False,
        start_position: float = 0.0,
        flush: bool = False,
    ):
        """Отправляет ссылку на трек на стрим сервер.

        Args:
            track_url (str): Прямая ссылка на источник потока.
            radio (bool): Режим радио.
            start_position (float): Позиция старта в секундах.
            flush (bool): Принудительная привязка Ruark со сбросом буфера.
        """
        try:
            async with aiohttp.ClientSession() as session:
                logger.info(f"🎵 Отправляем трек на стрим сервер: {track_url}")
                async with session.post(
                    f"http://{self._stream_server_url}:"
                    f"{settings.local_server_port_dlna}/set_stream",
                    params={
                        "yandex_url": track_url,
                        "radio": str(radio).lower(),
                        "start_position": f"{start_position:.1f}",
                        "flush": str(flush).lower(),
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
        self, track: Track, last_progress: float
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
                updated_track is not None
                and updated_track.id == track.id
                and updated_track.progress > last_progress
            ):
                logger.info("✅ Трек успешно перезапущен")
                return True
        return False

    def _log_current_track(
        self,
        track: Track,
        state: str | None,
        ctx: "_CycleContext",
    ):
        """Логирует состояние: INFO при изменении, DEBUG на каждом тике."""
        message = (
            f"🎵 Сейчас играет: {track.id} - {track.artist} - "
            f"{track.title} - {track.progress}/{track.duration}, "
            f"статус Алисы: {state}, "
            f"предыдущий статус Алисы: {ctx.last_alice_state}, "
            f"проигрывание: {track.playing}"
        )
        signature = (track.id, track.playing, state)
        if signature != ctx.last_log_signature:
            logger.info(message)
            ctx.last_log_signature = signature
        else:
            logger.debug(message)
