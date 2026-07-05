import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

import aiohttp
from yandex_music import ClientAsync
from yandex_music.utils.sign_request import DEFAULT_SIGN_KEY

from core.config.settings import settings

logger = logging.getLogger(__name__)

GET_FILE_INFO_URL = "https://api.music.yandex.net/get-file-info"

# Заголовки официального клиента: без них get-file-info отвечает 403
MUSIC_CLIENT_HEADERS = {
    "User-Agent": "Yandex-Music-API",
    "X-Yandex-Music-Client": "YandexMusicAndroid/24023621",
}

LOSSLESS_CODECS = "flac,flac-mp4,mp3,aac,he-aac,aac-mp4,he-aac-mp4"


@dataclass
class TrackSource:
    """Источник потока трека: прямая ссылка и кодек."""

    url: str
    codec: str  # "flac" | "mp3"


class YandexMusicAPI:
    """Класс для работы с API Яндекс.Музыки."""

    _client: ClientAsync

    def __init__(self, client: ClientAsync):
        self._client = client

    async def get_track_source(
        self,
        track_id: str | int,
        quality: str | None = None,
    ) -> TrackSource | None:
        """Возвращает лучший источник трека: FLAC, если доступен, иначе MP3.

        Args:
            track_id (str | int): Идентификатор трека.
            quality (str | None): Битрейт MP3 для фолбэка, kbps.
        Returns:
            TrackSource | None: Ссылка и кодек или None, если недоступно.
        """
        if settings.prefer_lossless:
            lossless_url = await self._get_lossless_url(track_id)
            if lossless_url:
                return TrackSource(url=lossless_url, codec="flac")

        mp3_url = await self.get_file_info(track_id, quality=quality)
        if mp3_url:
            return TrackSource(url=mp3_url, codec="mp3")
        return None

    async def _get_lossless_url(self, track_id: str | int) -> str | None:
        """Запрашивает прямую lossless-ссылку через get-file-info.

        Подписанный запрос к новому API; транспорт raw отдаёт
        нешифрованный поток (codec flac-mp4). Любая ошибка означает
        фолбэк на MP3, поэтому наружу не пробрасывается.
        """
        try:
            params: dict[str, str | int] = {
                "ts": int(time.time()),
                "trackId": str(track_id),
                "quality": "lossless",
                "codecs": LOSSLESS_CODECS,
                "transports": "raw",
            }
            message = "".join(str(v) for v in params.values()).replace(",", "")
            digest = hmac.new(
                DEFAULT_SIGN_KEY.encode(),
                message.encode(),
                hashlib.sha256,
            ).digest()
            params["sign"] = base64.b64encode(digest).decode()[:-1]

            headers = dict(MUSIC_CLIENT_HEADERS)
            headers["Authorization"] = f"OAuth {settings.ya_music_token}"

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    GET_FILE_INFO_URL, params=params, headers=headers
                ) as response:
                    if response.status != 200:
                        logger.info(
                            f"ℹ️ Lossless недоступен для {track_id}: "
                            f"HTTP {response.status}"
                        )
                        return None
                    body = await response.json()

            info = (body.get("result") or {}).get("downloadInfo") or {}
            codec = str(info.get("codec", ""))
            urls = info.get("urls") or []
            if not codec.startswith("flac") or not urls:
                logger.info(
                    f"ℹ️ Lossless для {track_id} не выдан "
                    f"(codec={codec or 'нет'})"
                )
                return None

            logger.info(f"💎 Lossless источник для {track_id}: {codec}")
            return str(urls[0])
        except Exception as e:
            logger.warning(
                f"⚠️ Ошибка запроса lossless для {track_id}: {e} — "
                f"фолбэк на MP3"
            )
            return None

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
