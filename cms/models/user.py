"""User, Role, and UserGroup ORM models for RBAC."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base

# How long a welcome/invite setup token stays usable after it is issued.
# Invitation tokens cluster between 24 h and 7 d across the industry (GitHub,
# GitLab, Google Workspace invites are all 7 d); 7 d gives a new user time to
# acquire their device while bounding the credential-exposure window of a
# magic-login link sitting in an inbox (see issue #599).
SETUP_TOKEN_TTL = timedelta(days=7)

# How long a self-service password-reset token stays usable after it is issued
# (issue #231). Reset links are far more sensitive than invites — a single
# click sets a new password and grants access to an *existing* account — so the
# window is deliberately short. One hour matches the common reset-token TTL
# across the industry (GitHub, Django's default PasswordResetTokenGenerator
# day-scale notwithstanding, most SaaS reset links sit at 15 min – 1 h).
RESET_TOKEN_TTL = timedelta(hours=1)


def setup_token_is_expired(user: "User", *, now: datetime | None = None) -> bool:
    """Return ``True`` iff ``user``'s setup token exists but is past its TTL.

    Fails closed: a token with no ``setup_token_created_at`` issue timestamp
    (e.g. a legacy row the backfill somehow missed) is treated as expired so a
    timestamp-less token can never grant indefinite access. A user with no
    token at all is *not* "expired" — there is simply nothing to validate.
    """
    if not user.setup_token:
        return False
    if user.setup_token_created_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    issued = user.setup_token_created_at
    # Tolerate naive timestamps from SQLite (CI) by assuming UTC.
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return now > issued + SETUP_TOKEN_TTL


def reset_token_is_expired(user: "User", *, now: datetime | None = None) -> bool:
    """Return ``True`` iff ``user``'s password-reset token exists but is past TTL.

    Same fail-closed contract as :func:`setup_token_is_expired`: a token present
    with no ``reset_token_created_at`` is treated as expired so a timestamp-less
    reset token can never be redeemed. A user with no reset token at all is not
    "expired" — there is simply nothing to validate.
    """
    if not user.reset_token:
        return False
    if user.reset_token_created_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    issued = user.reset_token_created_at
    # Tolerate naive timestamps from SQLite (CI) by assuming UTC.
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return now > issued + RESET_TOKEN_TTL


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    users: Mapped[list["User"]] = relationship(back_populates="role")


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    setup_token: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    # When ``setup_token`` was issued (create-user / resend-invite). Drives the
    # SETUP_TOKEN_TTL expiry check in ``setup_token_is_expired`` so an old
    # invite link can't be used indefinitely (issue #599). Nullable because
    # rows predate the column; the 0052 migration backfills pending invites.
    setup_token_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Self-service password-reset token (issue #231) + its issue timestamp.
    # Distinct from setup_token: that one activates a never-logged-in invite,
    # this one re-credentials an existing account. Burned on successful reset
    # and gated by RESET_TOKEN_TTL via reset_token_is_expired(). Nullable+unique
    # so at most one live reset link exists per user at a time.
    reset_token: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    reset_token_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    role: Mapped[Role] = relationship(back_populates="users")
    groups: Mapped[list["UserGroup"]] = relationship(back_populates="user")
    api_keys: Mapped[list["APIKey"]] = relationship(back_populates="user")


class UserGroup(Base):
    """Junction table: which device groups a user can access."""

    __tablename__ = "user_groups"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True
    )

    user: Mapped[User] = relationship(back_populates="groups")
    group: Mapped["DeviceGroup"] = relationship()
