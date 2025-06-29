import asyncio
from logging import getLogger

from fastapi import APIRouter, Response, Request

from core.dependencies.main_di_container import MainDIContainer
from dlna_stream_server.handlers.stream_handler import StreamHandler

logger = getLogger(__name__)


router = APIRouter()

di_container = MainDIContainer().get_container()

stream_handler = di_container.get(StreamHandler)

# Словарь для отслеживания активных задач
_active_tasks = {}


async def _handle_stream_task(
        yandex_url: str,
        task_id: str,
        radio: bool = False
):
    """Обработчик задачи потока с логированием ошибок."""
    try:
        await stream_handler.play_stream(yandex_url, radio)
        logger.info(f"✅ Задача потока {task_id} завершена успешно")
    except Exception as e:
        logger.exception(f"❌ Ошибка в задаче потока {task_id}: {e}")
    finally:
        # Удаляем задачу из отслеживания
        _active_tasks.pop(task_id, None)


@router.post("/set_stream")
async def set_stream(yandex_url: str, radio: bool = False):
    """Принимает URL трека и запускает потоковую передачу на Ruark."""
    logger.info(f"📥 Запуск нового потока с {yandex_url}")

    # Генерируем уникальный ID для задачи
    task_id = f"stream_{len(_active_tasks)}"

    # Отменяем предыдущие активные задачи
    for old_task_id, old_task in list(_active_tasks.items()):
        if not old_task.done():
            logger.info(f"🔄 Отменяем предыдущую задачу {old_task_id}")
            old_task.cancel()
        _active_tasks.pop(old_task_id, None)

    # Запускаем новую задачу
    task = asyncio.create_task(_handle_stream_task(yandex_url, task_id, radio))
    _active_tasks[task_id] = task

    return {
        "message": "Стрим запущен",
        "stream_url": yandex_url,
        "task_id": task_id
    }


@router.get("/live_stream.mp3")
async def serve_stream(request: Request, radio: bool = False):
    """Раздает потоковый аудиофайл через HTTP."""
    user_agent = request.headers.get("user-agent", "unknown")
    logger.info(f"🛰️ Запрос потока от клиента: {user_agent}")
    return await stream_handler.stream_audio(radio)


@router.head("/live_stream.mp3")
async def serve_head(radio: bool = False):
    """Обрабатывает HEAD-запрос для Ruark R5 с корректными заголовками."""
    headers = {
        "Content-Type": "audio/mpeg" if not radio else "audio/aac",
        "Accept-Ranges": "bytes",
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked"
    }
    return Response(headers=headers)


@router.post("/stop_stream")
async def stop_stream():
    """Останавливает потоковую передачу на Ruark."""
    logger.info("🛑 Остановка потоковой передачи...")
    await stream_handler.stop_ffmpeg()
    return {"message": "Потоковая передача остановлена"}
