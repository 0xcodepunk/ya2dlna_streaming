import asyncio
from logging import getLogger

from fastapi import APIRouter, Request, Response

from dlna_stream_server.handlers.stream_handler import StreamHandler
from dlna_stream_server.handlers.utils import ruark_r5_request_logger

logger = getLogger(__name__)


router = APIRouter()

# Словарь для отслеживания активных задач
_active_tasks: dict[str, asyncio.Task[None]] = {}


def _get_stream_handler(request: Request) -> StreamHandler:
    """Возвращает StreamHandler, созданный в lifespan приложения."""
    return request.app.state.stream_handler


async def _handle_stream_task(
    stream_handler: StreamHandler,
    yandex_url: str,
    task_id: str,
    radio: bool = False,
    start_position: float = 0.0,
    title: str = "",
    artist: str = "",
    codec: str = "mp3",
):
    """Обработчик задачи потока с логированием ошибок."""
    try:
        await stream_handler.play_stream(
            yandex_url, radio, start_position, title, artist, codec
        )
        logger.info(f"✅ Задача потока {task_id} завершена успешно")
    except Exception as e:
        logger.exception(f"❌ Ошибка в задаче потока {task_id}: {e}")
    finally:
        # Удаляем задачу из отслеживания
        _active_tasks.pop(task_id, None)


@router.post("/set_stream")
async def set_stream(
    request: Request,
    yandex_url: str,
    radio: bool = False,
    start_position: float = 0.0,
    title: str = "",
    artist: str = "",
    codec: str = "mp3",
):
    """Принимает URL трека и запускает потоковую передачу на Ruark.

    start_position — позиция старта в секундах для ресинка (повтор,
    перемотка, продолжение после паузы); title и artist показываются
    на дисплее Ruark.
    """
    logger.info(
        f"📥 Запуск нового потока с {yandex_url} "
        f"(позиция: {start_position:.1f}s)"
    )

    # Генерируем уникальный ID для задачи
    task_id = f"stream_{len(_active_tasks)}"

    # Отменяем предыдущие активные задачи
    for old_task_id, old_task in list(_active_tasks.items()):
        if not old_task.done():
            logger.info(f"🔄 Отменяем предыдущую задачу {old_task_id}")
            old_task.cancel()
        _active_tasks.pop(old_task_id, None)

    # Запускаем новую задачу
    task = asyncio.create_task(
        _handle_stream_task(
            _get_stream_handler(request),
            yandex_url,
            task_id,
            radio,
            start_position,
            title,
            artist,
            codec,
        )
    )
    _active_tasks[task_id] = task

    return {
        "message": "Стрим запущен",
        "stream_url": yandex_url,
        "task_id": task_id,
    }


@router.get("/live_stream.mp3")
async def serve_stream(request: Request, radio: bool = False):
    """Раздает потоковый аудиофайл через HTTP."""
    await ruark_r5_request_logger(request)
    return await _get_stream_handler(request).stream_audio(radio)


@router.head("/live_stream.mp3")
async def serve_head(request: Request, radio: bool = False):
    """Обрабатывает HEAD-запрос для Ruark R5 с корректными заголовками."""
    headers = {
        "Content-Type": _get_stream_handler(request).stream_media_type,
        "Accept-Ranges": "bytes",
        "Connection": "keep-alive",
    }
    return Response(headers=headers)


@router.post("/stop_stream")
async def stop_stream(request: Request):
    """Останавливает потоковую передачу на Ruark."""
    logger.info("🛑 Остановка потоковой передачи...")
    await _get_stream_handler(request).stop_ffmpeg()
    return {"message": "Потоковая передача остановлена"}
