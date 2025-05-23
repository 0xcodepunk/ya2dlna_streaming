from core.logging.setup import setup_logging  # noqa: F401
from logging import getLogger

import uvicorn
from fastapi import FastAPI

from api.endpoints.routers import main_router
from core.config.settings import settings


# setup_logging()

logger = getLogger(__name__)

app = FastAPI()

app.include_router(main_router)


def main():
    logger.info("▶️ Запуск API сервера...")
    uvicorn.run(
        app,
        host=settings.local_server_host,
        port=settings.local_server_port_api
    )


if __name__ == "__main__":
    main()
