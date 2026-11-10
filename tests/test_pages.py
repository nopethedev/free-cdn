from __future__ import annotations

from cascade.security import login_limiter


def test_home_renders_for_anonymous(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Every drive you own" in response.text
    assert "/login" in response.text


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_admin_pages_render(panel):
    for path in (
        "/admin",
        "/admin/files",
        "/admin/links",
        "/admin/nodes",
        "/admin/users",
        "/admin/settings",
        "/admin/activity",
    ):
        response = panel.get(path)
        assert response.status_code == 200, f"{path}: {response.text[:400]}"
        assert "Cascade" in response.text
        assert 'name="csrf-token"' in response.text


def test_admin_requires_login(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_rejects_bad_password(client):
    login_limiter.reset()
    response = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "wrong"},
    )
    assert response.status_code == 400
    assert "Invalid email or password" in response.text


def test_logout_clears_session(panel):
    response = panel.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    response = panel.get("/admin", follow_redirects=False)
    assert response.status_code == 303


def test_user_role_cannot_reach_admin_only_pages(panel):
    created = panel.post(
        "/api/users",
        json={
            "name": "Regular",
            "email": "regular@example.com",
            "password": "regular-password",
            "role": "user",
        },
    )
    assert created.status_code == 200, created.text
    panel.post("/logout", follow_redirects=False)
    login_limiter.reset()
    response = panel.client.post(
        "/login",
        data={"email": "regular@example.com", "password": "regular-password"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    from tests.conftest import extract_csrf

    csrf = extract_csrf(response.text)
    headers = {"X-CSRF-Token": csrf}
    assert panel.client.get("/admin").status_code == 200
    assert panel.client.get("/admin/nodes").status_code == 403
    assert panel.client.get("/api/nodes", headers=headers).status_code == 403
    # A normal user can still create a link.
    assert (
        panel.client.post(
            "/api/links",
            json={"url": "https://example.com/file.mp4"},
            headers=headers,
        ).status_code
        == 200
    )
