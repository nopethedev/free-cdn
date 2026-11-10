from __future__ import annotations


def test_setup_redirects_once_admin_exists(client):
    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_signup_disabled_by_default(panel):
    panel.post("/logout", follow_redirects=False)
    response = panel.client.get("/signup", follow_redirects=False)
    assert response.status_code == 403


def test_static_assets_are_served(client):
    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200


def test_robots_and_favicon(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow" in robots.text
    favicon = client.get("/favicon.ico", follow_redirects=False)
    assert favicon.status_code == 307
    assert favicon.headers["location"] == "/static/favicon.svg"


def test_health_payload(client):
    payload = client.get("/healthz").json()
    assert payload["name"] == "cascade"
    assert payload["version"]


def test_unknown_page_returns_html_error(client):
    response = client.get("/doesnotexist", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "not found" in response.text.lower() or "does not exist" in response.text.lower()
