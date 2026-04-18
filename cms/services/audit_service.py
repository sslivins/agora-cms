"""Lightweight audit logging helper.

Usage in a router:
    from cms.services.audit_service import audit_log
    await audit_log(db, user=current_user, action="device.reboot",
                    resource_type="device", resource_id=str(device_id),
                    details={"reason": "manual"}, request=request)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from cms.models.audit_log import AuditLog

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession
    from cms.models.user import User


# Keys added by callers for context but not useful in user-facing descriptions
_INTERNAL_DETAIL_KEYS = frozenset({
    "actor_username", "target_username", "target_display_name",
    "owner_username", "owner_id", "on_behalf_of", "auth_method",
    "role_name", "permissions_count",
})


def build_description(action: str, details: dict | None = None) -> str:
    """Build a human-readable description from action + details."""
    d = details or {}

    target = d.get("target_username") or d.get("email") or d.get("target_display_name")
    actor = d.get("actor_username")

    if action == "user.create":
        msg = f"Created user '{target}'" if target else "Created a user"
        email = d.get("email")
        if email and target and email != target:
            msg += f" ({email})"
        return msg

    if action == "user.update":
        label = f"user '{target}'" if target else "a user"
        if d.get("is_active") is False:
            return f"Deactivated {label}"
        if d.get("is_active") is True:
            return f"Activated {label}"
        changes = [k for k in d if k not in _INTERNAL_DETAIL_KEYS and k != "email"]
        if changes:
            return f"Updated {label} ({', '.join(changes)})"
        return f"Updated {label}"

    if action == "user.delete":
        msg = f"Deleted user '{target}'" if target else "Deleted a user"
        email = d.get("email")
        if email and target and email != target:
            msg += f" ({email})"
        return msg

    if action == "role.create":
        name = d.get("name", "unknown")
        count = d.get("permissions_count")
        if count is not None:
            return f"Created role '{name}' with {count} permissions"
        return f"Created role '{name}'"

    if action == "role.update":
        name = d.get("role_name") or d.get("name") or "unknown"
        changes = [k for k in d if k not in _INTERNAL_DETAIL_KEYS and k != "name"]
        if changes:
            return f"Updated role '{name}' ({', '.join(changes)})"
        return f"Updated role '{name}'"

    if action == "role.delete":
        name = d.get("name", "unknown")
        return f"Deleted role '{name}'"

    if action == "api_key.regenerate":
        key_name = d.get("key_name", "unknown")
        return f"Regenerated API key '{key_name}'"

    if action == "api_key.revoke":
        key_name = d.get("key_name", "unknown")
        return f"Revoked API key '{key_name}'"

    # Fallback: titlecase the action
    return action.replace(".", " ").replace("_", " ").title()


def _serialize_value(v: Any) -> Any:
    """JSON-safe representation for diff payloads."""
    if isinstance(v, uuid.UUID):
        return str(v)
    # date / time / datetime all expose isoformat()
    if hasattr(v, "isoformat") and callable(getattr(v, "isoformat")):
        return v.isoformat()
    return v


def compute_diff(
    obj: Any,
    updates: dict,
    *,
    exclude: set[str] | None = None,
) -> dict[str, dict]:
    """Compute a true diff between an ORM/model instance and incoming updates.

    Returns ``{field: {"old": <current>, "new": <incoming>}}`` only for fields
    whose value actually changed.  Call this BEFORE applying the updates.

    Use to render audit-log diffs in the UI.  Pair with a short description
    (a title) and put this in ``details["changes"]``.
    """
    exclude = exclude or set()
    diff: dict[str, dict] = {}
    for field, new_val in updates.items():
        if field in exclude or not hasattr(obj, field):
            continue
        old_val = getattr(obj, field)
        if old_val != new_val:
            diff[field] = {
                "old": _serialize_value(old_val),
                "new": _serialize_value(new_val),
            }
    return diff


async def audit_log(
    db: AsyncSession,
    *,
    user: User | None = None,
    action: str,
    resource_type: str = "",
    resource_id: str | None = None,
    description: str | None = None,
    details: dict | None = None,
    request: Request | None = None,
) -> AuditLog:
    """Insert an audit log row and flush (caller owns the commit).

    When the request is made via the MCP service key, the real user identity
    from X-On-Behalf-Of is recorded in details['on_behalf_of'].

    If ``description`` is supplied it is stored verbatim; otherwise a summary
    is auto-generated from ``action`` + ``details`` via :func:`build_description`.
    """
    ip = None
    if request and request.client:
        ip = request.client.host

    if details is None:
        details = {}

    # Record on-behalf-of identity for service key requests
    if request and hasattr(request.state, "on_behalf_of"):
        details["on_behalf_of"] = request.state.on_behalf_of
        details["auth_method"] = "mcp_service"

    if description is None:
        description = build_description(action, details)

    entry = AuditLog(
        user_id=user.id if user else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details if details else None,
        description=description,
        ip_address=ip,
    )
    db.add(entry)
    await db.flush()
    return entry
