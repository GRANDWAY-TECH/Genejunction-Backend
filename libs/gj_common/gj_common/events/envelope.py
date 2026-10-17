import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gj_common.db import uuid7
from gj_common.events.models import OutboxEvent

_SOURCE = "unknown"


def set_event_source(name: str) -> None:
    global _SOURCE
    _SOURCE = name


def topic_for(event_type: str) -> str:
    """`order.paid` -> `gj.order`. One topic per bounded context; the aggregate id is the partition key."""
    return f"gj.{event_type.split('.', 1)[0]}"


def to_jsonable(value: Any) -> Any:
    """JSON-safe conversion that keeps money exact (Decimal -> str, never float)."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump())
    return value


class EventEnvelope(BaseModel):
    id: uuid.UUID
    type: str
    version: int = 1
    occurred_at: datetime
    source: str
    key: str
    tenant_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def emit(
    session: AsyncSession,
    event_type: str,
    *,
    key: Any,
    data: dict[str, Any],
    tenant_id: Any = None,
    version: int = 1,
) -> EventEnvelope:
    """Stage an event in the outbox. It is published only if the surrounding transaction commits."""
    envelope = EventEnvelope(
        id=uuid7(),
        type=event_type,
        version=version,
        occurred_at=datetime.now(timezone.utc),
        source=_SOURCE,
        key=str(key),
        tenant_id=str(tenant_id) if tenant_id else None,
        data=to_jsonable(data),
    )
    session.add(
        OutboxEvent(
            id=envelope.id,
            topic=topic_for(event_type),
            key=envelope.key,
            event_type=event_type,
            payload=envelope.model_dump(mode="json"),
        )
    )
    return envelope
