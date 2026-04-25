"""Device and DeviceGroup ORM models."""

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class DeviceStatus(str, PyEnum):
    PENDING = "pending"
    ADOPTED = "adopted"
    ORPHANED = "orphaned"


class DeviceGroup(Base):
    __tablename__ = "device_groups"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    default_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    devices: Mapped[list["Device"]] = relationship(back_populates="group")
    default_asset: Mapped["Asset | None"] = relationship(foreign_keys=[default_asset_id])


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        Index(
            "ix_devices_pubkey_unique",
            "pubkey",
            unique=True,
            postgresql_where=text("pubkey IS NOT NULL"),
            sqlite_where=text("pubkey IS NOT NULL"),
        ),
        Index(
            "ix_devices_stale_check",
            "last_seen",
            postgresql_where=text("online = true AND last_seen IS NOT NULL"),
            sqlite_where=text("online = 1 AND last_seen IS NOT NULL"),
        ),
        {"extend_existing": True},
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Pi serial or UUID
    name: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus), default=DeviceStatus.PENDING
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True
    )
    firmware_version: Mapped[str] = mapped_column(String(32), default="")
    storage_capacity_mb: Mapped[int] = mapped_column(Integer, default=0)
    storage_used_mb: Mapped[int] = mapped_column(Integer, default=0)
    device_type: Mapped[str] = mapped_column(String(100), default="")
    supported_codecs: Mapped[str] = mapped_column(String(100), default="")
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_profiles.id"), nullable=True
    )
    default_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True
    )
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_auth_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_api_key_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous_api_key_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bootstrap redesign (issue #420): ed25519 public key, base64-encoded.
    # Populated by /api/devices/adopt when the device is first adopted via
    # the QR flow, or by the Stage C migration endpoint for devices that
    # still have an API key.  NULL during the coexistence window for
    # devices adopted via the legacy WS path that have not migrated yet.
    # When cleared (set to NULL), the device is effectively revoked —
    # signed /connect-token requests will 401 and the device will fall
    # back to bootstrap mode.
    pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # ---- Stage 2c: presence + telemetry (see alembic/versions/0004_*) ----
    # Presence (DB is the sole source of truth across replicas).
    online: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    connection_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Monotonic guard — incoming STATUS writes that are older than this
    # timestamp are dropped so out-of-order deliveries can't rewind state.
    last_status_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Health metrics from the most recent STATUS heartbeat.
    cpu_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    uptime_seconds: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # Playback state.
    mode: Mapped[str] = mapped_column(
        Text, default="unknown", server_default=text("'unknown'"), nullable=False
    )
    asset: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_state: Mapped[str] = mapped_column(
        Text, default="NULL", server_default=text("'NULL'"), nullable=False
    )
    playback_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    playback_position_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Error state — ``error_since`` latches on the first error and only
    # clears when the device reports no error again.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Device-side toggles + hardware presence.
    ssh_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    local_api_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    display_connected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Per-HDMI-port connection state from the most recent STATUS heartbeat.
    # Multi-port boards (Pi 5, Pi 4) report each port independently here;
    # the legacy ``display_connected`` is kept for backward compatibility
    # and tracks ``display_ports[0].connected`` on the device side.
    # Stored as a list of {"name": str, "connected": bool} dicts; ``None``
    # means the device hasn't reported per-port state yet (older firmware
    # or single-port boards).  See issue #350.
    display_ports: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Last-known IP address written by whichever replica processed the
    # most recent register.  None when no registering replica has written
    # yet (or the device is only reachable via WPS, which has no IP).
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Stage 4: atomic upgrade claim (see 0007_alert_state_upgrade_claim) ----
    # Timestamp-as-claim token for the upgrade endpoint.  Set by an
    # atomic CAS update that only succeeds if the column is NULL or
    # older than the configured TTL; cleared by the ``/ws/device``
    # register path on reconnect and by the upgrade endpoint's failure
    # rollback (compare-and-clear against the claimed timestamp).
    upgrade_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    group: Mapped[DeviceGroup | None] = relationship(back_populates="devices")
    profile: Mapped["DeviceProfile | None"] = relationship(back_populates="devices")
    default_asset: Mapped["Asset | None"] = relationship(foreign_keys="[Device.default_asset_id]")
    device_assets: Mapped[list["DeviceAsset"]] = relationship(back_populates="device")
