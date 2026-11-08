from dataclasses import dataclass

from core.enums import TrackType


@dataclass
class Track:
    """Класс для представления музыкального трека."""

    id: str
    title: str
    type: TrackType
    artist: str
    duration: float
    progress: float
    playing: bool
    # Соседние треки очереди станции (entityInfo) — для предзагрузки
    next_id: str | None = None
    prev_id: str | None = None

    @property
    def is_radio(self) -> bool:
        """Станция играет радио, а не трек."""
        return self.type == TrackType.FM_RADIO
