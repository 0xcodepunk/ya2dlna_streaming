import re
from logging import getLogger

import aiohttp

from ruark_audio_system.exceptions import (
    RuarkApiError,
    RuarkDeviceNotFoundError,
)

logger = getLogger(__name__)

SESSION_ID_REGEX = re.compile(r"<sessionId>(.*?)</sessionId>")
POWER_STATUS_REGEX = re.compile(r"<value><u8>(.*?)</u8></value>")


class RuarkFsApiClient:
    """HTTP-клиент fsapi устройства Ruark: сессия и управление питанием."""

    def __init__(self, pin: str) -> None:
        self._pin = pin
        self._ip: str | None = None
        self._session_id = ""

    def bind(self, ip: str) -> None:
        """Привязывает клиент к IP найденного устройства."""
        self._ip = ip

    def _require_ip(self) -> str:
        """Возвращает IP устройства или бросает ошибку.

        Raises:
            RuarkDeviceNotFoundError: Если устройство ещё не найдено в сети.
        """
        if self._ip is None:
            raise RuarkDeviceNotFoundError(
                "IP устройства Ruark неизвестен — fsapi недоступен, "
                "вызовите connect()"
            )
        return self._ip

    async def _get(self, path: str) -> str:
        """Выполняет GET-запрос к fsapi и возвращает тело ответа."""
        url = f"http://{self._require_ip()}{path}"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.text()

    async def create_session(self) -> str:
        """Создаёт сессию fsapi и запоминает session_id.

        Raises:
            RuarkApiError: Если ответ не содержит sessionId.
        """
        content = await self._get(f"/fsapi/CREATE_SESSION/?pin={self._pin}")
        match = SESSION_ID_REGEX.search(content)
        if not match:
            raise RuarkApiError(
                f"Ответ CREATE_SESSION без sessionId: {content[:200]}"
            )
        self._session_id = match.group(1)
        return self._session_id

    async def get_power_status(self) -> str:
        """Получение статуса питания ('1' — включено, '0' — выключено).

        Raises:
            RuarkApiError: Если ответ не содержит статус питания.
        """
        content = await self._get(
            f"/fsapi/GET/netRemote.sys.power"
            f"?pin={self._pin}&sid={self._session_id}"
        )
        match = POWER_STATUS_REGEX.search(content)
        if not match:
            raise RuarkApiError(
                f"Ответ netRemote.sys.power без статуса: {content[:200]}"
            )
        status = match.group(1)
        logger.info(f"🔌 Статус питания: {status}")
        return status

    async def _set_power(self, value: int) -> bool:
        """Переключает питание и проверяет итоговый статус."""
        try:
            await self._get(
                f"/fsapi/SET/netRemote.sys.power"
                f"?pin={self._pin}&sid={self._session_id}&value={value}"
            )
            return await self.get_power_status() == str(value)
        except (
            aiohttp.ClientError,
            RuarkApiError,
            RuarkDeviceNotFoundError,
        ) as e:
            logger.error(f"Ошибка при переключении питания: {e}")
            return False

    async def turn_power_on(self) -> bool:
        """Включение питания."""
        success = await self._set_power(1)
        if success:
            logger.info("🔌 Питание включено")
        return success

    async def turn_power_off(self) -> bool:
        """Выключение питания."""
        success = await self._set_power(0)
        if success:
            logger.info("🔌 Питание выключено")
        return success
