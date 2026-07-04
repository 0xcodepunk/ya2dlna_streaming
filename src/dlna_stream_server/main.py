import asyncio
from contextlib import asynccontextmanager
from logging import getLogger
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from core.config.settings import settings
from core.dependencies.main_di_container import MainDIContainer
from core.logging.setup import setup_logging
from dlna_stream_server.endpoints.routers import main_router
from dlna_stream_server.handlers.stream_handler import StreamHandler
from ruark_audio_system.ruark_r5_controller import RuarkR5Controller

setup_logging()

logger = getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Инициализация зависимостей и фоновый поиск Ruark при старте."""
    container = MainDIContainer().get_container()
    app.state.stream_handler = container.get(StreamHandler)
    ruark = container.get(RuarkR5Controller)
    connect_task = asyncio.create_task(ruark.connect())
    logger.info("✅ Зависимости DLNA сервера инициализированы")

    yield

    if not connect_task.done():
        connect_task.cancel()
    await app.state.stream_handler.stop_ffmpeg()
    logger.info("⏹ DLNA сервер остановлен")


app = FastAPI(lifespan=lifespan)

app.include_router(main_router)


def main():
    """Запускает DLNA стриминговый сервер."""
    logger.info("▶️ Запуск dlna стримингового сервера...")
    uvicorn.run(
        app,
        host=settings.local_server_host,
        port=settings.local_server_port_dlna,
    )


if __name__ == "__main__":
    main()
