"""Перечисления потока и станции, общие для API-сервиса и DLNA-сервера."""

from enum import StrEnum


class Codec(StrEnum):
    """Кодек потока трека или радио."""

    MP3 = "mp3"
    FLAC = "flac"
    AAC = "aac"


class TrackType(StrEnum):
    """Тип того, что играет станция."""

    TRACK = "Track"
    FM_RADIO = "FmRadio"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> "TrackType":
        """Неизвестный станции тип становится «прочим», а не ошибкой."""
        return cls.OTHER


class AliceState(StrEnum):
    """Состояние Алисы из сообщений станции."""

    IDLE = "IDLE"
    LISTENING = "LISTENING"
    SPEAKING = "SPEAKING"
    BUSY = "BUSY"
    # Станция присылает между BUSY и IDLE: не молчит и не активна
    NONE = "NONE"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> "AliceState":
        """Неизвестное состояние становится «прочим», а не ошибкой."""
        return cls.OTHER
