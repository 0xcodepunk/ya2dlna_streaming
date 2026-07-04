import asyncio

import pytest

from yandex_station.exceptions import ClientNotRunningError


@pytest.mark.asyncio
async def test_send_command_raises_when_client_not_running(
    mock_station_client,
):
    mock_station_client.running = False

    with pytest.raises(ClientNotRunningError) as exc_info:
        await mock_station_client.send_command({"command": "ping"})

    assert "Клиент не активен" in str(exc_info.value)


@pytest.mark.asyncio
async def test_wait_for_state_update_wakes_on_new_message(
    mock_station_client,
):
    async def notify():
        mock_station_client.state_updated.set()

    task = asyncio.create_task(notify())
    woke = await mock_station_client.wait_for_state_update(timeout=1.0)
    await task

    assert woke is True
    assert not mock_station_client.state_updated.is_set()


@pytest.mark.asyncio
async def test_wait_for_state_update_times_out_without_messages(
    mock_station_client,
):
    assert (
        await mock_station_client.wait_for_state_update(timeout=0.05) is False
    )


@pytest.mark.asyncio
async def test_wait_for_state_update_consumes_pending_notification(
    mock_station_client,
):
    # Сообщение пришло, пока цикл обрабатывал предыдущую итерацию
    mock_station_client.state_updated.set()

    assert (
        await mock_station_client.wait_for_state_update(timeout=0.05) is True
    )
    assert not mock_station_client.state_updated.is_set()
