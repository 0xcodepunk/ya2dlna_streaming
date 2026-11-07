import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import aiohttp
from yandex_music import ClientAsync, DownloadInfo
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

# AAC ниже этого битрейта не выбирается: лучший MP3 предпочтительнее
AAC_MIN_BITRATE_KBPS = 256


@dataclass
class TrackSource:
    """Источник потока трека: прямая ссылка и кодек."""

    url: str
    codec: str  # "flac" | "mp3" | "aac"


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
        """Возвращает лучший источник трека с учётом настройки lossless.

        Lossless первым, если включён; затем MP3 запрошенного качества,
        AAC не ниже AAC_MIN_BITRATE_KBPS, лучший MP3; при выключенном
        lossless — разовое lossless-спасение последним.

        Args:
            track_id (str | int): Идентификатор трека.
            quality (str | None): Запрошенный битрейт MP3, kbps.
        Returns:
            TrackSource | None: Ссылка и кодек или None, если недоступно.
        """
        if settings.prefer_lossless:
            lossless_url = await self._get_lossless_url(track_id)
            if lossless_url:
                return TrackSource(url=lossless_url, codec="flac")

        source = self._pick_lossy_source(
            await self._get_download_info(track_id), quality
        )
        if source:
            return source

        # Нет ни MP3, ни AAC от 256 — разово спасаемся lossless
        if not settings.prefer_lossless:
            lossless_url = await self._get_lossless_url(track_id)
            if lossless_url:
                return TrackSource(url=lossless_url, codec="flac")
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

            token = settings.ya_music_token.get_secret_value()
            headers = dict(MUSIC_CLIENT_HEADERS)
            headers["Authorization"] = f"OAuth {token}"

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

    async def _get_download_info(
        self, track_id: str | int
    ) -> list[DownloadInfo]:
        """Запрашивает варианты файла трека с прямыми ссылками."""
        track = await self._client.tracks(track_id)
        if not track:
            return []

        download_info = await track[0].get_download_info_async(
            get_direct_links=True
        )
        logger.info(f"🔍 Получены ссылки для скачивания: {download_info}")
        return download_info or []

    @staticmethod
    def _pick_lossy_source(
        download_info: Sequence[DownloadInfo], quality: str | None
    ) -> TrackSource | None:
        """Выбирает источник с потерями по ступеням качества.

        Ступени: MP3 запрошенного качества, лучший AAC не ниже
        AAC_MIN_BITRATE_KBPS, лучший MP3. Кодек фильтруется на каждой
        ступени: AAC, скопированный в MP3-контейнер, роняет FFmpeg.
        """
        mp3 = [info for info in download_info if info.codec == "mp3"]
        exact_mp3 = [
            info
            for info in mp3
            if quality and info.bitrate_in_kbps == int(quality)
        ]
        aac = [
            info
            for info in download_info
            if info.codec == "aac"
            and info.bitrate_in_kbps >= AAC_MIN_BITRATE_KBPS
        ]
        for tier in (exact_mp3, aac, mp3):
            best = max(tier, key=lambda x: x.bitrate_in_kbps, default=None)
            if best:
                logger.info(
                    f"✅ Выбран источник {best.codec} "
                    f"{best.bitrate_in_kbps} kbps — {best.direct_link}"
                )
                return TrackSource(url=str(best.direct_link), codec=best.codec)
        return None
