import asyncio
import json
import logging
import ssl
import time
import uuid
from collections import deque
from typing import Dict, Tuple

import aiohttp
from injector import inject

from core.authorization.yandex_tokens import get_device_token
from yandex_station.constants import SOCKET_RECONNECT_DELAY
from yandex_station.exceptions import ClientNotRunningError
from yandex_station.mdns_device_finder import DeviceFinder

logger = logging.getLogger(__name__)


class YandexStationClient:
    """Класс для управления Yandex Station через WebSocket."""

    @inject
    def __init__(
        self,
        device_finder: DeviceFinder,
        device_token: str = None,
        buffer_size: int = 10,
    ):
        self.device_finder = device_finder
        self.device_token = device_token
        self.queue = deque(maxlen=buffer_size)  # Очередь для сообщений станции
        self.waiters: Dict[str, Tuple[asyncio.Future, float]] = {}
        self.lock = asyncio.Lock()
        self.session: aiohttp.ClientSession = None
        self.websocket: aiohttp.ClientWebSocketResponse = None
        self.command_queue = asyncio.Queue()
        self.authenticated = False
        self.running = True
        self.reconnect_required = False
        self._connect_task: asyncio.Task | None = None
        self.tasks = []  # Хранение фоновых задач

        self.device_finder.find_devices()  # Поиск устройств Yandex в сети
        self.device_id = self.device_finder.device["device_id"]
        self.platform = self.device_finder.device["platform"]
        self.uri = (
            f"wss://{self.device_finder.device['host']}:"
            f"{self.device_finder.device['port']}"
        )

    async def run_once(self):
        """Гарантированный однократный запуск WebSocket"""
        if self._connect_task and not self._connect_task.done():
            logger.warning("⚠️ WebSocket уже запущен")
            return

        logger.info("🚀 Запуск WebSocket-клиента в новой задаче")
        self._connect_task = asyncio.create_task(self.connect())
        self._check_duplicate_tasks()

    async def connect(self):
        """Подключение к WebSocket станции."""
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            while True:
                self.reconnect_required = False
                self.running = True

                try:
                    if not self.device_token:
                        self.device_token = await get_device_token(
                            self.device_id, self.platform
                        )

                    if (
                        self.websocket is not None
                        and not self.websocket.closed
                    ):
                        logger.warning(
                            "⚠️ Обнаружено старое WebSocket-соединение, "
                            "закрываем..."
                        )
                        await self.close()

                    if self.session:
                        logger.info(
                            "🔄 Обнаружена существующая HTTP-сессия, "
                            "закрываем..."
                        )
                        await self.session.close()
                        self.session = None

                    async with aiohttp.ClientSession() as session:
                        self.session = session
                        logger.info(f"🔄 Подключение к станции: {self.uri}")
                        self.websocket = await session.ws_connect(
                            self.uri,
                            ssl=ssl_context,
                            timeout=aiohttp.ClientWSTimeout(ws_close=10),
                        )
                        logger.info(
                            "✅ Подключение к WebSocket станции установлено"
                        )

                        await self._cancel_tasks()
                        stream_status_task = asyncio.create_task(
                            self.stream_station_messages()
                        )
                        command_producer_task = asyncio.create_task(
                            self.command_producer_handler()
                        )
                        keep_alive_ws_task = asyncio.create_task(
                            self.keep_alive_ws_connection()
                        )
                        cleanup_task = asyncio.create_task(
                            self.clean_expired_futures()
                        )

                        self.tasks = [
                            stream_status_task,
                            command_producer_task,
                            keep_alive_ws_task,
                            cleanup_task,
                        ]

                        auth_success = await self.authenticate()
                        if not auth_success:
                            logger.warning(
                                "❌ Ошибка авторизации! Требуется новый токен."
                            )
                            await self.refresh_token()
                            continue  # Попробуем снова

                        results = await asyncio.gather(
                            *self.tasks, return_exceptions=True
                        )
                        for i, result in enumerate(results):
                            if isinstance(result, Exception):
                                logger.error(
                                    f"Задача {i} завершилась "
                                    f"с ошибкой: {result}"
                                )

                except aiohttp.ClientError as e:
                    logger.error(f"❌ WebSocket ошибка: {e}")

                finally:
                    await self._cancel_tasks()

                    if not self.running and not self.reconnect_required:
                        logger.info(
                            "🛑 WebSocket-клиент завершает работу — "
                            "переподключение не требуется"
                        )
                        break

                    logger.info(
                        f"🔄 Переподключение через"
                        f"{SOCKET_RECONNECT_DELAY} секунд..."
                    )
                    await asyncio.sleep(SOCKET_RECONNECT_DELAY)

        except asyncio.CancelledError:
            logger.info("🛑 connect() прерван через CancelledError")
            raise

    async def keep_alive_ws_connection(self):
        """Поддерживает WebSocket-соединение активным"""
        while self.running:
            try:
                response = await self.send_command({"command": "ping"})
                if response.get("error") == "Timeout":
                    logger.warning(
                        "❌ Ping timeout. Инициируем переподключение."
                    )
                    self.reconnect_required = True
                    self.running = False
                    return

            except Exception as e:
                logger.error(f"❌ Ошибка при отправке пинга: {e}")
            await asyncio.sleep(10)

    async def clean_expired_futures(self, timeout: float = 15) -> None:
        """Удаляет зависшие Future из self.waiters"""
        while self.running:
            now = time.time()
            expired = []

            for request_id, (future, created_at) in list(self.waiters.items()):
                if now - created_at > timeout and not future.done():
                    future.set_exception(
                        asyncio.TimeoutError("⏱ Застрявший Future очищен")
                    )
                    expired.append(request_id)

            for request_id in expired:
                del self.waiters[request_id]
                logger.warning(f"🧹 Удалён зависший Future: {request_id}")

            await asyncio.sleep(10)

    async def authenticate(self) -> bool:
        """Отправляет пинг и ожидает ответа для подтверждения авторизации."""
        try:
            response = await self.send_command({"command": "ping"})

            if response.get("requestId"):
                logger.info(
                    f"🔑 Авторизация успешна: {response.get('requestId')}"
                )

            if response.get("error") == "Timeout":
                raise asyncio.TimeoutError("Timeout")

            self.authenticated = True
            return True

        except asyncio.TimeoutError:
            logger.warning(
                "❌ WebSocket не ответил на ping! Вероятно, ошибка авторизации."
            )
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке авторизации: {e}")
            return False

    async def refresh_token(self):
        """Запрашивает новый токен и перезапускает WebSocket."""
        logger.info("🔄 Запрос нового токена...")
        # Здесь вызываем функцию обновления токена
        self.device_token = await get_device_token(
            self.device_id, self.platform
        )
        logger.info("✅ Новый токен получен. Переподключение...")
        await asyncio.sleep(1)

    async def stream_station_messages(self):
        """Постоянный поток сообщений от станции с защитой от зависания."""
        logger.info("📥 Поток приёма сообщений от станции запущен")

        while self.running:
            if self.websocket.closed:
                logger.warning("❌ WebSocket внезапно закрыт")
                self.reconnect_required = True
                self.running = False
                break

            try:
                # Ждём сообщение от станции, не дольше 30 секунд
                msg = await asyncio.wait_for(
                    self.websocket.receive(),
                    timeout=30
                )

                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    self.queue.append(data)
                    logger.debug("📨 Получено сообщение от станции")

                    # Если это ответ на команду, передаём в Future
                    request_id = data.get("requestId")
                    if request_id and request_id in self.waiters:
                        self.waiters[request_id][0].set_result(data)
                        del self.waiters[request_id]

                elif msg.type == aiohttp.WSMsgType.CLOSING:
                    logger.warning("❌ WebSocket начал закрываться на станции")
                    self.reconnect_required = True
                    self.running = False
                    break

                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("❌ WebSocket закрыт станцией")
                    self.reconnect_required = True
                    self.running = False
                    break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("❌ Ошибка WebSocket-соединения")
                    self.reconnect_required = True
                    self.running = False
                    break

                else:
                    logger.warning(f"⚠️ Неизвестный тип сообщения: {msg.type}")

            except asyncio.TimeoutError:
                logger.warning(
                    "📭 Нет сообщений от станции более 30 секунд "
                    "— считаем соединение зависшим"
                )
                self.reconnect_required = True
                self.running = False
                break

            except Exception as e:
                logger.error(f"❌ Ошибка в stream_station_messages: {e}")
                self._fail_all_pending_futures(e)
                self.reconnect_required = True
                self.running = False
                break

        await self.command_queue.put("stop")
        logger.info("🛑 stream_station_messages завершен")

    async def command_producer_handler(self):
        """Обрабатывает команды из очереди и отправляет их на станцию."""
        logger.info("📤 Поток отправки команд на станцию запущен")
        while self.running:
            try:
                command = await asyncio.wait_for(
                    self.command_queue.get(),
                    timeout=30
                )
            except asyncio.TimeoutError:
                logger.debug("⏱ Очередь пуста, ждём новые команды...")
                continue

            if command == "stop":
                break

            #  Блокировка гарантирует,
            #  что команды отправляются последовательно
            async with self.lock:
                if not self.websocket or self.websocket.closed:
                    logger.warning("❌ WebSocket закрыт, команда удалена")
                    continue  # Пропускаем команду, не отправляя её

                try:
                    await asyncio.wait_for(
                        self.websocket.send_json(command),
                        timeout=5
                    )
                    logger.info(f"✅ Команда отправлена на станцию: {command}")
                except asyncio.TimeoutError:
                    logger.warning(
                        "⚠️ Таймаут при отправке команды через WebSocket"
                    )
                except Exception as e:
                    logger.error(f"❌ Ошибка при отправке команды: {e}")
        logger.info("🛑 command_producer_handler завершен")

    async def send_command(self, command: dict) -> dict:
        """Отправляет команды в очередь для отправки на станцию
        и ожидает именованный uuid ответ от станции на команду.
        """
        if not self.running:
            logger.warning(
                "⚠️ Попытка отправки команды при остановленном клиенте"
            )
            raise ClientNotRunningError(
                "Клиент не активен, отправка команды невозможна"
            )

        request_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        self.waiters[request_id] = (future, time.time())

        command_payload = {
            "conversationToken": self.device_token,
            "id": request_id,
            "payload": command,
            "sentTime": int(round(time.time() * 1000)),
        }

        await self.command_queue.put(command_payload)
        logger.info(f"✅ Команда {request_id} добавлена в очередь")

        try:
            response = await asyncio.wait_for(future, timeout=10)
            logger.info(f"✅ Ответ на команду {request_id} получен")
            return response
        except asyncio.TimeoutError:
            logger.error(
                f"❌ Timeout при ожидании ответа на команду {request_id}"
            )
            return {"error": "Timeout"}
        finally:
            self.waiters.pop(request_id, None)  # Чистим Future после обработки

    async def get_latest_message(self):
        """Возвращает самое последнее сообщение из очереди или None,
        если очередь пуста.
        """
        latest = self.queue[-1] if self.queue else None
        return latest

    async def _cancel_tasks(self):
        """Отмена всех активных задач, чтобы избежать зависших WebSocket."""

        if not self.tasks:
            logger.info("🛑 Нет активных фоновых задач для отмены")
            return

        logger.info("🛑 Отмена всех фоновых задач...")
        tasks_to_cancel = [task for task in self.tasks if not task.done()]

        for task in tasks_to_cancel:
            task.cancel()

        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self.tasks.clear()
        logger.info("✅ Все фоновые задачи успешно отменены")

    async def close(self):
        """Закрытие WebSocket-соединения и фоновых задач."""
        self.running = False

        # Завершаем все зависшие Future
        logger.info("🔄 Завершение всех зависших Future...")
        self._fail_all_pending_futures(RuntimeError("🛑 Клиент закрыт"))
        logger.info("✅ Все зависшие Future завершены")

        # Очищаем очередь команд, чтобы не отправлять их в закрытый WebSocket
        while not self.command_queue.empty():
            try:
                self.command_queue.get_nowait()
                self.command_queue.task_done()
            except asyncio.QueueEmpty:
                break

        # Отмена всех фоновых задач
        await self._cancel_tasks()

        if self.websocket:
            try:
                logger.info("🔄 Закрытие WebSocket-соединения...")
                await self.websocket.close()
                logger.info("✅ WebSocket-соединение закрыто")
            except Exception as e:
                logger.error(f"❌ Ошибка при закрытии WebSocket: {e}")
            finally:
                self.websocket = None

        if self.session:
            try:
                logger.info("🔄 Закрытие HTTP-сессии...")
                await self.session.close()
                logger.info("✅ HTTP-сессия закрыта")
            except Exception as e:
                logger.error(f"❌ Ошибка при закрытии HTTP-сессии: {e}")
            finally:
                self.session = None

        if self._connect_task:
            logger.info("🔄 Отмена задачи подключения к станции...")
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                logger.info("✅ Задача подключения к станции отменена")
            except Exception as e:
                logger.error(f"❌ Ошибка при остановке задачи подключения: {e}")
            self._connect_task = None
            logger.info("✅ Задача подключения к станции отменена")

    def _fail_all_pending_futures(self, error: Exception):
        """Завершает все зависшие Future"""
        count = 0
        for request_id, (future, _) in list(self.waiters.items()):
            if not future.done():
                future.set_exception(error)
                del self.waiters[request_id]
                count += 1
        if count:
            logger.warning(
                f"❌ Завершено {count} зависших Future с ошибкой: {error}"
            )

    def _check_duplicate_tasks(self):
        """Проверка на повторяющиеся задачи"""
        names = [t.get_coro().__name__ for t in self.tasks if not t.done()]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            logger.warning(f"⚠️ Найдены повторяющиеся задачи: {duplicates}")
        else:
            logger.info("✅ Нет повторяющихся задач")
