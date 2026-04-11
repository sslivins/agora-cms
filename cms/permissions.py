"""Permission constants for the Agora CMS RBAC system.

Permissions follow the pattern 'resource:action'. A Role is a named set of
permission strings stored as a PostgreSQL TEXT[].
"""

from __future__ import annotations

# ── Device permissions ──
DEVICES_READ = "devices:read"
DEVICES_WRITE = "devices:write"
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
API_KEYS_READ = "api_keys:read"
API_KEYS_WRITE = "api_keys:write"

# ── Log permissions ──
LOGS_READ = "logs:read"

# ── Audit permissions ──
AUDIT_READ = "audit:read"


ALL_PERMISSIONS: list[str] = [
    DEVICES_READ, DEVICES_WRITE, DEVICES_REBOOT, DEVICES_DELETE,
    GROUPS_READ, GROUPS_WRITE,
    ASSETS_READ, ASSETS_WRITE,
    SCHEDULES_READ, SCHEDULES_WRITE,
    PROFILES_READ, PROFILES_WRITE,
    USERS_READ, USERS_WRITE,
    ROLES_READ, ROLES_WRITE,
    SETTINGS_READ, SETTINGS_WRITE,
    API_KEYS_READ, API_KEYS_WRITE,
    LOGS_READ,
    AUDIT_READ,
]

# ── Predefined role templates ──

ADMIN_PERMISSIONS: list[str] = list(ALL_PERMISSIONS)

OPERATOR_PERMISSIONS: list[str] = [
    DEVICES_READ, DEVICES_WRITE, DEVICES_REBOOT,
    GROUPS_READ,
    ASSETS_READ, ASSETS_WRITE,
    SCHEDULES_READ, SCHEDULES_WRITE,
    PROFILES_READ,
    LOGS_READ,
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
    """Check whether a permission list includes the required permission."""
    return required in user_permissions
