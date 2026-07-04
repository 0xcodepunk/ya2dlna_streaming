import asyncio
import urllib.parse
from logging import getLogger
from typing import Any, Dict, List, Literal, Optional

import upnpclient

from core.config.settings import settings
from ruark_audio_system.constants import META_INFO
from ruark_audio_system.exceptions import RuarkDeviceNotFoundError
from ruark_audio_system.fsapi_client import RuarkFsApiClient

logger = getLogger(__name__)

PlayModeType = Literal["NORMAL", "SHUFFLE", "REPEAT_ALL"]
SeekUnitType = Literal["REL_TIME", "ABS_TIME", "ABS_COUNT", "TRACK_NR"]


class RuarkR5Controller:
    """Класс для управления устройством Ruark R5.

    UPnP-команды выполняются напрямую, HTTP API fsapi (сессия, питание)
    делегируется RuarkFsApiClient.
    """

    def __init__(self, device_name: str = "Ruark R5") -> None:
        """Сохраняет имя устройства; поиск в сети выполняется в connect()."""
        self.device_name = device_name
        self.device: Optional[upnpclient.Device] = None
        self.ip: Optional[str] = None
        self.services: Dict[str, Any] = {}
        self._av_transport: Any = None
        self._connection_manager: Any = None
        self._rendering_control: Any = None
        self._fsapi = RuarkFsApiClient(pin=settings.ruark_pin)

    @property
    def av_transport(self) -> Any:
        """Сервис AVTransport найденного устройства."""
        return self._require_service(self._av_transport, "AVTransport")

    @property
    def connection_manager(self) -> Any:
        """Сервис ConnectionManager найденного устройства."""
        return self._require_service(
            self._connection_manager, "ConnectionManager"
        )

    @property
    def rendering_control(self) -> Any:
        """Сервис RenderingControl найденного устройства."""
        return self._require_service(
            self._rendering_control, "RenderingControl"
        )

    def _require_service(self, service: Any, name: str) -> Any:
        """Возвращает сервис или бросает ошибку, если устройство не найдено.

        Raises:
            RuarkDeviceNotFoundError: Если устройство ещё не найдено в сети.
        """
        if service is None:
            raise RuarkDeviceNotFoundError(
                f"Устройство '{self.device_name}' не найдено в сети — "
                f"сервис {name} недоступен, вызовите connect()"
            )
        return service

    async def connect(self, attempts: int = 3, delay: float = 2.0) -> bool:
        """Ищет устройство в сети с несколькими попытками.

        Args:
            attempts (int): Количество попыток поиска.
            delay (float): Пауза между попытками в секундах.
        Returns:
            bool: True, если устройство найдено.
        """
        if self.device:
            return True

        for attempt in range(1, attempts + 1):
            logger.info(
                f"🔍 Поиск {self.device_name}: попытка {attempt}/{attempts}"
            )
            await asyncio.to_thread(self.refresh_device)
            if self.device:
                self.print_available_services()
                return True
            if attempt < attempts:
                await asyncio.sleep(delay)

        logger.error(
            f"❌ {self.device_name} не найден после {attempts} попыток"
        )
        return False

    def refresh_device(self) -> None:
        """Обновление устройства."""
        logger.info("🔄 Обновление устройства")
        self.device = self.find_device(device_name=self.device_name)
        if not self.device:
            logger.warning(
                f"⚠ Устройство '{self.device_name}' не найдено в сети!"
            )
            return

        self.ip = self.get_device_ip()
        if self.ip:
            self._fsapi.bind(self.ip)
        self.services = {
            service.service_type: service for service in self.device.services
        }
        self._av_transport = self.services.get(
            "urn:schemas-upnp-org:service:AVTransport:1"
        )
        self._connection_manager = self.services.get(
            "urn:schemas-upnp-org:service:ConnectionManager:1"
        )
        self._rendering_control = self.services.get(
            "urn:schemas-upnp-org:service:RenderingControl:1"
        )
        logger.info(
            f"Устройство обновлено: {self.device.friendly_name} "
            f"({self.device.location})"
        )

    def find_device(self, device_name: str) -> Optional[upnpclient.Device]:
        """Находит устройство по имени."""
        logger.info(f"Начинаем поиск устройства: {device_name}")
        try:
            devices = upnpclient.discover()
            logger.info(f"Найдено {len(devices)} устройств")
            logger.info(f"Устройства: {devices}")
            for device in devices:
                try:
                    logger.info(
                        f"Проверяем устройство: {device.friendly_name}"
                    )
                    if device_name in device.friendly_name:
                        logger.info(
                            "Найдено подходящее устройство: "
                            f"{device.friendly_name}"
                        )
                        return device
                except Exception as e:
                    logger.error(
                        "Ошибка при обработке устройства "
                        f"{device}: {str(e)}"
                    )
                    continue
            logger.info(f"Не найдено устройств с именем: {device_name}")
            return None
        except Exception as e:
            logger.error(f"Error during device discovery: {str(e)}")
            return None

    def get_device_ip(self) -> Optional[str]:
        """Получает IP-адрес устройства."""
        if self.device:
            parsed_url = urllib.parse.urlparse(self.device.location)
            return parsed_url.hostname
        return None

    def print_available_services(self):
        """Выводит список всех поддерживаемых сервисов."""
        logger.info("\n📡 Доступные UPnP сервисы:")
        for service in self.services:
            logger.info(f" - {service}")

    #  ConnectionManager
    async def get_protocol_info(self, connection_manager) -> Dict[str, str]:
        """Получение списка поддерживаемых форматов."""
        return await asyncio.to_thread(self.connection_manager.GetProtocolInfo)

    async def get_current_connection_ids(self) -> List[str]:
        """Получение списка активных соединений."""
        return (
            await asyncio.to_thread(
                self.connection_manager.GetCurrentConnectionIDs
            )
        )["ConnectionIDs"]

    async def get_current_connection_info(
        self, connection_id: int
    ) -> Dict[str, Any]:
        """Получение информации о соединении."""
        return await asyncio.to_thread(
            self.connection_manager.GetCurrentConnectionInfo,
            ConnectionID=connection_id,
        )

    #   AVTransport
    async def set_av_transport_uri(self, uri: str) -> None:
        """Установка нового потока."""
        metadata = self.generate_metadata_with_fake_duration(uri)
        await asyncio.to_thread(
            self.av_transport.SetAVTransportURI,
            InstanceID=0,
            CurrentURI=uri,
            CurrentURIMetaData=metadata,
        )
        logger.info(f"🎵 Поток установлен: {uri}")

    async def play(self) -> None:
        """Запуск воспроизведения."""
        await asyncio.to_thread(
            self.av_transport.Play, InstanceID=0, Speed="1"
        )
        logger.info("▶ Воспроизведение запущено")

    async def pause(self) -> None:
        """Приостановка воспроизведения."""
        await asyncio.to_thread(self.av_transport.Pause, InstanceID=0)
        logger.info("⏸ Воспроизведение приостановлено")

    async def stop(self) -> None:
        """Остановка воспроизведения."""
        playing = await self.is_playing()
        if playing:
            await asyncio.to_thread(self.av_transport.Stop, InstanceID=0)
            logger.info("⏹ Воспроизведение остановлено")

    async def next_track(self) -> None:
        """Переключение на следующий трек."""
        await asyncio.to_thread(self.av_transport.Next, InstanceID=0)
        logger.info("⏭ Следующий трек")

    async def previous_track(self) -> None:
        """Переключение на предыдущий трек."""
        await asyncio.to_thread(self.av_transport.Previous, InstanceID=0)
        logger.info("⏮ Предыдущий трек")

    async def seek(self, target: str, unit: SeekUnitType = "REL_TIME") -> None:
        """Перемотка на указанное время (например, '00:01:30')."""
        await asyncio.to_thread(
            self.av_transport.Seek, InstanceID=0, Unit=unit, Target=target
        )
        logger.info(f"⏩ Перемотка на {target}")

    async def get_media_info(self) -> Dict[str, Any]:
        """Получение информации о текущем медиафайле."""
        return await asyncio.to_thread(
            self.av_transport.GetMediaInfo, InstanceID=0
        )

    async def get_position_info(self) -> Dict[str, Any]:
        """Получение информации о текущей позиции воспроизведения."""
        return await asyncio.to_thread(
            self.av_transport.GetPositionInfo, InstanceID=0
        )

    async def get_transport_info(self) -> Dict[str, Any]:
        """Получение информации о состоянии транспорта."""
        return await asyncio.to_thread(
            self.av_transport.GetTransportInfo, InstanceID=0
        )

    async def get_transport_settings(self) -> Dict[str, Any]:
        """Получение настроек воспроизведения."""
        return await asyncio.to_thread(
            self.av_transport.GetTransportSettings, InstanceID=0
        )

    async def is_playing(self, timeout: float = 5.0) -> bool:
        """Проверка, воспроизводится ли что-либо, с защитой по таймауту."""
        try:
            ruark_state = await asyncio.wait_for(
                self.get_transport_info(), timeout=timeout
            )
            return ruark_state.get("CurrentTransportState") == "PLAYING"
        except asyncio.TimeoutError:
            logger.warning("⚠️ Ruark: timeout при get_transport_info()")
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке is_playing: {e}")
            return False

    async def set_play_mode(self, mode: PlayModeType) -> None:
        """Установка режима воспроизведения."""
        await asyncio.to_thread(
            self.av_transport.SetPlayMode, InstanceID=0, NewPlayMode=mode
        )
        logger.info(f"🔄 Установлен режим воспроизведения: {mode}")

    #   RenderingControl
    async def get_volume(self) -> int:
        """Получение текущего уровня громкости."""
        result = await asyncio.to_thread(
            self.rendering_control.GetVolume, InstanceID=0, Channel="Master"
        )
        return result["CurrentVolume"]

    async def set_volume(self, volume: int) -> None:
        """Установка громкости (0-100)."""
        await asyncio.to_thread(
            self.rendering_control.SetVolume,
            InstanceID=0,
            Channel="Master",
            DesiredVolume=volume,
        )
        logger.info(f"🔊 Громкость установлена на {volume}")

    async def get_mute(self) -> bool:
        """Получение состояния mute."""
        result = await asyncio.to_thread(
            self.rendering_control.GetMute, InstanceID=0, Channel="Master"
        )
        return bool(result["CurrentMute"])

    async def set_mute(self, mute: bool) -> None:
        """Отключение/включение звука."""
        await asyncio.to_thread(
            self.rendering_control.SetMute,
            InstanceID=0,
            Channel="Master",
            DesiredMute=int(mute),
        )
        logger.info("🔇 Звук отключен" if mute else "🔊 Звук включен")

    async def fade_out_ruark(
        self,
        start_volume: int,
        min_volume: int = 2,
        step: int = 6,
        delay: float = 0.1,
    ):
        """Плавное уменьшение громкости Ruark в несколько шагов."""
        volume = start_volume - start_volume % 2

        logger.info(
            f"🔉 Плавное снижение громкости Ruark: "
            f"{volume} ➝ {min_volume} шагом {step}"
        )

        try:
            for v in range(volume, min_volume - 1, -step):
                logger.info(f"  ➤ Устанавливаем громкость: {v}")
                await self.set_volume(v)
                await asyncio.sleep(delay)

            logger.info("✅ Плавное снижение громкости Ruark завершено")

        except Exception as e:
            logger.error(f"❌ Ошибка при снижении громкости Ruark: {e}")

    async def list_presets(self) -> str:
        """Получение списка пресетов."""
        result = await asyncio.to_thread(
            self.rendering_control.ListPresets, InstanceID=0
        )
        return result["CurrentPresetNameList"]

    async def select_preset(self, preset_name: str) -> None:
        """Выбор пресета."""
        await asyncio.to_thread(
            self.rendering_control.SelectPreset,
            InstanceID=0,
            PresetName=preset_name,
        )
        logger.info(f"🎛 Выбран пресет: {preset_name}")

    async def get_session_id(self) -> str:
        """Получение session_id через fsapi.

        Raises:
            RuarkApiError: Если ответ fsapi не содержит sessionId.
        """
        return await self._fsapi.create_session()

    async def get_power_status(self) -> str:
        """Получение статуса питания ('1' — включено, '0' — выключено).

        Raises:
            RuarkApiError: Если ответ fsapi не содержит статус питания.
        """
        return await self._fsapi.get_power_status()

    async def turn_power_on(self) -> bool:
        """Включение питания."""
        return await self._fsapi.turn_power_on()

    async def turn_power_off(self) -> bool:
        """Выключение питания."""
        return await self._fsapi.turn_power_off()

    def generate_metadata_with_fake_duration(self, uri: str) -> str:
        """Генерация DIDL-Lite метаданных с длительностью 999999 часов."""
        logger.info(f"🔊 Генерируем метаданные для {uri}")
        return META_INFO.format(url=uri)

    async def print_status(self) -> None:
        """Вывод текущего состояния устройства."""
        logger.info("🎶 Текущее состояние Ruark R5:")
        volume = await self.get_volume()
        mute = await self.get_mute()
        media_info = await self.get_media_info()
        position_info = await self.get_position_info()
        transport_info = await self.get_transport_info()

        logger.info(f"🔊 Громкость: {volume}")
        logger.info(f"🔇 Mute: {mute}")
        logger.info(f"📀 Медиа: {media_info}")
        logger.info(f"⏱ Позиция: {position_info}")
        logger.info(f"🚀 Транспорт: {transport_info}")
