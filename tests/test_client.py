import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from yandex_station.exceptions import ClientNotRunningError

DEVICE_TOKEN = "secret-device-token"


def levels_of(caplog, marker):
    """Возвращает уровни записей лога, в тексте которых есть marker."""
    return [
        record.levelno for record in caplog.records if marker in record.message
    ]


@pytest.fixture
def connected_client(mock_station_client):
    """Клиент с открытым WebSocket: станция сразу отвечает на команду."""
    client = mock_station_client
    client.device_token = DEVICE_TOKEN

    async def answer(envelope):
        client.waiters[envelope["id"]][0].set_result(
            {"requestId": envelope["id"]}
        )

    client.websocket = MagicMock(closed=False)
    client.websocket.send_json = AsyncMock(side_effect=answer)
    return client


@pytest.fixture
def found_client(mock_station_client):
    """Клиент с найденной станцией и токеном, готовый к connect()."""
    client = mock_station_client
    client.device_id = "dummy_id"
    client.platform = "dummy_platform"
    client.uri = "wss://localhost:1234"
    client.device_token = DEVICE_TOKEN
    return client


def record_sleeps(monkeypatch, cancel_on=None):
    """Подменяет asyncio.sleep: копит паузы, на cancel_on-й обрывает цикл."""
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == cancel_on:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


def stub_session_tasks(client):
    """Фоновые задачи сеанса завершаются сразу и не трогают asyncio.sleep."""
    client.stream_station_messages = AsyncMock()
    client.command_producer_handler = AsyncMock()
    client.keep_alive_ws_connection = AsyncMock()
    client.clean_expired_futures = AsyncMock()


async def send_through_producer(client, command):
    """Отправляет команду через очередь и останавливает отправителя."""
    producer = asyncio.create_task(client.command_producer_handler())
    await client.send_command(command)
    await client.command_queue.put("stop")
    await producer


@pytest.mark.asyncio
async def test_command_logs_do_not_contain_device_token(
    connected_client, caplog
):
    caplog.set_level(logging.DEBUG)

    await send_through_producer(
        connected_client, {"command": "setVolume", "volume": 0.5}
    )

    assert "setVolume" in caplog.text
    assert DEVICE_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_command_service_messages_logged_at_debug(
    connected_client, caplog
):
    caplog.set_level(logging.DEBUG)

    await send_through_producer(connected_client, {"command": "play"})

    envelope = connected_client.websocket.send_json.await_args.args[0]
    # Очередь, отправка и ответ — три записи с id команды
    assert levels_of(caplog, envelope["id"]) == [logging.DEBUG] * 3


@pytest.mark.asyncio
async def test_ping_frame_logged_at_debug(
    mock_station_client, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    mock_station_client.websocket = MagicMock(closed=False, ping=AsyncMock())
    # Таймаут ответа завершает цикл после первого пинга
    mock_station_client.send_command = AsyncMock(
        return_value={"error": "Timeout"}
    )

    await mock_station_client.keep_alive_ws_connection()

    assert levels_of(caplog, "ping-frame") == [logging.DEBUG]


@pytest.mark.asyncio
async def test_connection_failure_logged_at_info_and_above(
    found_client, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    client = found_client
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "ws_connect",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("refused")),
    )
    # Пауза перед переподключением прерывает бесконечный цикл connect()
    monkeypatch.setattr(
        asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(asyncio.CancelledError):
        await client.connect()

    assert levels_of(caplog, "Подключение к станции") == [logging.INFO]
    assert levels_of(caplog, "WebSocket ошибка") == [logging.ERROR]
    assert levels_of(caplog, "Переподключение через") == [logging.INFO]


@pytest.mark.asyncio
async def test_reconnect_delays_grow_up_to_ceiling(found_client, monkeypatch):
    """Станция недоступна — паузы растут от секунды до потолка."""
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "ws_connect",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("refused")),
    )
    get_token = AsyncMock()
    monkeypatch.setattr(
        "yandex_station.station_ws_control.get_device_token", get_token
    )
    delays = record_sleeps(monkeypatch, cancel_on=7)

    with pytest.raises(asyncio.CancelledError):
        await found_client.connect()

    assert delays == [1, 2, 4, 8, 16, 30, 30]
    # Станция недоступна, а не отвергла токен — quasar не трогаем
    get_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_authorization_resets_reconnect_delay(
    found_client, monkeypatch
):
    """После успешной авторизации реакция на обрыв снова быстрая."""
    refused = aiohttp.ClientConnectionError("refused")
    websocket = MagicMock(closed=False, close=AsyncMock())
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "ws_connect",
        AsyncMock(side_effect=[refused, refused, refused, websocket, refused]),
    )
    stub_session_tasks(found_client)
    found_client.authenticate = AsyncMock(return_value=True)
    delays = record_sleeps(monkeypatch, cancel_on=5)

    with pytest.raises(asyncio.CancelledError):
        await found_client.connect()

    # Сеанс после третьей паузы оборвался — снова секунда, а не 8
    assert delays == [1, 2, 4, 1, 2]


@pytest.mark.asyncio
async def test_failed_authorization_pauses_before_new_token(
    found_client, monkeypatch
):
    """Станция отвергла токен — новый запрашивается после растущей паузы."""
    websocket = MagicMock(closed=False, close=AsyncMock())
    monkeypatch.setattr(
        aiohttp.ClientSession, "ws_connect", AsyncMock(return_value=websocket)
    )
    stub_session_tasks(found_client)
    # Третья попытка авторизации обрывает цикл
    found_client.authenticate = AsyncMock(
        side_effect=[False, False, asyncio.CancelledError]
    )
    timeline = record_sleeps(monkeypatch)

    async def request_token(device_id, platform):
        timeline.append("token")
        return "fresh-token"

    monkeypatch.setattr(
        "yandex_station.station_ws_control.get_device_token", request_token
    )

    with pytest.raises(asyncio.CancelledError):
        await found_client.connect()

    assert timeline == [1, "token", 2, "token"]


@pytest.mark.asyncio
async def test_authorization_logged_at_info(mock_station_client, caplog):
    caplog.set_level(logging.DEBUG)
    mock_station_client.send_command = AsyncMock(
        return_value={"requestId": "r1", "softwareVersion": "1.0"}
    )
    # Версия ПО пишется в файл logs/ рядом с исходниками — в тесте не нужно
    mock_station_client._log_software_version = MagicMock()

    assert await mock_station_client.authenticate() is True

    assert levels_of(caplog, "Авторизация успешна") == [logging.INFO]


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


async def test_close_forgets_station_messages(mock_station_client):
    """Новый сеанс не читает сообщения прошлого и ждёт свежее."""
    mock_station_client.queue.append({"state": {"volume": 0.1}})
    mock_station_client.state_updated.set()

    await mock_station_client.close()

    assert await mock_station_client.get_latest_message() is None
    assert (
        await mock_station_client.wait_for_state_update(timeout=0.01) is False
    )
