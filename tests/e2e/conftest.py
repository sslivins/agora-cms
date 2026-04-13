"""Playwright e2e test fixtures for asset scoping.

NOTE: These tests are currently disabled (see #183).
They depend on a live Azure CMS instance which is being decommissioned.
They need to be converted to run against a local server.
"""
import os
import pytest
import requests

CMS_URL = os.environ.get("CMS_URL", "https://agrbac-cms.nicemoss-9002ee38.westus3.azurecontainerapps.io")
ADMIN_USER = "admin"
ADMIN_PASS = os.environ.get("CMS_ADMIN_PASS", "HN2PfmihDkWy5Ugx")


@pytest.fixture(scope="session")
def cms_url():
    return CMS_URL


@pytest.fixture(scope="session")
def admin_session():
    """Create a requests session logged in as admin."""
    s = requests.Session()
    resp = s.post(f"{CMS_URL}/login", data={"email": ADMIN_USER, "password": ADMIN_PASS}, allow_redirects=False)
    assert resp.status_code in (302, 303), f"Admin login failed: {resp.status_code}"
    return s


@pytest.fixture(scope="session")
def roles(admin_session):
    """Fetch all roles and return as {name: id} dict."""
    resp = admin_session.get(f"{CMS_URL}/api/roles")
    assert resp.status_code == 200
    return {r["name"]: r["id"] for r in resp.json()}


@pytest.fixture(scope="session")
def groups(admin_session):
    """Create two test groups: E2E-GroupA and E2E-GroupB. Return {name: id} dict."""
    result = {}
    # First check what groups already exist
    existing = admin_session.get(f"{CMS_URL}/api/devices/groups/").json()
    existing_map = {g["name"]: g["id"] for g in existing}

    for name in ("E2E-GroupA", "E2E-GroupB"):
        if name in existing_map:
            result[name] = existing_map[name]
        else:
            resp = admin_session.post(f"{CMS_URL}/api/devices/groups/", json={"name": name})
            assert resp.status_code == 201, f"Failed to create group {name}: {resp.status_code} {resp.text}"
            result[name] = resp.json()["id"]
    return result


@pytest.fixture(scope="session")
def test_users(admin_session, roles, groups):
    """Create test users:
    - userA: Operator in GroupA only
    - userB: Operator in GroupB only
    - userAB: Operator in GroupA + GroupB
    - userNone: Operator with no groups
    """
    operator_role_id = roles["Operator"]
    users = {}
    specs = {
        "userA": {"email": "e2e-usera@test.local", "groups": [groups["E2E-GroupA"]]},
        "userB": {"email": "e2e-userb@test.local", "groups": [groups["E2E-GroupB"]]},
        "userAB": {"email": "e2e-userab@test.local", "groups": [groups["E2E-GroupA"], groups["E2E-GroupB"]]},
        "userNone": {"email": "e2e-usernone@test.local", "groups": []},
    }
    for name, spec in specs.items():
        password = f"TestPass{name}123!"
        resp = admin_session.post(f"{CMS_URL}/api/users", json={
            "email": spec["email"],
            "display_name": name,
            "password": password,
            "role_id": operator_role_id,
            "group_ids": spec["groups"],
        })
        if resp.status_code == 201:
            uid = resp.json()["id"]
        elif resp.status_code == 409:
            all_users = admin_session.get(f"{CMS_URL}/api/users").json()
            uid = None
            for u in all_users:
                if u["email"] == spec["email"]:
                    uid = u["id"]
                    break
            assert uid, f"User {spec['email']} exists but not found in list"
        else:
            raise AssertionError(f"Failed to create user {name}: {resp.status_code} {resp.text}")

        # Always update to ensure correct groups, password, and clear must_change_password
        admin_session.patch(f"{CMS_URL}/api/users/{uid}", json={
            "group_ids": spec["groups"],
            "password": password,
            "must_change_password": False,
        })
        users[name] = {"id": uid, "email": spec["email"], "password": password}
    return users


def login_playwright(page, cms_url, email, password):
    """Log into CMS via the web login form."""
    page.goto(f"{cms_url}/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{cms_url}/**")
