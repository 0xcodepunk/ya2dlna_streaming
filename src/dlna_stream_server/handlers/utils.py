import re
import urllib.parse
from logging import getLogger

import aiohttp
from fastapi import Request

logger = getLogger(__name__)

INDEX_URL_PATTERN = re.compile(r"(/kal/[^/]+/[^/]+/index-[\w\-]+\.m3u8\?\S*)")


def extract_index_url(playlist_url: str, playlist_text: str) -> str:
    """Извлекает абсолютный URL index-*.m3u8 из текста мастер-плейлиста.

    Args:
        playlist_url (str): URL, с которого получен плейлист.
        playlist_text (str): Текст мастер-плейлиста.
    Returns:
        str: Абсолютный URL index-*.m3u8.
    Raises:
        ValueError: Если путь index-*.m3u8 не найден в плейлисте.
    """
    match = INDEX_URL_PATTERN.search(playlist_text)
    if not match:
        raise ValueError(
            "Не найден полный путь index-*.m3u8 в мастер-плейлисте"
        )
    return urllib.parse.urljoin(playlist_url, match.group(1))


async def get_latest_index_url(master_url: str) -> str:
    """Получает последний index-*.m3u8 из мастер-плейлиста."""
    logger.info(
        f"🔍 Получаем последний index-*.m3u8 из мастер-плейлиста: {master_url}"
    )
    async with aiohttp.ClientSession() as client:
        async with client.get(master_url) as response:
            response.raise_for_status()
            playlist_text = await response.text()
            # База для относительных путей — конечный URL после редиректов
            final_url = str(response.url)
    full_url = extract_index_url(final_url, playlist_text)
    logger.info(f"🔍 Найденный полный URL: {full_url}")
    return full_url


async def ruark_r5_request_logger(request: Request) -> None:
    """Логирует параметры входящего запроса от Ruark R5."""
    logger.info(
        f"ℹ️ User-Agent: {request.headers.get('user-agent', 'unknown')}"
    )
    logger.info(f"ℹ️ URL запроса: {request.url}")
    logger.info(f"ℹ️ Метод запроса: {request.method}")
    logger.info(f"ℹ️ Заголовки запроса: {request.headers}")
