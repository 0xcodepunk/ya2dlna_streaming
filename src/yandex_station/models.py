from dataclasses import dataclass


@dataclass
class Track:
    """Класс для представления музыкального трека."""

    id: str
    title: str
    type: str
    artist: str
    duration: float
    progress: float
    playing: bool
    # Соседние треки очереди станции (entityInfo) — для предзагрузки
    next_id: str | None = None
    prev_id: str | None = None
