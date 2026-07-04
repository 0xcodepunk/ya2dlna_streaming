import logging

from yandex_music import ClientAsync

logger = logging.getLogger(__name__)


class YandexMusicAPI:
    """Класс для работы с API Яндекс.Музыки."""

    _client: ClientAsync

    def __init__(self, client: ClientAsync):
        self._client = client

    async def get_file_info(
        self,
        track_id: str | int,
        quality: str | None = None,
        codecs: str | None = None,
    ) -> str | None:
        """Возвращает прямую ссылку на файл трека.

        Args:
            track_id (str | int): Идентификатор трека.
            quality (str | None): Желаемый битрейт в kbps.
            codecs (str | None): Фильтр по кодеку.
        Returns:
            str | None: Прямая ссылка или None, если трек недоступен.
        """
        track = await self._client.tracks(track_id)
        if not track:
            return None

        download_info = await track[0].get_download_info_async(
            get_direct_links=True
        )
        logger.info(f"🔍 Получены ссылки для скачивания: {download_info}")
        if not download_info:
            return None

        candidates = [
            info
            for info in download_info
            if not codecs or info.codec == codecs
        ]

        if quality:
            quality_kbps = int(quality)
            logger.info(f"🔍 Ищем ссылку с качеством: {quality_kbps} kbps")
            for info in candidates:
                if info.bitrate_in_kbps == quality_kbps:
                    logger.info(f"✅ Найдена: {info.direct_link}")
                    return str(info.direct_link)

        # Если не указано качество — возвращаем лучшее
        best = max(candidates, key=lambda x: x.bitrate_in_kbps, default=None)
        if best:
            logger.info(
                f"✅ Лучшее качество: {best.bitrate_in_kbps} "
                f"kbps — {best.direct_link}"
            )
            return str(best.direct_link)

        return None
