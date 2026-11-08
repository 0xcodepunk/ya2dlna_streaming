import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from core.enums import Codec
from dlna_stream_server.endpoints import stream
from dlna_stream_server.endpoints.routers import main_router
from dlna_stream_server.handlers.stream_handler import StreamHandler


@pytest.fixture
def handler():
    """Обработчик потока без FFmpeg и Ruark."""
    return AsyncMock(spec=StreamHandler)


@pytest.fixture
async def client(handler):
    """Клиент DLNA-сервера без lifespan: без DI-контейнера и поиска Ruark."""
    app = FastAPI()
    app.include_router(main_router)
    app.state.stream_handler = handler
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://dlna"
    ) as client:
        yield client


async def test_set_stream_passes_codec_enum_to_handler(client, handler):
    response = await client.post(
        "/set_stream", params={"yandex_url": "http://track", "codec": "flac"}
    )

    assert response.status_code == 200
    # Поток запускается фоновой задачей эндпоинта
    await asyncio.gather(*stream._active_tasks.values())
    handler.play_stream.assert_awaited_once_with(
        "http://track", Codec.FLAC, False, 0.0, "", ""
    )
    assert handler.play_stream.await_args.args[1] is Codec.FLAC


async def test_each_set_stream_cancels_previous_hanging_start(client, handler):
    """Новый запрос отменяет зависший запуск, сколько бы их ни было."""
    cancelled = []
    release = asyncio.Event()

    async def hanging_play(yandex_url, *args):
        # Запуск висит, как при зависании UPnP Ruark
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(yandex_url)
            raise

    handler.play_stream.side_effect = hanging_play
    task_ids = []
    for n in range(4):
        response = await client.post(
            "/set_stream",
            params={"yandex_url": f"http://track/{n}", "codec": "mp3"},
        )
        task_ids.append(response.json()["task_id"])
        # Задача стартует, а отменённая успевает выполнить finally
        for _ in range(5):
            await asyncio.sleep(0)

    release.set()
    await asyncio.gather(*stream._active_tasks.values())

    assert len(set(task_ids)) == 4
    assert cancelled == ["http://track/0", "http://track/1", "http://track/2"]


async def test_set_stream_rejects_unknown_codec(client, handler):
    response = await client.post(
        "/set_stream", params={"yandex_url": "http://track", "codec": "ogg"}
    )

    assert response.status_code == 422
    handler.play_stream.assert_not_called()


async def test_set_stream_requires_codec(client, handler):
    """Без codec запрос отвергается, а не играет как MP3."""
    response = await client.post(
        "/set_stream", params={"yandex_url": "http://track"}
    )

    assert response.status_code == 422
    handler.play_stream.assert_not_called()
