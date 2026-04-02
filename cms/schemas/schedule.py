"""Pydantic schemas for schedule API."""

import uuid
from datetime import datetime, time, timezone
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator


class ScheduleCreate(BaseModel):
    name: str
    device_id: Optional[str] = None
    group_id: Optional[uuid.UUID] = None
    asset_id: uuid.UUID
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    start_time: time
    end_time: time
    days_of_week: Optional[list[int]] = None
    priority: int = 0
    enabled: bool = True
    loop_count: Optional[int] = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def ensure_tz_aware(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def check_target(self):
        if not self.device_id and not self.group_id:
            raise ValueError("Either device_id or group_id must be set")
        if self.device_id and self.group_id:
            raise ValueError("Set device_id or group_id, not both")
        return self

    @model_validator(mode="after")
    def check_dates_and_times(self):
        if self.start_time == self.end_time:
            raise ValueError("Start time and end time cannot be the same")
        if self.start_date and self.end_date:
            if self.end_date.date() < self.start_date.date():
                raise ValueError("End date cannot be before start date")
        return self


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    device_id: Optional[str] = None
    group_id: Optional[uuid.UUID] = None
    asset_id: Optional[uuid.UUID] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    days_of_week: Optional[list[int]] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    loop_count: Optional[int] = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def ensure_tz_aware(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def check_dates_and_times(self):
        if self.start_time is not None and self.end_time is not None:
            if self.start_time == self.end_time:
                raise ValueError("Start time and end time cannot be the same")
        if self.start_date is not None and self.end_date is not None:
            if self.end_date.date() < self.start_date.date():
                raise ValueError("End date cannot be before start date")
        return self


class ScheduleOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    device_id: Optional[str] = None
    group_id: Optional[uuid.UUID] = None
    asset_id: uuid.UUID
    asset_filename: Optional[str] = None
    device_name: Optional[str] = None
    group_name: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    start_time: time
    end_time: time
    days_of_week: Optional[list[int]] = None
    priority: int
    enabled: bool
    loop_count: Optional[int] = None
    created_at: datetime
