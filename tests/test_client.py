import pytest

from yandex_station.exceptions import ClientNotRunningError


@pytest.mark.asyncio
async def test_send_command_raises_when_client_not_running(mock_station_client):
    mock_station_client.running = False

    with pytest.raises(ClientNotRunningError) as exc_info:
        await mock_station_client.send_command({"command": "ping"})

    assert "Клиент не активен" in str(exc_info.value)
