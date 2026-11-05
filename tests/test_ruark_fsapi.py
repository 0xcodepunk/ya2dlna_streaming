import asyncio
import traceback
from unittest.mock import AsyncMock

import pytest

from ruark_audio_system.exceptions import (
    RuarkApiError,
    RuarkDeviceNotFoundError,
)
from ruark_audio_system.fsapi_client import RuarkFsApiClient

SESSION_RESPONSE = (
    "<fsapiResponse><sessionId>12345</sessionId></fsapiResponse>"
)
POWER_ON_RESPONSE = "<fsapiResponse><value><u8>1</u8></value></fsapiResponse>"
POWER_OFF_RESPONSE = "<fsapiResponse><value><u8>0</u8></value></fsapiResponse>"
SET_OK_RESPONSE = "<fsapiResponse><status>FS_OK</status></fsapiResponse>"
SECRET_PIN = "dummy-pin"


def make_client(*responses: str) -> RuarkFsApiClient:
    """Создаёт клиент с привязанным IP и замоканными ответами fsapi."""
    client = RuarkFsApiClient(pin="1234")
    client.bind("10.0.0.1")
    client._get = AsyncMock(side_effect=list(responses))
    return client


@pytest.fixture(
    params=[
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
        b"not http at all\r\n\r\n",
    ],
    ids=["status-403", "not-http"],
)
async def failing_device(request):
    """Запускает сервер с заданным ответом и отдаёт адрес устройства."""

    async def reply(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(request.param)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(reply, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    async with server:
        yield f"{host}:{port}"


def traceback_text(error: BaseException) -> str:
    """Возвращает трассировку так, как её напечатает лог, с цепочкой причин."""
    return "".join(traceback.format_exception(error))


async def test_failed_request_hides_pin_in_traceback(failing_device):
    client = RuarkFsApiClient(pin=SECRET_PIN)
    client.bind(failing_device)

    with pytest.raises(RuarkApiError) as error:
        await client.create_session()

    assert SECRET_PIN not in traceback_text(error.value)


async def test_invalid_address_hides_pin_in_traceback():
    client = RuarkFsApiClient(pin=SECRET_PIN)
    client.bind("[bad")

    with pytest.raises(RuarkApiError) as error:
        await client.create_session()

    assert SECRET_PIN not in traceback_text(error.value)


async def test_failed_power_switch_hides_pin_in_log(failing_device, caplog):
    client = RuarkFsApiClient(pin=SECRET_PIN)
    client.bind(failing_device)

    assert await client.turn_power_on() is False
    assert SECRET_PIN not in caplog.text


async def test_create_session_parses_and_stores_session_id():
    client = make_client(SESSION_RESPONSE)

    assert await client.create_session() == "12345"
    assert client._session_id == "12345"


async def test_create_session_raises_on_malformed_response():
    client = make_client("<html>Bad Request</html>")

    with pytest.raises(RuarkApiError):
        await client.create_session()


async def test_get_power_status_parses_value():
    client = make_client(POWER_ON_RESPONSE)

    assert await client.get_power_status() == "1"


async def test_turn_power_on_verifies_final_status():
    client = make_client(SET_OK_RESPONSE, POWER_ON_RESPONSE)

    assert await client.turn_power_on() is True


async def test_turn_power_off_returns_false_when_status_mismatch():
    # После SET статус остался "1" — выключение не удалось
    client = make_client(SET_OK_RESPONSE, POWER_ON_RESPONSE)

    assert await client.turn_power_off() is False


async def test_unbound_client_raises_device_not_found():
    client = RuarkFsApiClient(pin="1234")

    with pytest.raises(RuarkDeviceNotFoundError):
        await client.create_session()
