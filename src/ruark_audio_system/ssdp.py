import socket
from logging import getLogger

logger = getLogger(__name__)

SSDP_PORT = 1900

M_SEARCH_REQUEST = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: {host}:{port}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    "ST: upnp:rootdevice\r\n"
    "\r\n"
)


def ssdp_locate(host: str, timeout: float = 2.0) -> str | None:
    """Запрашивает location описания UPnP-устройства напрямую у IP.

    Юникастовый M-SEARCH вместо сканирования всей сети: устройство
    с известным адресом отвечает за десятки миллисекунд.

    Args:
        host (str): IP-адрес устройства.
        timeout (float): Ожидание ответа в секундах.
    Returns:
        str | None: URL описания устройства или None, если нет ответа.
    """
    request = M_SEARCH_REQUEST.format(host=host, port=SSDP_PORT).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(request, (host, SSDP_PORT))
        data, _ = sock.recvfrom(4096)
        for line in data.decode(errors="ignore").splitlines():
            if line.lower().startswith("location:"):
                return line.split(":", 1)[1].strip()
        return None
    except OSError as e:
        logger.info(f"ℹ️ {host} не ответил на M-SEARCH: {e}")
        return None
    finally:
        sock.close()
