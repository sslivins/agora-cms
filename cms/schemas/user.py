"""Pydantic schemas for User and Role RBAC endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ── Role schemas ──

class RoleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    permissions: list[str] = Field(default_factory=list)


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    permissions: list[str] | None = None


class RoleRead(RoleBase):
    id: uuid.UUID
    is_builtin: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── User schemas ──

class UserCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    display_name: str = ""
    password: str | None = Field(None, min_length=6)
    role_id: uuid.UUID
    group_ids: list[uuid.UUID] = Field(default_factory=list)


class UserUpdate(BaseModel):
    email: str | None = Field(None, min_length=3, max_length=255)
    display_name: str | None = None
    password: str | None = Field(None, min_length=6)
    role_id: uuid.UUID | None = None
    is_active: bool | None = None
    must_change_password: bool | None = None
    group_ids: list[uuid.UUID] | None = None


class UserRead(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role_id: uuid.UUID
    role: RoleRead | None = None
    is_active: bool
    must_change_password: bool = False
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None
    group_ids: list[uuid.UUID] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=6)


class UserMe(BaseModel):
    """Lightweight profile for the currently authenticated user."""
    id: uuid.UUID
    email: str
    display_name: str
    role: RoleRead
    group_ids: list[uuid.UUID] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}
