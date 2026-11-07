import asyncio
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
VALUE_REGEX = re.compile(r"<value><\w+>(.*?)</\w+></value>")
MODE_ITEM_REGEX = re.compile(r'<item key="(\d+)">(.*?)</item>', re.S)
MODE_ID_REGEX = re.compile(r'<field name="id"><\w+>(.*?)</\w+></field>')
# Режимов у Ruark R5 14, список читается одним запросом
MODE_LIST_MAX_ITEMS = 32
# Режим меняется не мгновенно: сколько раз и как часто проверять, секунды
MODE_CHECK_ATTEMPTS = 10
MODE_CHECK_DELAY = 0.3
# Строка запроса в тексте ошибок aiohttp: в ней PIN и идентификатор сессии
URL_QUERY_REGEX = re.compile(r"\?\S*")


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
        """Выполняет GET-запрос к fsapi и возвращает тело ответа.

        Raises:
            RuarkApiError: Если запрос не удался; текст ошибки без PIN.
        """
        url = f"http://{self._require_ip()}{path}"
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.text()
        except aiohttp.ClientError as e:
            # from None: цепочка причин показала бы исходный текст с PIN
            message = URL_QUERY_REGEX.sub("", str(e))
            raise RuarkApiError(f"Запрос fsapi не удался: {message}") from None

    async def create_session(self) -> str:
        """Создаёт сессию fsapi и запоминает session_id.

        Raises:
            RuarkApiError: Если запрос не удался или ответ без sessionId.
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
            RuarkApiError: Если запрос не удался или ответ без статуса.
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
        except (RuarkApiError, RuarkDeviceNotFoundError) as e:
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

    async def get_mode(self) -> str:
        """Получение ключа текущего режима (источника звука).

        Raises:
            RuarkApiError: Если запрос не удался или ответ без режима.
        """
        content = await self._get(
            f"/fsapi/GET/netRemote.sys.mode"
            f"?pin={self._pin}&sid={self._session_id}"
        )
        match = VALUE_REGEX.search(content)
        if not match:
            raise RuarkApiError(
                f"Ответ netRemote.sys.mode без значения: {content[:200]}"
            )
        return match.group(1)

    async def _mode_key(self, mode_id: str) -> str:
        """Ищет ключ режима по его id в списке режимов устройства.

        Ключи у разных устройств Frontier разные, id постоянны.

        Raises:
            RuarkApiError: Если запрос не удался или режима нет в списке.
        """
        content = await self._get(
            f"/fsapi/LIST_GET_NEXT/netRemote.sys.caps.validModes/-1"
            f"?pin={self._pin}&sid={self._session_id}"
            f"&maxItems={MODE_LIST_MAX_ITEMS}"
        )
        for key, fields in MODE_ITEM_REGEX.findall(content):
            match = MODE_ID_REGEX.search(fields)
            if match and match.group(1) == mode_id:
                return key
        raise RuarkApiError(
            f"Режима {mode_id} нет в списке режимов Ruark: {content[:200]}"
        )

    async def set_mode(self, mode_id: str) -> bool:
        """Переключает режим по id (например AUXIN) и проверяет итог.

        Args:
            mode_id (str): Идентификатор режима из validModes.
        Returns:
            bool: True, если устройство перешло в режим.
        Raises:
            RuarkApiError: Если запрос не удался или режима нет.
        """
        key = await self._mode_key(mode_id)
        await self._get(
            f"/fsapi/SET/netRemote.sys.mode"
            f"?pin={self._pin}&sid={self._session_id}&value={key}"
        )
        for _ in range(MODE_CHECK_ATTEMPTS):
            if await self.get_mode() == key:
                logger.info(f"📻 Режим Ruark: {mode_id}")
                return True
            await asyncio.sleep(MODE_CHECK_DELAY)
        logger.warning(f"⚠️ Ruark не перешёл в режим {mode_id}")
        return False
