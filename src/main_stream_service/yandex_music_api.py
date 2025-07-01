import logging


from yandex_music import ClientAsync

logger = logging.getLogger(__name__)


class YandexMusicAPI:
    """Класс для работы с API Яндекс.Музыки"""

    _client: ClientAsync

    def __init__(self, client: ClientAsync):
        self._client = client

    async def get_file_info(
        self,
        track_id: int,
        quality: str = None,
        codecs: str = None,
    ):
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
            info for info in download_info
            if not codecs or info.codec == codecs
        ]

        if quality:
            quality = int(quality)
            logger.info(f"🔍 Ищем ссылку на {quality} kbps")
            for info in candidates:
                if info.bitrate_in_kbps == quality:
                    logger.info(f"✅ Найдена: {info.direct_link}")
                    return info.direct_link

        # Если не указано качество — возвращаем лучшее
        best = max(candidates, key=lambda x: x.bitrate_in_kbps, default=None)
        if best:
            logger.info(
                f"✅ Лучшее качество: {best.bitrate_in_kbps} "
                f"kbps — {best.direct_link}"
            )
            return best.direct_link

        return None
