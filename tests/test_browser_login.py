from __future__ import annotations

import json

from cascade import browser_login, db
from cascade.nodes import NODE_KINDS


def test_all_cloud_drives_expose_a_browser_sign_in_path():
    for kind in ("dropbox", "gdrive", "onedrive"):
        assert NODE_KINDS[kind]["oauth"] is True
    assert NODE_KINDS["terabox"]["browser_login"]["label"] == "Sign in with browser"


def test_cookie_header_keeps_only_provider_session_cookies():
    cookies = [
        {"name": "ndus", "value": "abc", "domain": ".terabox.com"},
        {"name": "BDUSS", "value": "xyz", "domain": "www.terabox.com"},
        {"name": "tracking", "value": "no", "domain": ".terabox.com"},
        {"name": "ndus", "value": "wrong-domain", "domain": ".example.com"},
        {"name": "ndus", "value": "duplicate", "domain": ".terabox.app"},
    ]
    header = browser_login.cookie_header(
        cookies,
        ("terabox.com", "terabox.app"),
        ("ndus", "BDUSS"),
    )
    assert header == "ndus=abc; BDUSS=xyz"


def test_browser_login_saves_a_cookie_without_returning_or_auditing_it(clean_state):
    admin = db.fetchone("SELECT id FROM users WHERE email = ?", ("admin@example.com",))
    request = {
        "kind": "terabox",
        "user_id": int(admin["id"]),
        "node_id": 0,
        "name": "TeraBox browser",
        "ip": "127.0.0.1",
    }
    node_id, message = browser_login._save_cookie_node(request, "ndus=secret-value")
    row = db.fetchone("SELECT * FROM nodes WHERE id = ?", (node_id,))
    assert json.loads(row["config"])["cookie"] == "ndus=secret-value"
    assert "secret-value" not in message
    audit = db.fetchone(
        "SELECT detail FROM audit WHERE action = 'node.browser_login' ORDER BY id DESC LIMIT 1"
    )
    assert "secret-value" not in audit["detail"]

    request["node_id"] = node_id
    updated, _ = browser_login._save_cookie_node(request, "ndus=fresh-value")
    assert updated == node_id
    row = db.fetchone("SELECT * FROM nodes WHERE id = ?", (node_id,))
    assert json.loads(row["config"])["cookie"] == "ndus=fresh-value"
    assert db.fetchone("SELECT COUNT(*) AS n FROM nodes")["n"] == 1


def test_nodes_page_exposes_browser_sign_in(panel):
    response = panel.get("/admin/nodes")
    assert response.status_code == 200
    assert 'data-add-node-kind="terabox"' in response.text
    assert "Sign in to TeraBox" in response.text


def test_browser_login_start_does_not_return_the_cookie(panel, monkeypatch):
    def fake_start(*args, **kwargs):
        return {
            "id": "test-session",
            "kind": "terabox",
            "label": "TeraBox",
            "state": "waiting",
            "message": "",
            "expires_at": 9999999999,
            "seconds_left": 600,
            "node_id": None,
        }

    monkeypatch.setattr(browser_login, "start", fake_start)
    response = panel.post(
        "/api/nodes/browser/start",
        json={"kind": "terabox", "name": "TeraBox browser"},
    )
    assert response.status_code == 200, response.text
    assert "cookie" not in response.text
    assert response.json()["session"]["id"] == "test-session"
