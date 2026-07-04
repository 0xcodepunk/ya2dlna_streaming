import asyncio
from logging import getLogger
from typing import Awaitable, Callable, Sequence

logger = getLogger(__name__)

# Колбэк вызывается после успешного перезапуска потока с флагом radio
RestartCallback = Callable[[bool], Awaitable[None]]


class FfmpegSupervisor:
    """Владеет процессом FFmpeg: запуск, мониторинг stderr, перезапуск.

    Политика перезапуска: радио перезапускается сразу (HLS-сегменты
    заканчиваются кодом 0), трек — только при ошибке, с растущей
    задержкой и ограничением числа попыток.
    """

    def __init__(
        self,
        on_restarted: RestartCallback | None = None,
        max_restart_attempts: int = 3,
    ) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._restart_attempts = 0
        self._max_restart_attempts = max_restart_attempts
        self._is_restarting = False
        self._current_url: str | None = None
        self._current_radio = False
        self._current_params: Sequence[str] | None = None
        self._on_restarted = on_restarted

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        """Текущий процесс FFmpeg или None."""
        return self._process

    def reset_restart_state(self) -> None:
        """Сбрасывает счётчик попыток перед новым пользовательским стартом."""
        self._restart_attempts = 0
        self._is_restarting = False

    async def start(
        self, url: str, params: Sequence[str], radio: bool = False
    ) -> None:
        """Запускает FFmpeg, гася предыдущий процесс в фоне.

        Args:
            url (str): Источник потока, подставляется в params.
            params (Sequence[str]): Аргументы FFmpeg с плейсхолдером
                {yandex_url}.
            radio (bool): Режим радио (влияет на политику перезапуска).
        """
        # Сохраняем ссылки на старый процесс для фонового завершения
        old_process = self._process
        old_monitor_task = self._monitor_task

        # Сбрасываем текущие ссылки сразу, не дожидаясь завершения старого
        self._process = None
        self._monitor_task = None

        await self._cancel_restart_task()

        logger.info(f"🎥 Запуск потоковой передачи с {url}")

        # Запускаем фоновое завершение старого процесса (не блокирующе)
        if old_process:
            asyncio.create_task(self._terminate(old_process, old_monitor_task))

        self._current_url = url
        self._current_radio = radio
        self._current_params = list(params)

        command = [
            (
                param.format(yandex_url=url)
                if "{yandex_url}" in param
                else param
            )
            for param in self._current_params
        ]
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        logger.info(f"🎥 Запущен процесс FFmpeg с PID: {self._process.pid}")

        self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        """Останавливает процесс FFmpeg и все служебные задачи."""
        await self._cancel_restart_task()

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        if self._process:
            proc = self._process
            # Сбрасываем состояние сразу (монитор уже отменён выше)
            self._process = None
            self._current_url = None
            self._current_radio = False
            self._current_params = None
            self._restart_attempts = 0

            logger.info("⏹ Останавливаем текущий поток FFmpeg...")
            await self._terminate(proc, None)

    async def _cancel_restart_task(self) -> None:
        """Отменяет задачу перезапуска, если она выполняется."""
        if self._restart_task:
            self._restart_task.cancel()
            try:
                await self._restart_task
            except asyncio.CancelledError:
                pass
            self._restart_task = None
        self._is_restarting = False

    async def _monitor(self) -> None:
        """Ждёт завершения процесса и решает, нужен ли перезапуск."""
        if not self._process:
            return

        proc = self._process
        logger.info(f"🔍 Начинаем мониторинг FFmpeg процесса PID: {proc.pid}")

        try:
            # Читаем stderr в отдельной задаче
            stderr_task = asyncio.create_task(self._log_stderr(proc))

            returncode = await proc.wait()

            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

            if returncode == 0:
                if self._current_radio:
                    self._restart_task = asyncio.create_task(
                        self._safe_restart()
                    )
                    logger.info(
                        "🔄 Перезапускаем поток радио в фоновом режиме"
                    )
                logger.info(
                    f"✅ FFmpeg процесс завершился нормально "
                    f"(код: {returncode}) - трек закончился естественным путем"
                )
            else:
                logger.warning(
                    f"⚠️ FFmpeg процесс завершился с ошибкой "
                    f"(код: {returncode})"
                )

            # Восстановление трека — только при ошибках
            if self._process == proc and self._current_url and returncode != 0:
                logger.warning(
                    "⚠️ Запускаем автоматическое восстановление потока в фоне"
                )
                self._restart_task = asyncio.create_task(self._safe_restart())

        except asyncio.CancelledError:
            logger.info("🔍 Мониторинг FFmpeg процесса отменен")
        except Exception as e:
            logger.exception(f"❌ Ошибка в мониторинге FFmpeg: {e}")

    async def _safe_restart(self) -> None:
        """Безопасный перезапуск с очисткой задачи после завершения."""
        try:
            await self._restart()
        except Exception as e:
            logger.exception(f"❌ Ошибка в безопасном перезапуске: {e}")
        finally:
            self._restart_task = None

    async def _restart(self) -> None:
        """Перезапускает поток с сохранёнными URL и параметрами."""
        if self._is_restarting:
            logger.info("⏸️ Перезапуск уже выполняется, пропускаем")
            return

        if not self._current_url or not self._current_params:
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
        # Прогрессивная задержка; радио перезапускается сразу
        delay = (
            min(2**self._restart_attempts, 30)
            if not self._current_radio
            else 0
        )

        try:
            logger.info(
                f"🔄 Перезапускаем поток (попытка {self._restart_attempts}/"
                f"{self._max_restart_attempts}) через {delay}s с "
                f"{self._current_url}"
            )
            await asyncio.sleep(delay)

            radio = self._current_radio
            await self.start(self._current_url, self._current_params, radio)

            if self._on_restarted is not None:
                await self._on_restarted(radio)

            self._restart_attempts = 0
            logger.info("✅ Поток успешно перезапущен быстрой логикой!")

        except Exception as e:
            logger.exception(f"❌ Ошибка при перезапуске потока: {e}")
        finally:
            self._is_restarting = False

    async def _terminate(
        self,
        proc: asyncio.subprocess.Process,
        monitor_task: asyncio.Task[None] | None,
    ) -> None:
        """Гасит процесс FFmpeg: SIGTERM, по таймауту — SIGKILL."""
        logger.info(f"🔄 Фоновое завершение FFmpeg процесса PID: {proc.pid}")

        if monitor_task:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        try:
            proc.terminate()
            logger.info(f"📤 SIGTERM отправлен старому FFmpeg PID: {proc.pid}")

            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
                logger.info(
                    f"✅ Старый FFmpeg завершился, код: "
                    f"{proc.returncode}, PID: {proc.pid}"
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"⚠️ Старый FFmpeg PID {proc.pid} не завершился "
                    f"вовремя, принудительное завершение"
                )
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                    logger.info(
                        f"✅ Старый FFmpeg принудительно завершён, "
                        f"код: {proc.returncode}"
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        f"❌ Старый FFmpeg PID {proc.pid} "
                        f"не завершился даже после kill()"
                    )

        except ProcessLookupError:
            logger.info(
                f"⚠️ Старый FFmpeg PID {proc.pid} уже завершился "
                f"(ProcessLookupError)"
            )
        except Exception as e:
            logger.exception(
                f"❌ Ошибка при фоновом завершении FFmpeg "
                f"PID {proc.pid}: {e}"
            )

    async def _log_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Логирование stderr FFmpeg процесса.

        Строки фильтруются по уровням важности.
        """
        stderr = proc.stderr
        if stderr is None:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()
                if not line_str:
                    continue

                lower_line = line_str.lower()

                # Диагностика: обязательно логируем все ошибки и завершения
                error_keywords = [
                    "fatal",
                    "cannot open",
                    "invalid argument",
                    "invalid data found",
                    "no such file",
                    "permission denied",
                ]
                warning_keywords = [
                    "error",
                    "failed",
                    "connection",
                    "broken",
                    "timeout",
                    "invalid data found",
                    "deprecated",
                ]

                # Специальные ключевые слова для диагностики
                critical_keywords = [
                    "segmentation fault",
                    "core dumped",
                    "killed",
                    "terminated",
                    "aborted",
                ]

                if any(keyword in lower_line for keyword in critical_keywords):
                    logger.error(f"💥 FFmpeg CRITICAL: {line_str}")
                elif any(keyword in lower_line for keyword in error_keywords):
                    logger.error(f"🔥 FFmpeg error: {line_str}")
                elif any(
                    keyword in lower_line for keyword in warning_keywords
                ):
                    logger.debug(f"⚠️ FFmpeg warning: {line_str}")
                elif "duration:" in lower_line or "bitrate:" in lower_line:
                    # Информация о файле - важно для диагностики
                    logger.debug(f"📋 FFmpeg info: {line_str}")
                else:
                    logger.debug(f"📝 FFmpeg: {line_str}")
        except Exception as e:
            logger.debug(f"🛑 Завершено чтение stderr: {e}")
