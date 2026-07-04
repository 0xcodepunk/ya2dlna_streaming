import ipaddress
from logging import getLogger
from time import monotonic, sleep

from zeroconf import (
    ServiceBrowser,
    ServiceListener,
    ServiceStateChange,
    Zeroconf,
)

logger = getLogger(__name__)


class DeviceFinder(ServiceListener):
    """Класс для поиска устройств Yandex Station в сети."""

    def __init__(self):
        self.device = {}
        self.zeroconf = Zeroconf()
        self.browser: ServiceBrowser | None = None

    def find_devices(
        self,
        type_: str = "_yandexio._tcp.local.",
        timeout: float = 5.0,
    ) -> bool:
        """Ищет устройство Yandex Station не дольше timeout секунд.

        Блокирующий вызов, запускать через asyncio.to_thread.

        Args:
            type_ (str): Тип mDNS-сервиса для поиска.
            timeout (float): Максимальное время ожидания в секундах.
        Returns:
            bool: True, если устройство найдено.
        """
        if self.device:
            return True
        if self.browser is None:
            self.browser = ServiceBrowser(
                zc=self.zeroconf, type_=type_, handlers=[self._handler_device]
            )
        deadline = monotonic() + timeout
        while not self.device and monotonic() < deadline:
            sleep(0.1)
        return bool(self.device)

    def _handler_device(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Обработчик событий для устройств Yandex Station."""
        try:
            info = zeroconf.get_service_info(service_type, name)
            properties = {
                a.decode(): v.decode() for a, v in info.properties.items()
            }
            logger.info(f"Properties: {properties}")

            self.device = {
                "device_id": properties["deviceId"],
                "platform": properties["platform"],
                "host": str(ipaddress.ip_address(info.addresses[0])),
                "port": info.port,
            }
            logger.info(f"Найдены устройства: {self.device}")

        except Exception as e:
            logger.error(f"Error: {e}")

    def close(self):
        """Закрытие zeroconf."""
        self.zeroconf.close()
