import logging

import aiohttp

from core.authorization.exceptions import TokenRequestError
from core.config.settings import settings

logger = logging.getLogger(__name__)


async def get_device_token(device_id: str, platform: str) -> str:
    """Запрашивает токен устройства у quasar.yandex.net.

    Args:
        device_id (str): Идентификатор станции.
        platform (str): Платформа станции.
    Returns:
        str: Токен устройства.
    Raises:
        TokenRequestError: Если сервис вернул ошибку или ответ без токена.
    """
    url = "https://quasar.yandex.net/glagol/token"
    params = {"device_id": device_id, "platform": platform}

    headers = {
        "Authorization": f"OAuth {settings.ya_music_token}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers=headers, params=params
        ) as response:
            response_data = await response.json()

    if response_data.get("status") == "ok" and "token" in response_data:
        logger.info("✅ Токен успешно получен")
        return str(response_data["token"])

    raise TokenRequestError(
        f"Ошибка получения токена устройства {device_id}: {response_data}"
    )
