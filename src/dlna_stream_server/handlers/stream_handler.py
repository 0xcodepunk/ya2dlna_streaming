import asyncio
from logging import getLogger

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from core.config.settings import settings
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller

from .constants import (FFMPEG_AAC_PARAMS, FFMPEG_MP3_PARAMS,  # noqa: F401
                        FFMPEG_STABLE_PARAMS)
from .utils import get_latest_index_url

logger = getLogger(__name__)


class StreamHandler:
    """Класс для управления потоковой передачей и воспроизведением на Ruark."""
    def __init__(self, ruark_controls: RuarkR5Controller):
        self._radio_url: str | None = None
        self._ruark_lock = asyncio.Lock()
        self._ffmpeg_process: asyncio.subprocess.Process | None = None
        self._ruark_controls = ruark_controls
        self._current_url: str | None = None
        self._current_radio: bool = False
        self._current_ffmpeg_params: list[str] | None = None
        self._monitor_task: asyncio.Task | None = None
        self._restart_attempts = 0
        self._max_restart_attempts = 3
        self._restart_task: asyncio.Task | None = None
        self._is_restarting = False

    async def execute_with_lock(self, func, *args, **kwargs):
        """Выполняет вызов UPnP-команды в Ruark с блокировкой."""
        async with self._ruark_lock:
            for attempt in range(3):
                try:
                    logger.debug(
                        f"Выполняем {func.__name__} с аргументами "
                        f"{args}, {kwargs}"
                    )
                    await func(*args, **kwargs)
                    logger.debug(f"✅ {func.__name__} выполнено успешно")
                    return
                except Exception as e:
                    logger.warning(
                        f"⚠️ Ошибка при {func.__name__}, "
                        f"попытка {attempt + 1}: {e}"
                    )
                    await asyncio.sleep(1)

    async def _monitor_ffmpeg_process(self):
        """Мониторинг состояния FFmpeg процесса и логирование stderr."""
        if not self._ffmpeg_process:
            return

        proc = self._ffmpeg_process
        logger.info(
            f"🔍 Начинаем мониторинг FFmpeg процесса PID: {proc.pid}"
        )

        try:
            # Читаем stderr в отдельной задаче
            stderr_task = asyncio.create_task(self._log_stderr(proc))

            # Ждем завершения процесса
            returncode = await proc.wait()
            logger.warning(
                f"⚠️ FFmpeg процесс завершился с кодом: {returncode}"
            )

            # Отменяем задачу чтения stderr
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

            # Проверяем нужность восстановления
            if self._ffmpeg_process == proc and self._current_url:
                logger.info("🔄 Пытаемся автоматически восстановить поток...")
                await asyncio.sleep(2)  # Небольшая задержка перед перезапуском
                await self._restart_stream()

        except asyncio.CancelledError:
            logger.info("🔍 Мониторинг FFmpeg процесса отменен")
        except Exception as e:
            logger.exception(f"❌ Ошибка в мониторинге FFmpeg: {e}")

    async def _log_stderr(self, proc):
        """
        Логирование stderr FFmpeg процесса
        с фильтрацией по уровням важности.
        """
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                line_str = line.decode('utf-8', errors='ignore').strip()
                if not line_str:
                    continue

                lower_line = line_str.lower()

                error_keywords = [
                    'fatal', 'cannot open', 'invalid argument',
                    'invalid data found'
                ]
                warning_keywords = [
                    'error', 'failed', 'connection', 'broken', 'timeout',
                    'invalid data found'
                ]

                if any(keyword in lower_line for keyword in error_keywords):
                    logger.error(f"🔥 FFmpeg error: {line_str}")
                elif any(
                    keyword in lower_line for keyword in warning_keywords
                ):
                    logger.warning(f"⚠️ FFmpeg warning: {line_str}")
                else:
                    logger.debug(f"📝 FFmpeg: {line_str}")
        except Exception as e:
            logger.debug(f"🛑 Завершено чтение stderr: {e}")

    async def _restart_stream(self):
        """Перезапуск потока с текущим URL."""
        if self._is_restarting:
            logger.info("⏸️ Перезапуск уже выполняется, пропускаем")
            return

        if not self._current_url:
            logger.warning("⚠️ Нет сохраненного URL для перезапуска")
            return

        if self._restart_attempts >= self._max_restart_attempts:
            logger.error(
                f"❌ Превышено максимальное количество попыток перезапуска "
                f"({self._max_restart_attempts}). Останавливаем."
            )
            return

        self._is_restarting = True
        self._restart_attempts += 1
        delay = min(2 ** self._restart_attempts, 30)  # Прогрессивная задержка

        try:
            logger.info(
                f"🔄 Перезапускаем поток (попытка {self._restart_attempts}/"
                f"{self._max_restart_attempts}) через {delay}s с "
                f"{self._current_url}"
            )
            await asyncio.sleep(delay)

            if self._current_radio:
                # При перезапуске передаем исходный мастер-плейлист
                await self.start_ffmpeg_stream(
                    self._radio_url, self._current_radio
                )
            else:
                await self.start_ffmpeg_stream(
                    self._current_url, self._current_radio
                )

            track_url = (
                f"http://{settings.local_server_host}:"
                f"{settings.local_server_port_dlna}/live_stream.mp3"
                f"?radio={str(self._current_radio).lower()}"
            )
            await self.execute_with_lock(
                self._ruark_controls.set_av_transport_uri,
                track_url
            )
            await self.execute_with_lock(
                self._ruark_controls.play
            )
            self._restart_attempts = 0
            logger.info("✅ Поток успешно перезапущен")

        except Exception as e:
            logger.exception(f"❌ Ошибка при перезапуске потока: {e}")
        finally:
            self._is_restarting = False

    async def stop_ffmpeg(self):
        """Останавливает текущий процесс FFmpeg, если он запущен."""
        # Отменяем задачу перезапуска если она выполняется
        if self._restart_task:
            self._restart_task.cancel()
            try:
                await self._restart_task
            except asyncio.CancelledError:
                pass
            self._restart_task = None

        self._is_restarting = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        if self._ffmpeg_process:
            proc = self._ffmpeg_process
            self._ffmpeg_process = None  # избегаем гонки
            self._current_url = None
            self._current_radio = False
            self._radio_url = None

            logger.info("⏹ Останавливаем текущий поток FFmpeg...")

            try:
                proc.terminate()
                logger.info("📤 SIGTERM отправлен FFmpeg")

                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                    logger.info(
                        f"✅ FFmpeg завершился, код: {proc.returncode}, "
                        f"PID: {proc.pid}"
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "⚠️ FFmpeg не завершился вовремя, "
                        "принудительное завершение."
                    )
                    proc.kill()
                    logger.debug("💀 Отправили kill()")

                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                        logger.info(
                            f"✅ FFmpeg принудительно завершён, "
                            f"код: {proc.returncode}"
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            "❌ FFmpeg не завершился даже после kill() — "
                            "залипший процесс!"
                        )

            except ProcessLookupError:
                logger.warning("⚠️ FFmpeg уже завершился (ProcessLookupError)")
            except Exception as e:
                logger.exception(f"❌ Ошибка при остановке FFmpeg: {e}")

    async def start_ffmpeg_stream(self, yandex_url: str, radio: bool = False):
        """Запускает потоковую передачу через FFmpeg."""
        await self.stop_ffmpeg()  # Останавливаем старый процесс
        if self._current_ffmpeg_params:
            self._current_ffmpeg_params = None
        logger.info(f"🎥 Запуск потоковой передачи с {yandex_url}")
        if radio:
            # Сохраняем исходный мастер-плейлист
            self._radio_url = yandex_url
            yandex_url = await get_latest_index_url(self._radio_url)
            self._current_ffmpeg_params = self._get_ffmpeg_params(codec="aac")
        else:
            self._current_ffmpeg_params = self._get_ffmpeg_params(codec="mp3")
        self._current_url = yandex_url
        self._current_radio = radio

        # Улучшенные параметры для стабильной работы с временными ссылками
        ffmpeg_params = [
            param.format(yandex_url=yandex_url)
            if isinstance(param, str) and '{yandex_url}' in param
            else param
            for param in self._current_ffmpeg_params
        ]
        self._ffmpeg_process = await asyncio.create_subprocess_exec(
            *ffmpeg_params,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        logger.info(
            f"🎥 Запущен процесс FFmpeg с PID: {self._ffmpeg_process.pid}"
        )

        self._monitor_task = asyncio.create_task(
            self._monitor_ffmpeg_process()
        )

    async def stream_audio(self, radio: bool = False):
        if not self._ffmpeg_process:
            raise HTTPException(status_code=404, detail="Поток не запущен")

        async def generate():
            try:
                proc = self._ffmpeg_process
                if not proc:
                    logger.info("🛑 FFmpeg-процесс отсутствует в generate()")
                    return

                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        logger.debug(
                            "📭 Поток данных пуст — отправляем keepalive-байт"
                        )
                        yield b"\0"
                        await asyncio.sleep(1.5)
                        continue
                    yield chunk

            except asyncio.CancelledError:
                logger.info("🔌 Клиент отключился от стрима")
                # Запускаем перезапуск в фоновой задаче только если
                # не выполняется уже другой перезапуск
                if not self._is_restarting and not self._restart_task:
                    self._restart_task = asyncio.create_task(
                        self._safe_restart_stream()
                    )
                raise

        media_type = "audio/mpeg" if not radio else "audio/aac"
        response_headers = {
            "Content-Type": "audio/mpeg" if not radio else "audio/aac",
            "Accept-Ranges": "bytes",
            "Connection": "keep-alive",
        }
        logger.info(
            f"🎧 Отправляем стрим с типом {media_type} и заголовками "
            f"{response_headers}"
        )

        return StreamingResponse(
            generate(),
            media_type=media_type,
            headers=response_headers
        )

    async def _safe_restart_stream(self):
        """Безопасный перезапуск с очисткой задачи после завершения."""
        try:
            await self._restart_stream()
        except Exception as e:
            logger.exception(f"❌ Ошибка в безопасном перезапуске: {e}")
        finally:
            self._restart_task = None

    async def play_stream(self, yandex_url: str, radio: bool = False):
        """Запускает потоковую трансляцию и передает её на Ruark."""
        logger.info(f"🎶 Начинаем потоковое воспроизведение {yandex_url}")

        # Сбрасываем счетчик попыток и флаги для нового потока
        self._restart_attempts = 0
        self._is_restarting = False

        try:
            # Запускаем потоковую передачу
            await self.start_ffmpeg_stream(yandex_url, radio)
            track_url = (
                f"http://{settings.local_server_host}:"
                f"{settings.local_server_port_dlna}/live_stream.mp3"
                f"?radio={str(radio).lower()}"
            )
            logger.info(f"📡 Поток доступен по URL: {track_url}")

            # Устанавливаем новый поток
            await self.execute_with_lock(
                self._ruark_controls.set_av_transport_uri,
                track_url
            )

            # Запускаем воспроизведение
            await self.execute_with_lock(
                self._ruark_controls.play
            )
        except Exception as e:
            logger.exception(f"❌ Ошибка при запуске потока: {e}")
            await self.stop_ffmpeg()
            raise

    def _get_ffmpeg_params(self, codec: str):
        if codec == "mp3":
            # return FFMPEG_MP3_PARAMS
            return FFMPEG_MP3_PARAMS
        elif codec == "aac":
            return FFMPEG_AAC_PARAMS
        else:
            raise ValueError(f"Неизвестный кодек {codec}")
