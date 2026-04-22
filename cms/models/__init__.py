from sqlalchemy.orm import relationship

from cms.models.api_key import APIKey  # noqa: F401
from cms.models.asset import Asset, AssetType, AssetVariant, DeviceAsset, VariantStatus  # noqa: F401
from cms.models.audit_log import AuditLog  # noqa: F401
from cms.models.device import Device, DeviceGroup, DeviceStatus  # noqa: F401
from cms.models.device_event import DeviceEvent, DeviceEventType  # noqa: F401
from cms.models.device_profile import DeviceProfile  # noqa: F401
from cms.models.group_asset import GroupAsset  # noqa: F401
from cms.models.log_request import LogRequest  # noqa: F401
from cms.models.notification import Notification  # noqa: F401
from cms.models.notification_pref import UserNotificationPref  # noqa: F401
from cms.models.schedule import Schedule  # noqa: F401
from cms.models.schedule_device_skip import ScheduleDeviceSkip  # noqa: F401
from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent  # noqa: F401
from cms.models.setting import CMSSetting  # noqa: F401
from cms.models.user import Role, User, UserGroup  # noqa: F401

# ── CMS-only relationships ──
# These reference models (Schedule, Device) that only exist in the CMS package,
# not in the shared package used by the worker. Adding them here after all models
# are imported ensures SQLAlchemy can resolve the forward references.
Asset.schedules = relationship("Schedule", back_populates="asset")
DeviceAsset.device = relationship("Device", back_populates="device_assets")
DeviceProfile.devices = relationship("Device", back_populates="profile")
GroupAsset.group = relationship("DeviceGroup")
