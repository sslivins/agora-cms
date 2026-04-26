from sqlalchemy import Column, String, Table
from sqlalchemy.dialects.postgresql import UUID

from shared.database import Base

# ── Stub tables for FK resolution ──
# Shared models have ForeignKey columns that reference CMS-only tables (users,
# devices, device_groups).  The full ORM models live in cms.models; these
# bare Table entries only need the PK column so SQLAlchemy can resolve FKs
# in the worker (which never imports CMS models).  `extend_existing=True`
# ensures the CMS's full model definitions extend rather than conflict.
Table("users", Base.metadata, Column("id", UUID(as_uuid=True), primary_key=True), extend_existing=True)
Table("devices", Base.metadata, Column("id", String(64), primary_key=True), extend_existing=True)
Table("device_groups", Base.metadata, Column("id", UUID(as_uuid=True), primary_key=True), extend_existing=True)

from shared.models.asset import Asset, AssetType, AssetVariant, DeviceAsset, VariantStatus  # noqa: F401
from shared.models.device_profile import DeviceProfile  # noqa: F401
from shared.models.group_asset import GroupAsset  # noqa: F401
from shared.models.job import Job, JobStatus, JobType, MAX_JOB_RETRIES  # noqa: F401
from shared.models.slideshow_slide import SlideshowSlide  # noqa: F401
