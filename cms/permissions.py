"""Permission constants for the Agora CMS RBAC system.

Permissions follow the pattern 'resource:action'. A Role is a named set of
permission strings stored as a PostgreSQL TEXT[].
"""

from __future__ import annotations

# ── Device permissions ──
DEVICES_READ = "devices:read"
DEVICES_WRITE = "devices:write"
DEVICES_MANAGE = "devices:manage"

# Legacy — kept for backward compatibility; treated as devices:manage
DEVICES_REBOOT = "devices:reboot"
DEVICES_DELETE = "devices:delete"

# ── Device group permissions ──
GROUPS_READ = "groups:read"
GROUPS_WRITE = "groups:write"

# ── Asset permissions ──
ASSETS_READ = "assets:read"
ASSETS_WRITE = "assets:write"

# ── Schedule permissions ──
SCHEDULES_READ = "schedules:read"
SCHEDULES_WRITE = "schedules:write"

# ── Profile permissions ──
PROFILES_READ = "profiles:read"
PROFILES_WRITE = "profiles:write"

# ── User management permissions ──
USERS_READ = "users:read"
USERS_WRITE = "users:write"

# ── Role management permissions ──
ROLES_READ = "roles:read"
ROLES_WRITE = "roles:write"

# ── Settings permissions ──
SETTINGS_READ = "settings:read"
SETTINGS_WRITE = "settings:write"

# ── API key permissions ──
API_KEYS_READ = "api_keys:read"      # Legacy — kept for backward compat
API_KEYS_WRITE = "api_keys:write"    # Legacy — kept for backward compat
MCP_KEYS_SELF = "mcp_keys:self"      # Create/manage own MCP keys
API_KEYS_SELF = "api_keys:self"      # Create/manage own API keys
API_KEYS_MANAGE = "api_keys:manage"  # Admin: see/revoke/regenerate any user's keys

# ── Log permissions ──
LOGS_READ = "logs:read"

# ── Audit permissions ──
AUDIT_READ = "audit:read"

# ── Notification permissions ──
NOTIFICATIONS_SYSTEM = "notifications:system"

# ── Group scope permissions ──
GROUPS_VIEW_ALL = "groups:view_all"


ALL_PERMISSIONS: list[str] = [
    DEVICES_READ, DEVICES_WRITE, DEVICES_MANAGE,
    GROUPS_READ, GROUPS_WRITE,
    ASSETS_READ, ASSETS_WRITE,
    SCHEDULES_READ, SCHEDULES_WRITE,
    PROFILES_READ, PROFILES_WRITE,
    USERS_READ, USERS_WRITE,
    ROLES_READ, ROLES_WRITE,
    SETTINGS_READ, SETTINGS_WRITE,
    API_KEYS_READ, API_KEYS_WRITE,
    MCP_KEYS_SELF, API_KEYS_SELF, API_KEYS_MANAGE,
    LOGS_READ,
    AUDIT_READ,
    NOTIFICATIONS_SYSTEM,
    GROUPS_VIEW_ALL,
]

PERMISSION_DESCRIPTIONS: dict[str, str] = {
    DEVICES_READ: "View devices and their status",
    DEVICES_WRITE: "Rename devices and assign to groups",
    DEVICES_MANAGE: "Adopt, reboot, delete, update firmware, and configure device settings (SSH, password, timezone, profile, factory reset)",
    GROUPS_READ: "View device groups",
    GROUPS_WRITE: "Create, update, and delete device groups",
    ASSETS_READ: "View and download media assets",
    ASSETS_WRITE: "Upload, update, and delete media assets",
    SCHEDULES_READ: "View playback schedules",
    SCHEDULES_WRITE: "Create, update, and delete schedules",
    PROFILES_READ: "View transcode profiles",
    PROFILES_WRITE: "Create, update, and delete transcode profiles",
    USERS_READ: "View user accounts and details",
    USERS_WRITE: "Create, update, and delete user accounts",
    ROLES_READ: "View roles and their permissions",
    ROLES_WRITE: "Create, update, and delete roles",
    SETTINGS_READ: "View system settings",
    SETTINGS_WRITE: "Modify system settings",
    API_KEYS_READ: "View API keys",
    API_KEYS_WRITE: "Create, revoke, and delete API keys",
    MCP_KEYS_SELF: "Create, revoke, and regenerate your own MCP keys",
    API_KEYS_SELF: "Create, revoke, and regenerate your own API keys",
    API_KEYS_MANAGE: "View and manage all users' API keys",
    LOGS_READ: "View system and device logs",
    AUDIT_READ: "View audit trail and activity history",
    NOTIFICATIONS_SYSTEM: "View system-level notifications (SMTP failures, errors)",
    GROUPS_VIEW_ALL: "View all groups and their resources regardless of group assignment",
}

# ── Predefined role templates ──

ADMIN_PERMISSIONS: list[str] = list(ALL_PERMISSIONS)

OPERATOR_PERMISSIONS: list[str] = [
    DEVICES_READ, DEVICES_WRITE,
    GROUPS_READ,
    ASSETS_READ, ASSETS_WRITE,
    SCHEDULES_READ, SCHEDULES_WRITE,
    PROFILES_READ,
    LOGS_READ,
    MCP_KEYS_SELF,
]

VIEWER_PERMISSIONS: list[str] = [
    DEVICES_READ,
    GROUPS_READ,
    ASSETS_READ,
    SCHEDULES_READ,
    PROFILES_READ,
    LOGS_READ,
]

BUILTIN_ROLES: dict[str, dict] = {
    "Admin": {
        "description": "Full access to all resources and settings across all groups.",
        "permissions": ADMIN_PERMISSIONS,
    },
    "Operator": {
        "description": "Manage devices, assets, and schedules within assigned groups.",
        "permissions": OPERATOR_PERMISSIONS,
    },
    "Viewer": {
        "description": "Read-only access to dashboards and status within assigned groups.",
        "permissions": VIEWER_PERMISSIONS,
    },
}


def has_permission(user_permissions: list[str], required: str) -> bool:
    """Check whether a permission list includes the required permission.

    Backward compatibility: ``devices:reboot`` and ``devices:delete`` are
    treated as aliases for ``devices:manage``.
    """
    if required in user_permissions:
        return True
    # Legacy mapping: if the role still has the old granular permissions,
    # treat them as devices:manage.
    if required == DEVICES_MANAGE:
        return DEVICES_REBOOT in user_permissions or DEVICES_DELETE in user_permissions
    return False
