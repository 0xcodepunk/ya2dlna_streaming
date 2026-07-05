from contextlib import asynccontextmanager
from logging import getLogger
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from api.endpoints.routers import main_router
from core.config.settings import settings
from core.dependencies.main_di_container import MainDIContainer
from core.logging.setup import setup_logging
from main_stream_service.main_stream_manager import MainStreamManager

setup_logging("api")

logger = getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Создание DI-контейнера и менеджера стриминга при старте."""
    container = MainDIContainer().get_container()
    app.state.main_stream_manager = container.get(MainStreamManager)
    logger.info("✅ Зависимости API сервиса инициализированы")
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(main_router)


def main():
    """Запускает API сервер управления стримингом."""
    logger.info("▶️ Запуск API сервера...")
    uvicorn.run(
        app,
        host=settings.local_server_host,
        port=settings.local_server_port_api,
    )


if __name__ == "__main__":
    main()
