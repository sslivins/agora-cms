## Summary

Removes the last 7 `location.reload()` calls on the Users & Roles admin page (`/users`) as part of the issue #87 reload hunt. The page now uses the same server-rendered fragment + cross-session poller pattern we already applied to assets, profiles, schedules, and groups.

## What changed

**Fragment endpoints (new):**
- `GET /api/users/{id}/row` — returns the rendered `<tr>` for a single user.
- `GET /api/roles/{id}/card` — returns the rendered `<div class="role-card">` for a single role.

Both scope permissions to `users:read` / `roles:read` and re-use the same macros the full page renders with, so HTML stays byte-identical.

**Macros (`cms/templates/_macros.html`):**
- Extracted the user-table `<tr>` into `user_row(u, current_user_id, can_write)`.
- Extracted the role-list card into `role_card(role, perm_descriptions, can_write_roles)`.
- Both add `data-user-id` / `data-role-id` anchors the poller and action handlers use.

**`users.html`:**
- Replaced inline row / card markup with macro calls.
- Added `data-users-tbody`, `data-roles-list`, `data-users-count`, `data-roles-count` anchors.
- Added two 5s pollers (`pollUsersOnce` / `pollRolesOnce`) that diff `/api/users` and `/api/roles` against the known-id sets and insert/remove via the fragment endpoints.

**`app.js`:**
- `createUser`, `updateUser`, `deleteUser`, `toggleUserActive` — no reload. Patch `usersData`, DOM-insert / replace / remove the row via `_insertUserRow`, close the modal, update the count.
- `createRole`, `updateRole`, `deleteRole` — no reload. Patch `rolesData`, DOM-insert / replace / remove the card via `_insertRoleCard`, keep every `<select name="role_id">` in sync so a just-created role is immediately assignable.
- New helpers: `_insertUserRow`, `_insertRoleCard`, `_updateUsersCount`, `_updateRolesCount`, `_resetForm`.

## Why

Before this PR, any admin-side user or role change full-page-reloaded the `/users` tab. That wiped open kebab menus, scroll position, and any other session state. It also meant changes from a second admin session or a second CMS replica weren't visible until the user manually refreshed. Both issues are fixed by the same fragment-endpoint + cross-session-poller pattern established by the earlier reload-hunt slices.

## Tests

**New unit tests:**
- `tests/test_user_row_endpoint.py` — 2 tests: happy-path fragment rendering + 404.
- `tests/test_role_card_endpoint.py` — 3 tests: happy-path, built-in role (no edit/delete), 404.

**New cross-session e2e tests (`tests_e2e/test_ui_cross_session.py`):**
- `TestUsersCrossSession::test_create_propagates` / `test_delete_propagates`.
- `TestRolesCrossSession::test_create_propagates` / `test_delete_propagates`.

Local subset (`tests/test_user_row_endpoint.py`, `tests/test_role_card_endpoint.py`, `tests/test_rbac.py -k "user_create or user_update or user_delete or role"`) all green; relying on CI for the full suite.

## Not in scope

- `dashboard.html` and `devices.html` still have `location.reload()` calls — both are blocked on the in-flight multi-CMS WSS / in-memory transport rewrite.
- `schedules.html` / `app.js` schedules helpers, `profiles.html` fallbacks, and a couple of `app.js` device/asset defensive fallbacks still have reloads — planned follow-up slices.

Closes part of #87.
