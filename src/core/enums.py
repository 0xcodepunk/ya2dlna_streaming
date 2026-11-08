"""Перечисления, общие для API-сервиса и DLNA-сервера."""

from enum import StrEnum


class Codec(StrEnum):
    """Кодек потока трека или радио."""

    MP3 = "mp3"
    FLAC = "flac"
    AAC = "aac"
