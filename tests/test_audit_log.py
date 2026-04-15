"""Tests for audit log: description generation, enriched details, API filters, and UI."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password
from cms.models.audit_log import AuditLog
from cms.models.user import Role, User
from cms.services.audit_service import build_description, audit_log


# ── Helpers ──


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_test_user(db: AsyncSession, username: str = "tester") -> User:
    role_id = await _get_role_id(db, "Operator")
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username.title(),
        password_hash=hash_password("password123"),
        role_id=role_id,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.flush()
    return user


# ═══════════════════════════════════════════════════════════════════════
#  build_description() unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestBuildDescription:
    """Verify human-readable description generation for every action type."""

    def test_user_create_with_target(self):
        desc = build_description("user.create", {
            "target_username": "jsmith",
            "email": "jsmith@example.com",
            "actor_username": "admin",
        })
        assert "Created user 'jsmith'" in desc
        assert "jsmith@example.com" in desc

    def test_user_create_same_email_as_username(self):
        desc = build_description("user.create", {
            "target_username": "jsmith@example.com",
            "email": "jsmith@example.com",
        })
        assert "Created user 'jsmith@example.com'" in desc
        # Should NOT duplicate email when it matches target
        assert desc.count("jsmith@example.com") == 1

    def test_user_create_no_target(self):
        desc = build_description("user.create", {})
        assert desc == "Created a user"

    def test_user_update_with_changes(self):
        desc = build_description("user.update", {
            "target_username": "jsmith",
            "display_name": "John Smith",
        })
        assert "Updated user 'jsmith'" in desc
        assert "display_name" in desc

    def test_user_update_deactivate(self):
        desc = build_description("user.update", {
            "target_username": "jsmith",
            "is_active": False,
        })
        assert "Deactivated user 'jsmith'" in desc

    def test_user_update_activate(self):
        desc = build_description("user.update", {
            "target_username": "jsmith",
            "is_active": True,
        })
        assert "Activated user 'jsmith'" in desc

    def test_user_delete_with_email(self):
        desc = build_description("user.delete", {
            "target_username": "jsmith",
            "email": "john@example.com",
        })
        assert "Deleted user 'jsmith'" in desc
        assert "john@example.com" in desc

    def test_role_create_with_permissions(self):
        desc = build_description("role.create", {
            "name": "Editor",
            "permissions_count": 5,
        })
        assert "Created role 'Editor'" in desc
        assert "5 permissions" in desc

    def test_role_create_no_count(self):
        desc = build_description("role.create", {"name": "Viewer"})
        assert desc == "Created role 'Viewer'"

    def test_role_update_with_changes(self):
        desc = build_description("role.update", {
            "role_name": "Editor",
            "permissions": ["read", "write"],
        })
        assert "Updated role 'Editor'" in desc
        assert "permissions" in desc

    def test_role_delete(self):
        desc = build_description("role.delete", {"name": "OldRole"})
        assert desc == "Deleted role 'OldRole'"

    def test_api_key_regenerate(self):
        desc = build_description("api_key.regenerate", {"key_name": "my-key"})
        assert desc == "Regenerated API key 'my-key'"

    def test_api_key_revoke(self):
        desc = build_description("api_key.revoke", {"key_name": "old-key"})
        assert desc == "Revoked API key 'old-key'"

    def test_api_key_missing_name_fallback(self):
        desc = build_description("api_key.revoke", {})
        assert "unknown" in desc

    def test_unknown_action_fallback(self):
        desc = build_description("device.reboot", {})
        assert desc == "Device Reboot"

    def test_none_details(self):
        desc = build_description("user.create", None)
        assert desc == "Created a user"

    def test_internal_keys_excluded_from_changes(self):
        desc = build_description("user.update", {
            "target_username": "bob",
            "actor_username": "admin",
            "target_display_name": "Bob",
        })
        # Internal keys should NOT appear in the changes list
        assert "actor_username" not in desc
        assert "target_display_name" not in desc


# ═══════════════════════════════════════════════════════════════════════
#  audit_log() integration tests — description stored at write time
# ═══════════════════════════════════════════════════════════════════════


class TestAuditLogWrite:
    """Verify audit_log() stores description and enriched details."""

    @pytest.mark.asyncio
    async def test_description_stored(self, db_session):
        entry = await audit_log(
            db_session,
            action="role.create",
            resource_type="role",
            details={"name": "TestRole", "permissions_count": 3},
        )
        await db_session.commit()
        assert entry.description == "Created role 'TestRole' with 3 permissions"

    @pytest.mark.asyncio
    async def test_description_column_populated(self, db_session):
        await audit_log(
            db_session,
            action="user.delete",
            resource_type="user",
            details={"target_username": "victim", "email": "v@example.com"},
        )
        await db_session.commit()

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.action == "user.delete")
        )
        row = result.scalar_one()
        assert row.description is not None
        assert "victim" in row.description

    @pytest.mark.asyncio
    async def test_user_fk_recorded(self, app, db_session):
        user = await _create_test_user(db_session)
        entry = await audit_log(
            db_session,
            user=user,
            action="device.adopt",
            resource_type="device",
        )
        await db_session.commit()
        assert entry.user_id == user.id

    @pytest.mark.asyncio
    async def test_resource_id_stored(self, db_session):
        rid = str(uuid.uuid4())
        entry = await audit_log(
            db_session,
            action="schedule.create",
            resource_type="schedule",
            resource_id=rid,
        )
        await db_session.commit()
        assert entry.resource_id == rid

    @pytest.mark.asyncio
    async def test_details_stored(self, db_session):
        entry = await audit_log(
            db_session,
            action="role.create",
            resource_type="role",
            details={"name": "X", "actor_username": "admin"},
        )
        await db_session.commit()
        assert entry.details["actor_username"] == "admin"

    @pytest.mark.asyncio
    async def test_empty_details_becomes_none(self, db_session):
        entry = await audit_log(
            db_session,
            action="device.reboot",
            resource_type="device",
        )
        await db_session.commit()
        # Empty dict → stored as None
        assert entry.details is None


# ═══════════════════════════════════════════════════════════════════════
#  API endpoint tests — filters, search, pagination
# ═══════════════════════════════════════════════════════════════════════


class TestAuditAPI:
    """Test audit log REST API endpoints."""

    @pytest_asyncio.fixture
    async def seeded_audit(self, db_session, client):
        """Seed a variety of audit entries for filter testing."""
        user = await _create_test_user(db_session, "apiuser")
        entries = [
            ("role.create", "role", {"name": "Alpha", "actor_username": "admin"}),
            ("role.create", "role", {"name": "Beta", "actor_username": "admin"}),
            ("role.delete", "role", {"name": "Gamma", "actor_username": "admin"}),
            ("user.create", "user", {"target_username": "newguy", "actor_username": "admin"}),
            ("user.update", "user", {"target_username": "newguy", "display_name": "New Guy"}),
        ]
        for action, rtype, details in entries:
            await audit_log(db_session, user=user, action=action,
                            resource_type=rtype, details=details)
        await db_session.commit()
        return user

    @pytest.mark.asyncio
    async def test_list_returns_entries(self, client, seeded_audit):
        resp = await client.get("/api/audit-log")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 5

    @pytest.mark.asyncio
    async def test_filter_by_action(self, client, seeded_audit):
        resp = await client.get("/api/audit-log?action=role.create")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["action"] == "role.create" for e in data)
        assert len(data) >= 2

    @pytest.mark.asyncio
    async def test_filter_by_resource_type(self, client, seeded_audit):
        resp = await client.get("/api/audit-log?resource_type=user")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["resource_type"] == "user" for e in data)

    @pytest.mark.asyncio
    async def test_filter_by_user_id(self, client, seeded_audit):
        uid = str(seeded_audit.id)
        resp = await client.get(f"/api/audit-log?user_id={uid}")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["user_id"] == uid for e in data)

    @pytest.mark.asyncio
    async def test_search_q_description(self, client, seeded_audit):
        resp = await client.get("/api/audit-log?q=Alpha")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert any("Alpha" in e.get("description", "") for e in data)

    @pytest.mark.asyncio
    async def test_search_q_action(self, client, seeded_audit):
        resp = await client.get("/api/audit-log?q=role.delete")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    @pytest.mark.asyncio
    async def test_pagination_limit_offset(self, client, seeded_audit):
        resp = await client.get("/api/audit-log?limit=2&offset=0")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        resp2 = await client.get("/api/audit-log?limit=2&offset=2")
        assert resp2.status_code == 200
        # Should be different entries
        ids1 = {e["id"] for e in resp.json()}
        ids2 = {e["id"] for e in resp2.json()}
        assert ids1.isdisjoint(ids2)

    @pytest.mark.asyncio
    async def test_count_endpoint(self, client, seeded_audit):
        resp = await client.get("/api/audit-log/count")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 5

    @pytest.mark.asyncio
    async def test_count_with_action_filter(self, client, seeded_audit):
        resp = await client.get("/api/audit-log/count?action=role.create")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 2

    @pytest.mark.asyncio
    async def test_description_in_response(self, client, seeded_audit):
        resp = await client.get("/api/audit-log?action=role.create")
        assert resp.status_code == 200
        data = resp.json()
        for entry in data:
            assert entry["description"] is not None
            assert len(entry["description"]) > 0

    @pytest.mark.asyncio
    async def test_unauthed_rejected(self, unauthed_client):
        resp = await unauthed_client.get("/api/audit-log")
        assert resp.status_code in (401, 403, 303)


# ═══════════════════════════════════════════════════════════════════════
#  UI page tests
# ═══════════════════════════════════════════════════════════════════════


class TestAuditUI:
    """Test the audit log HTML page renders correctly."""

    @pytest_asyncio.fixture
    async def seeded_ui(self, db_session, client):
        user = await _create_test_user(db_session, "uiuser")
        for i in range(3):
            await audit_log(
                db_session, user=user,
                action="role.create", resource_type="role",
                details={"name": f"UIRole{i}", "actor_username": "admin", "permissions_count": i + 1},
            )
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_page_renders(self, client, seeded_ui):
        resp = await client.get("/audit")
        assert resp.status_code == 200
        assert "Audit Log" in resp.text

    @pytest.mark.asyncio
    async def test_filter_controls_present(self, client, seeded_ui):
        resp = await client.get("/audit")
        html = resp.text
        assert 'name="q"' in html
        assert 'name="action"' in html
        assert 'name="user_id"' in html
        assert 'name="since"' in html
        assert 'name="until"' in html

    @pytest.mark.asyncio
    async def test_expandable_rows_markup(self, client, seeded_ui):
        resp = await client.get("/audit")
        html = resp.text
        assert "audit-row" in html
        assert "audit-detail" in html
        assert "toggleAuditRow" in html

    @pytest.mark.asyncio
    async def test_description_shown_in_rows(self, client, seeded_ui):
        resp = await client.get("/audit")
        assert "UIRole0" in resp.text

    @pytest.mark.asyncio
    async def test_pagination_controls(self, client, seeded_ui):
        resp = await client.get("/audit")
        html = resp.text
        assert "per_page" in html
        assert "Showing" in html

    @pytest.mark.asyncio
    async def test_search_filters_results(self, client, seeded_ui):
        resp = await client.get("/audit?q=UIRole1")
        assert resp.status_code == 200
        assert "UIRole1" in resp.text

    @pytest.mark.asyncio
    async def test_action_filter(self, client, seeded_ui):
        resp = await client.get("/audit?action=role.create")
        assert resp.status_code == 200
        assert "role.create" in resp.text

    @pytest.mark.asyncio
    async def test_raw_json_toggle_present(self, client, seeded_ui):
        resp = await client.get("/audit")
        assert "Show raw JSON" in resp.text

    @pytest.mark.asyncio
    async def test_detail_labels_present(self, client, seeded_ui):
        resp = await client.get("/audit")
        html = resp.text
        # Friendly labels should appear instead of raw keys
        assert "Performed By" in html or "Role" in html
