import asyncio
import re
from logging import getLogger

import aiohttp

logger = getLogger(__name__)


async def get_latest_index_url(master_url: str) -> str:
    """Получает последний index-*.m3u8 из мастер-плейлиста."""
    async with aiohttp.ClientSession() as client:
        response = await client.get(master_url)
        response.raise_for_status()

    response_string = await response.text()
    pattern = r'(/kal/[^/]+/[^/]+/index-[\w\d\-]+\.m3u8\?[^\s]*)'
    m = re.search(pattern, response_string)
    if not m:
        raise ValueError(
            "Не найден полный путь index-*.m3u8 в мастер-плейлисте"
        )
    # Получаем базовый URL из исходного master_url
    base_url = master_url.split('/kal/')[0]
    full_url = f"{base_url}{m.group(1)}"
    logger.info(f"Найденный полный URL: {full_url}")
    return full_url


if __name__ == "__main__":
    asyncio.run(get_latest_index_url())
