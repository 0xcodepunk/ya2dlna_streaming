import asyncio
from logging import getLogger

from fastapi import APIRouter, Request

from main_stream_service.main_stream_manager import MainStreamManager

logger = getLogger(__name__)

router = APIRouter()

# Ссылки на задачи запуска, чтобы их не собрал сборщик мусора:
# задача держится до своего завершения, повторный запрос её не вытесняет
_start_tasks: set[asyncio.Task[None]] = set()


def _get_manager(request: Request) -> MainStreamManager:
    """Возвращает MainStreamManager, созданный в lifespan приложения."""
    return request.app.state.main_stream_manager


@router.post("/stream_on")
async def stream_on(request: Request):
    """API-команда для запуска стрима."""
    task = asyncio.create_task(_get_manager(request).start())
    _start_tasks.add(task)
    task.add_done_callback(_start_tasks.discard)
    return {"status": "stream_on"}


@router.post("/shutdown")
async def shutdown(request: Request):
    """API-команда для остановки стрима."""
    await _get_manager(request).stop()
    return {"status": "main_service stopped"}
