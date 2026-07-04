import asyncio
from logging import getLogger

from fastapi import APIRouter, Request

from main_stream_service.main_stream_manager import MainStreamManager

logger = getLogger(__name__)

router = APIRouter()

# Ссылка на задачу запуска, чтобы её не собрал сборщик мусора
_start_task: asyncio.Task | None = None


def _get_manager(request: Request) -> MainStreamManager:
    """Возвращает MainStreamManager, созданный в lifespan приложения."""
    return request.app.state.main_stream_manager


@router.post("/stream_on")
async def stream_on(request: Request):
    """API-команда для запуска стрима."""
    global _start_task
    _start_task = asyncio.create_task(_get_manager(request).start())
    return {"status": "stream_on"}


@router.post("/shutdown")
async def shutdown(request: Request):
    """API-команда для остановки стрима."""
    await _get_manager(request).stop()
    return {"status": "main_service stopped"}
