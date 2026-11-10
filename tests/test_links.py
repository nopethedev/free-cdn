from __future__ import annotations

from cascade import db
from cascade.utils import now


def _create_local_link(panel, node_id, **overrides):
    payload = {
        "node_id": node_id,
        "path": "/movie.bin",
        "name": "movie.bin",
        "size": 1024,
        "mode": "stream",
    }
    payload.update(overrides)
    response = panel.post("/api/links", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["link"]


def test_stream_and_range(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id)
    link_id = link["id"]

    response = panel.get(f"/s/{link_id}")
    assert response.status_code == 200
    assert response.content == local_file
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "application/octet-stream"

    response = panel.get(f"/s/{link_id}", headers={"Range": "bytes=10-19"})
    assert response.status_code == 206
    assert response.content == local_file[10:20]
    assert response.headers["content-range"] == f"bytes 10-19/{len(local_file)}"

    response = panel.get(f"/s/{link_id}", headers={"Range": "bytes=99999-"})
    assert response.status_code == 416


def test_short_link_and_forward_fallback(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id)
    link_id = link["id"]

    assert link["urls"]["short"].endswith(f"/{link_id}")
    assert link["urls"]["stream"].endswith(f"/s/{link_id}")
    assert link["urls"]["forward"].endswith(f"/f/{link_id}")

    response = panel.get(f"/{link_id}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/s/{link_id}"

    # A local file cannot be forwarded to a public URL, so /f/ hands off to /s/.
    response = panel.get(f"/f/{link_id}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/s/{link_id}"

    listing = panel.get(f"/api/links?{''}").json()
    assert listing["links"][0]["id"] == link_id
    detail = panel.get(f"/admin/links/{link_id}")
    assert detail.status_code == 200


def test_forwarded_host_is_used_for_generated_links(panel, make_node, monkeypatch):
    from cascade.config import settings

    node_id = make_node()
    monkeypatch.setattr(settings, "trust_proxy", True)
    response = panel.post(
        "/api/links",
        json={"node_id": node_id, "path": "/movie.bin", "name": "movie.bin", "mode": "stream"},
        headers={"x-forwarded-host": "files.example.com", "x-forwarded-proto": "https"},
    )
    assert response.status_code == 200, response.text
    urls = response.json()["link"]["urls"]
    assert urls["short"] == f"https://files.example.com/{response.json()['link']['id']}"
    assert urls["stream"].startswith("https://files.example.com/s/")
    assert urls["forward"].startswith("https://files.example.com/f/")


def test_forward_mode_short_link(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id, mode="forward")
    response = panel.get(f"/{link['id']}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/f/{link['id']}"


def test_external_url_stream_and_forward(panel, file_server):
    response = panel.post(
        "/api/links",
        json={"url": f"{file_server}/clip.txt", "name": "clip.txt", "mode": "forward"},
    )
    assert response.status_code == 200, response.text
    link = response.json()["link"]

    response = panel.get(f"/f/{link['id']}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].endswith("/clip.txt")

    response = panel.get(f"/s/{link['id']}")
    assert response.status_code == 200
    assert response.content == b"direct source contents"


def test_link_password_flow(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id, password="hunter2")
    link_id = link["id"]

    locked = panel.get(f"/s/{link_id}")
    assert locked.status_code == 401
    assert "protected" in locked.text.lower()

    wrong = panel.post("/api/public/links/{}/unlock".format(link_id), json={"password": "nope"})
    assert wrong.status_code == 401

    correct = panel.post("/api/public/links/{}/unlock".format(link_id), json={"password": "hunter2"})
    assert correct.status_code == 200
    token = correct.json()["token"]
    assert token

    response = panel.get(f"/s/{link_id}?t={token}")
    assert response.status_code == 200
    assert response.content == local_file

    # Form-based unlock redirects with a signed token.
    form = panel.post(
        f"/u/{link_id}",
        data={"password": "hunter2", "next": f"/s/{link_id}"},
        follow_redirects=False,
    )
    assert form.status_code == 303
    location = form.headers["location"]
    assert location.startswith(f"/s/{link_id}?t=")
    followed = panel.get(location)
    assert followed.status_code == 200

    bad_form = panel.post(
        f"/u/{link_id}",
        data={"password": "wrong", "next": f"/s/{link_id}"},
        follow_redirects=False,
    )
    assert bad_form.status_code == 401


def test_password_landing_buttons_keep_token(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id, password="pw123456", landing=True)
    page = panel.get(f"/{link['id']}", params={"p": "pw123456"})
    assert page.status_code == 200
    assert f"/s/{link['id']}?t=" in page.text
    assert f"/f/{link['id']}?t=" in page.text

    form = panel.post(
        f"/u/{link['id']}",
        data={"password": "pw123456", "next": f"/s/{link['id']}"},
        follow_redirects=False,
    )
    assert form.status_code == 303
    assert form.headers["location"].startswith(f"/s/{link['id']}?t=")


def test_expiry_and_download_limit(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id, max_downloads=1)
    link_id = link["id"]

    first = panel.get(f"/s/{link_id}")
    assert first.status_code == 200
    second = panel.get(f"/s/{link_id}")
    assert second.status_code == 410

    expired = _create_local_link(panel, node_id, name="expired.bin")
    db.execute("UPDATE links SET expires_at = ? WHERE id = ?", (now() - 5, expired["id"]))
    response = panel.get(f"/s/{expired['id']}")
    assert response.status_code == 410


def test_disabled_link_and_toggle(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id)
    response = panel.patch(f"/api/links/{link['id']}", json={"enabled": False})
    assert response.status_code == 200
    assert panel.get(f"/s/{link['id']}").status_code == 410
    response = panel.patch(f"/api/links/{link['id']}", json={"enabled": True})
    assert response.status_code == 200
    assert panel.get(f"/s/{link['id']}").status_code == 200


def test_landing_page(panel, make_node, local_file):
    node_id = make_node()
    link = _create_local_link(panel, node_id, landing=True)
    response = panel.get(f"/{link['id']}")
    assert response.status_code == 200
    assert "movie.bin" in response.text
    assert f"/s/{link['id']}" in response.text


def test_short_link_missing(panel):
    response = panel.get("/doesnotexist1", follow_redirects=False)
    assert response.status_code == 404


def test_csrf_required_for_api_mutations(panel, make_node, local_file):
    node_id = make_node()
    response = panel.client.post(
        "/api/links",
        json={"node_id": node_id, "path": "/movie.bin"},
    )
    assert response.status_code == 403


def test_bad_mode_rejected(panel, make_node):
    node_id = make_node()
    response = panel.post("/api/links", json={"node_id": node_id, "path": "/x.bin", "mode": "bogus"})
    assert response.status_code == 400
    response = panel.post("/api/links", json={"url": "ftp://example.com/x"})
    assert response.status_code == 400
