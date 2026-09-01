"""Pydantic schemas for device events."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class DeviceEventOut(BaseModel):
    id: uuid.UUID
    device_id: str | None = None
    device_name: str
    group_id: uuid.UUID | None = None
    group_ids: list[uuid.UUID] = []
    group_snapshots: list[dict] = []
    group_name: str = ""
    event_type: str
    details: dict | None = None
    description: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_event(cls, event) -> "DeviceEventOut":
        """Build from an ORM ``DeviceEvent`` with ``description`` computed.

        ``DeviceEvent`` doesn't carry a ``description`` column, so
        ``model_validate`` with ``from_attributes=True`` would set it
        to the default empty string.  Use this helper from API routes
        so the polled-JSON path stays in sync with the server-rendered
        event_log.html.
        """
        from cms.services.device_event_descriptions import build_event_description
        return cls(
            id=event.id,
            device_id=event.device_id,
            device_name=event.device_name,
            group_id=event.group_id,
            group_ids=list(getattr(event, "group_ids", None) or ([] if event.group_id is None else [event.group_id])),
            group_snapshots=list(
                getattr(event, "group_snapshots", None)
                or (
                    []
                    if event.group_id is None
                    else [{"id": str(event.group_id), "name": event.group_name or ""}]
                )
            ),
            group_name=event.group_name,
            event_type=event.event_type,
            details=event.details,
            description=build_event_description(event.event_type, event.details),
            created_at=event.created_at,
        )
