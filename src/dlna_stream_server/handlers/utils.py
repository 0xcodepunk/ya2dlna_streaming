from logging import getLogger

from fastapi import Request

logger = getLogger(__name__)


async def ruark_r5_request_logger(request: Request) -> None:
    """Логирует параметры входящего запроса от Ruark R5."""
    logger.info(
        f"ℹ️ User-Agent: {request.headers.get('user-agent', 'unknown')}"
    )
    logger.info(f"ℹ️ URL запроса: {request.url}")
    logger.info(f"ℹ️ Метод запроса: {request.method}")
    logger.info(f"ℹ️ Заголовки запроса: {request.headers}")
