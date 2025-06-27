import asyncio
from logging import getLogger

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from core.config.settings import settings
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller

logger = getLogger(__name__)


class StreamHandler:
    """Класс для управления потоковой передачей и воспроизведением на Ruark."""
    def __init__(self, ruark_controls: RuarkR5Controller):
        self._ruark_lock = asyncio.Lock()
        self._ffmpeg_process: asyncio.subprocess.Process | None = None
        self._ruark_controls = ruark_controls
        self._current_url: str | None = None
        self._monitor_task: asyncio.Task | None = None
        self._restart_attempts = 0
        self._max_restart_attempts = 3

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
        """Логирование stderr FFmpeg процесса."""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                line_str = line.decode('utf-8', errors='ignore').strip()
                if line_str:
                    # Фильтруем сообщения FFmpeg и логируем только важные
                    keywords = [
                        'error', 'failed', 'invalid', 'connection',
                        'timeout', 'broken'
                    ]
                    if any(
                        keyword in line_str.lower() for keyword in keywords
                    ):
                        logger.error(f"🔥 FFmpeg stderr: {line_str}")
                    else:
                        logger.debug(f"📝 FFmpeg stderr: {line_str}")
        except Exception as e:
            logger.debug(f"Завершено чтение stderr: {e}")

    async def _restart_stream(self):
        """Перезапуск потока с текущим URL."""
        if not self._current_url:
            logger.warning("⚠️ Нет сохраненного URL для перезапуска")
            return

        if self._restart_attempts >= self._max_restart_attempts:
            logger.error(
                f"❌ Превышено максимальное количество попыток перезапуска "
                f"({self._max_restart_attempts}). Останавливаем."
            )
            return

        self._restart_attempts += 1
        delay = min(2 ** self._restart_attempts, 30)  # Прогрессивная задержка

        try:
            logger.info(
                f"🔄 Перезапускаем поток (попытка {self._restart_attempts}/"
                f"{self._max_restart_attempts}) через {delay}s с "
                f"{self._current_url}"
            )
            await asyncio.sleep(delay)

            await self.start_ffmpeg_stream(self._current_url)

            track_url = (
                f"http://{settings.local_server_host}:"
                f"{settings.local_server_port_dlna}/live_stream.mp3"
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

    async def stop_ffmpeg(self):
        """Останавливает текущий процесс FFmpeg, если он запущен."""
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

            logger.info("⏹ Останавливаем текущий поток FFmpeg...")

            try:
                proc.terminate()
                logger.info("📤 SIGTERM отправлен FFmpeg")

                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
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

    async def start_ffmpeg_stream(self, yandex_url: str):
        """Запускает потоковую передачу через FFmpeg."""
        await self.stop_ffmpeg()  # Останавливаем старый процесс

        logger.info(f"🎥 Запуск потоковой передачи с {yandex_url}")
        self._current_url = yandex_url

        # Улучшенные параметры для стабильной работы с временными ссылками
        self._ffmpeg_process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            # Входные параметры
            "-re",  # Читаем файл с реальной скоростью
            "-user_agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "-headers", "Accept: */*",
            "-multiple_requests", "1",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            "-reconnect_at_eof", "1",
            "-timeout", "10000000",
            "-i", yandex_url,
            # Выходные параметры
            "-acodec", "libmp3lame",
            "-b:a", "320k",
            "-f", "mp3",
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts",
            "-max_muxing_queue_size", "1024",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        logger.info(
            f"🎥 Запущен процесс FFmpeg с PID: {self._ffmpeg_process.pid}"
        )

        self._monitor_task = asyncio.create_task(
            self._monitor_ffmpeg_process()
        )

    async def stream_audio(self):
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
                        logger.info("📡 Конец потока данных от FFmpeg")
                        break
                    yield chunk
            except asyncio.CancelledError:
                logger.info("🔌 Клиент отключился от стрима")
                # Не останавливаем FFmpeg при отключении клиента
                raise
            except Exception as e:
                logger.warning(f"⚠️ Ошибка в генераторе потока: {e}")

        return StreamingResponse(generate(), media_type="audio/mpeg")

    async def play_stream(self, yandex_url: str):
        """Запускает потоковую трансляцию и передает её на Ruark."""
        logger.info(f"🎶 Начинаем потоковое воспроизведение {yandex_url}")

        # Сбрасываем счетчик попыток для нового потока
        self._restart_attempts = 0

        try:
            # Запускаем потоковую передачу
            await self.start_ffmpeg_stream(yandex_url)
            track_url = (
                f"http://{settings.local_server_host}:"
                f"{settings.local_server_port_dlna}/live_stream.mp3"
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
