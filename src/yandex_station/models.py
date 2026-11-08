from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from core.enums import AliceState, TrackType

# Ключ контекста разбора: поля, взятые по умолчанию, и их ошибки
_FALLBACKS = "fallbacks"


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


class _StationModel(BaseModel):
    """Часть сообщения станции: ключи в camelCase, мягкий разбор."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        coerce_numbers_to_str=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        """Убирает поля со значением null: для станции это нет данных."""
        if isinstance(data, dict):
            return {
                key: value for key, value in data.items() if value is not None
            }
        return data

    @field_validator("*", mode="wrap")
    @classmethod
    def _default_on_error(
        cls,
        value: Any,
        handler: ValidatorFunctionWrapHandler,
        info: ValidationInfo,
    ) -> Any:
        """Поле непонятного вида получает значение по умолчанию."""
        try:
            return handler(value)
        # Одно плохое поле не бракует всё сообщение: иначе при сбое в
        # каждом сообщении цикл застыл бы на последнем годном
        except ValidationError as e:
            name = info.field_name
            if name is None:
                raise
            if info.context is not None:
                info.context[_FALLBACKS][f"{cls.__name__}.{name}"] = str(e)
            return cls.model_fields[name].get_default(
                call_default_factory=True
            )


class QueueNeighbor(_StationModel):
    """Соседний элемент очереди станции из entityInfo."""

    id: str = ""
    type: TrackType = TrackType.OTHER

    @property
    def track_id(self) -> str | None:
        """Идентификатор соседа, если сосед — трек."""
        return self.id if self.type == TrackType.TRACK and self.id else None


class EntityInfo(_StationModel):
    """Очередь станции вокруг текущего трека — для предзагрузки."""

    next: QueueNeighbor = Field(default_factory=QueueNeighbor)
    prev: QueueNeighbor = Field(default_factory=QueueNeighbor)


class PlayerState(_StationModel):
    """Состояние плеера станции."""

    id: str = "0"
    title: str = ""
    type: TrackType = TrackType.OTHER
    subtitle: str = ""
    duration: float = 0
    progress: float = 0
    entity_info: EntityInfo = Field(default_factory=EntityInfo)


class StationState(_StationModel):
    """Состояние станции: Алиса, громкость, плеер."""

    alice_state: AliceState | None = None
    volume: float | None = None
    playing: bool = False
    player_state: PlayerState = Field(default_factory=PlayerState)

    def to_track(self) -> Track:
        """Собирает текущий трек из состояния плеера."""
        player = self.player_state
        return Track(
            id=player.id,
            title=player.title,
            type=player.type,
            artist=player.subtitle,
            duration=player.duration,
            progress=player.progress,
            playing=self.playing,
            next_id=player.entity_info.next.track_id,
            prev_id=player.entity_info.prev.track_id,
        )


class StationExtra(_StationModel):
    """Служебная часть сообщения: состояние приложения в protobuf."""

    app_state: str | None = None


class StationMessage(_StationModel):
    """Сообщение станции, разобранное один раз при приёме."""

    state: StationState = Field(default_factory=StationState)
    extra: StationExtra = Field(default_factory=StationExtra)

    @classmethod
    def from_station(
        cls, data: JsonValue
    ) -> tuple["StationMessage", Mapping[str, str]]:
        """Разбирает сообщение станции из JSON.

        Args:
            data (JsonValue): Сообщение станции из WebSocket.
        Returns:
            tuple: Сообщение и поля, взятые по умолчанию, с ошибками.
        Raises:
            ValidationError: Когда сообщение не JSON-объект.
        """
        fallbacks: dict[str, str] = {}
        message = cls.model_validate(data, context={_FALLBACKS: fallbacks})
        return message, fallbacks
