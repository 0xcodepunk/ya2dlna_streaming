import asyncio
import os
import time
from logging import getLogger
from typing import Any, Awaitable, Callable, Sequence

import aiohttp
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from core.config.settings import settings
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller

from .constants import (
    FFMPEG_AAC_PARAMS,
    FFMPEG_LOCAL_MP3_PARAMS,
    FFMPEG_MP3_PARAMS,
)
from .ffmpeg_supervisor import FfmpegSupervisor

logger = getLogger(__name__)


def _insert_start_position(
    params: Sequence[str], start_position: float
) -> list[str]:
    """Вставляет -ss перед -i, чтобы FFmpeg начал с заданной позиции."""
    result = list(params)
    if start_position <= 0:
        return result
    try:
        input_index = result.index("-i")
    except ValueError:
        logger.warning("⚠️ В параметрах FFmpeg нет -i, позиция не применена")
        return result
    result[input_index:input_index] = ["-ss", f"{start_position:.1f}"]
    return result


class StreamHandler:
    """Класс для управления потоковой передачей и воспроизведением на Ruark."""

    def __init__(self, ruark_controls: RuarkR5Controller):
        self._ruark_controls = ruark_controls
        self._ruark_lock = asyncio.Lock()
        self._ffmpeg = FfmpegSupervisor(on_restarted=self._reattach_ruark)
        # Количество клиентов, читающих /live_stream.mp3 прямо сейчас
        self._active_clients = 0
        # Формат потока подключённого клиента (флаг radio)
        self._client_radio: bool | None = None
        # Сколько ждать новый процесс FFmpeg при бесшовной смене трека
        self._switch_grace = 5.0

    async def execute_with_lock(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> None:
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

    def _local_stream_url(self, radio: bool) -> str:
        """Строит URL локального стрима для Ruark."""
        return (
            f"http://{settings.local_server_host}:"
            f"{settings.local_server_port_dlna}/live_stream.mp3"
            f"?radio={str(radio).lower()}"
        )

    async def _reattach_ruark(self, radio: bool, force: bool = False) -> None:
        """Привязывает Ruark к локальному стриму, если он ещё не на нём.

        При подключённом клиенте того же формата привязка не нужна:
        байты продолжают течь по открытому HTTP-ответу без разрыва.
        force заставляет привязать в любом случае — Ruark при этом
        сбрасывает свой буфер (нужно для перемотки).
        """
        if (
            not force
            and self._active_clients > 0
            and self._client_radio == radio
        ):
            logger.info(
                "🔗 Клиент уже на потоке — привязка Ruark не требуется"
            )
            return

        track_url = self._local_stream_url(radio)
        await self.execute_with_lock(
            self._ruark_controls.set_av_transport_uri, track_url
        )
        await self.execute_with_lock(self._ruark_controls.play)

    async def _wait_for_replacement(
        self, old_proc: asyncio.subprocess.Process, radio: bool
    ) -> asyncio.subprocess.Process | None:
        """Ждёт новый процесс FFmpeg для бесшовной смены трека.

        Returns:
            Новый живой процесс или None, если поток остановлен,
            сменился формат (radio) или замена не появилась вовремя.
        """
        deadline = time.monotonic() + self._switch_grace
        while time.monotonic() < deadline:
            candidate = self._ffmpeg.process
            if (
                candidate is not None
                and candidate is not old_proc
                and candidate.stdout is not None
                and candidate.returncode is None
            ):
                if self._ffmpeg.current_radio != radio:
                    # Формат сменился — нужен новый HTTP-ответ
                    return None
                return candidate
            await asyncio.sleep(0.1)
        return None

    async def stop_ffmpeg(self) -> None:
        """Останавливает текущий процесс FFmpeg, если он запущен."""
        await self._ffmpeg.stop()

    async def start_ffmpeg_stream(
        self,
        yandex_url: str,
        radio: bool = False,
        start_position: float = 0.0,
    ) -> None:
        """Готовит источник и запускает потоковую передачу через FFmpeg.

        Args:
            yandex_url (str): Прямая ссылка на источник потока.
            radio (bool): Режим радио.
            start_position (float): Позиция старта трека в секундах.
        """
        # Очищаем папку от старых MP3 файлов перед запуском нового стрима
        if not radio and settings.stream_is_local_file:
            asyncio.create_task(self._cleanup_mp3_files())

        if radio:
            # Мастер-плейлист передаётся FFmpeg как есть: вложенные
            # плейлисты привязаны к сессии (hlssid) и должны запрашиваться
            # тем же клиентом, что получил мастер
            params = self._get_ffmpeg_params(codec="aac")
        else:
            yandex_url = (
                await self._download_and_get_local_mp3_path(yandex_url)
                if settings.stream_is_local_file
                else yandex_url
            )
            params = _insert_start_position(
                self._get_ffmpeg_params(
                    codec="mp3",
                    is_local_file=settings.stream_is_local_file,
                ),
                start_position,
            )

        await self._ffmpeg.start(yandex_url, params, radio)

    async def stream_audio(self, radio: bool = False) -> StreamingResponse:
        """Отдаёт потоковый аудио-ответ клиенту.

        Поток бесшовный: при смене трека генератор дожидается нового
        процесса FFmpeg и продолжает отдачу без разрыва HTTP-ответа.
        Защита от залипания сохранена: если FFmpeg не даёт данных —
        поток закрывается.
        """
        proc = self._ffmpeg.process
        if not proc or proc.stdout is None:
            raise HTTPException(status_code=404, detail="Поток не запущен")

        async def generate():
            current = proc
            stdout = current.stdout
            empty_count = 0
            # Счётчик таймаутов для снижения шума в логах
            timeout_count = 0
            total_bytes_sent = 0
            serve_started = time.monotonic()
            first_chunk_sent = False
            self._active_clients += 1
            self._client_radio = radio
            try:
                while True:
                    # Процесс кончился: пробуем бесшовно перейти на новый
                    if stdout is None or stdout.at_eof():
                        replacement = await self._wait_for_replacement(
                            current, radio
                        )
                        if replacement is None:
                            logger.info(
                                "📭 Поток завершён — замены процесса нет"
                            )
                            break
                        logger.info(
                            "🔗 Бесшовное переключение на новый "
                            "процесс FFmpeg"
                        )
                        current = replacement
                        stdout = current.stdout
                        continue

                    try:
                        chunk = await asyncio.wait_for(
                            stdout.read(4096),
                            timeout=15,  # Увеличили с 5 до 15 секунд
                        )
                    except asyncio.TimeoutError:
                        timeout_count += 1
                        # Логируем только каждый 3-й таймаут для снижения шума
                        if timeout_count % 3 == 0:
                            logger.warning(
                                f"⌛ Таймаут чтения stdout #{timeout_count} — "
                                f"возможно, зависание"
                            )
                        chunk = b""

                    if not chunk:
                        if stdout.at_eof():
                            # EOF обработает ветка переключения выше
                            continue
                        empty_count += 1
                        logger.debug(
                            f"📭 Пустой chunk ({empty_count}), ждем данные"
                        )
                        await asyncio.sleep(1.5)
                        if empty_count >= 10:
                            logger.error(
                                "❌ Поток завис: 10 пустых чтений подряд — "
                                "останавливаем FFmpeg"
                            )
                            await self.stop_ffmpeg()
                            break
                        continue

                    empty_count = 0
                    # Сбрасываем счётчик при получении данных
                    timeout_count = 0
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        logger.info(
                            f"⏱ Первый чанк клиенту через "
                            f"{time.monotonic() - serve_started:.2f}с "
                            f"после подключения"
                        )
                    total_bytes_sent += len(chunk)
                    # Диагностика: логируем прогресс передачи данных
                    if total_bytes_sent % (1024 * 1024) == 0:  # Каждый МБ
                        logger.info(
                            f"📊 Передано данных: "
                            f"{total_bytes_sent // 1024 // 1024} МБ"
                        )

                    yield chunk

                # После выхода из цикла логируем завершение FFmpeg
                if current.returncode is not None:
                    if current.returncode == 0:
                        logger.info(
                            f"✅ FFmpeg процесс завершился нормально "
                            f"(код: {current.returncode}) - "
                            "трек закончился естественным путем"
                        )
                    else:
                        logger.warning(
                            f"⚠️ FFmpeg процесс завершился с ошибкой "
                            f"(код: {current.returncode})"
                        )

            except asyncio.CancelledError:
                logger.info("🔌 Клиент отключился от стрима")
                logger.info(
                    f"📊 Всего передано данных: {total_bytes_sent} байт"
                )
                # Диагностика: проверяем состояние FFmpeg при отключении
                if current.returncode is None:
                    logger.debug(
                        "⚠️ FFmpeg всё ещё работает после отключения клиента"
                    )
                else:
                    logger.info(
                        f"ℹ️ FFmpeg завершился с кодом: {current.returncode}"
                    )
                raise
            except Exception as e:
                logger.exception(f"❌ Ошибка во время генерации стрима: {e}")
                logger.info(
                    f"📊 Всего передано данных: {total_bytes_sent} байт"
                )
                await self.stop_ffmpeg()
            finally:
                self._active_clients -= 1
                if self._active_clients == 0:
                    self._client_radio = None

        media_type = "audio/mpeg" if not radio else "audio/aac"

        logger.info(f"🎧 Отправляем стрим с типом {media_type}")

        return StreamingResponse(generate(), media_type=media_type)

    async def play_stream(
        self,
        yandex_url: str,
        radio: bool = False,
        start_position: float = 0.0,
        flush: bool = False,
    ) -> None:
        """Запускает потоковую трансляцию и передает её на Ruark.

        Args:
            yandex_url (str): Прямая ссылка на источник потока.
            radio (bool): Режим радио.
            start_position (float): Позиция старта трека в секундах.
            flush (bool): Принудительная привязка Ruark со сбросом буфера.
        """
        logger.info(f"🎶 Начинаем потоковое воспроизведение {yandex_url}")

        # Сбрасываем счетчик попыток и флаги для нового потока
        self._ffmpeg.reset_restart_state()

        try:
            play_started = time.monotonic()
            # Запускаем потоковую передачу (быстро, без ожидания)
            await self.start_ffmpeg_stream(yandex_url, radio, start_position)
            ffmpeg_seconds = time.monotonic() - play_started

            track_url = self._local_stream_url(radio)
            logger.info(f"📡 Поток доступен по URL: {track_url}")

            reattach_started = time.monotonic()
            await self._reattach_ruark(radio, force=flush)

            logger.info(
                f"⏱ Запуск потока: FFmpeg {ffmpeg_seconds:.2f}с, "
                f"привязка Ruark "
                f"{time.monotonic() - reattach_started:.2f}с"
            )
            logger.info("✅ Переключение трека завершено быстро!")

        except Exception as e:
            logger.exception(f"❌ Ошибка при запуске потока: {e}")
            await self.stop_ffmpeg()
            raise

    async def _download_and_get_local_mp3_path(self, yandex_url: str) -> str:
        """Скачивает MP3 файл по ссылке и возвращает локальный путь."""
        async with aiohttp.ClientSession() as session:
            async with session.get(yandex_url) as response:
                if response.status != 200:
                    logger.error(
                        f"Не удалось получить MP3 файл: {response.status}"
                    )
                    raise HTTPException(
                        status_code=404, detail="Не удалось получить MP3 файл"
                    )
                # Сохраняем в папку handlers/mp3_files
                mp3_dir = os.path.join(os.path.dirname(__file__), "mp3_files")
                os.makedirs(mp3_dir, exist_ok=True)

                filename = yandex_url.split("/")[-1]
                mp3_local_path = os.path.join(mp3_dir, filename)

                if not mp3_local_path.endswith(".mp3"):
                    mp3_local_path += ".mp3"
                with open(mp3_local_path, "wb") as file:
                    file.write(await response.read())
                logger.info(f"✅ MP3 файл сохранён в {mp3_local_path}")
                return mp3_local_path

    async def _cleanup_mp3_files(self) -> None:
        """Очищает папку handlers/mp3_files от всех сохранённых MP3 файлов."""
        mp3_dir = os.path.join(os.path.dirname(__file__), "mp3_files")
        try:
            if os.path.exists(mp3_dir):
                # Удаляем все файлы в папке
                for filename in os.listdir(mp3_dir):
                    file_path = os.path.join(mp3_dir, filename)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        logger.info(f"🗑️ Удалён старый MP3 файл: {file_path}")
                logger.info(f"🧹 Папка {mp3_dir} очищена от старых MP3 файлов")
            else:
                logger.info(
                    f"📁 Папка {mp3_dir} не существует, пропускаем очистку"
                )
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при очистке папки {mp3_dir}: {e}")

    def _get_ffmpeg_params(
        self, codec: str, is_local_file: bool = False
    ) -> Sequence[str]:
        """Возвращает набор параметров FFmpeg для кодека."""
        if codec == "mp3":
            return (
                FFMPEG_LOCAL_MP3_PARAMS if is_local_file else FFMPEG_MP3_PARAMS
            )
        elif codec == "aac":
            return FFMPEG_AAC_PARAMS
        else:
            raise ValueError(f"Неизвестный кодек {codec}")
