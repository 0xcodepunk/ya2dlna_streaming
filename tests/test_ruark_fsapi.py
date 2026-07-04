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


def make_client(*responses: str) -> RuarkFsApiClient:
    """Создаёт клиент с привязанным IP и замоканными ответами fsapi."""
    client = RuarkFsApiClient(pin="1234")
    client.bind("10.0.0.1")
    client._get = AsyncMock(side_effect=list(responses))
    return client


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
