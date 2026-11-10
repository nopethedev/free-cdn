from __future__ import annotations

import time

from cascade import db


def _list(panel, node_id, path="/"):
    response = panel.get(f"/api/files?node={node_id}&path={path}")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_upload_list_download_mkdir_rename_delete(panel, make_node):
    node_id = make_node("Source")
    upload = panel.post(
        "/api/files/upload",
        data={"node": str(node_id), "path": "/"},
        files={"files": ("hello.txt", b"hello cascade", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["count"] == 1

    items = _list(panel, node_id)
    assert [item["name"] for item in items] == ["hello.txt"]
    assert items[0]["size"] == len(b"hello cascade")

    raw = panel.get(f"/api/files/raw?node={node_id}&path=/hello.txt")
    assert raw.status_code == 200
    assert raw.content == b"hello cascade"
    assert raw.headers["content-disposition"].startswith("attachment;")

    folder = panel.post("/api/files/mkdir", json={"node": node_id, "path": "/", "name": "media"})
    assert folder.status_code == 200, folder.text
    assert "media" in [item["name"] for item in _list(panel, node_id)]

    rename = panel.post(
        "/api/files/rename",
        json={"node": node_id, "path": "/hello.txt", "new_name": "renamed.txt"},
    )
    assert rename.status_code == 200, rename.text
    assert "renamed.txt" in [item["name"] for item in _list(panel, node_id)]

    delete = panel.post("/api/files/delete", json={"node": node_id, "path": "/renamed.txt"})
    assert delete.status_code == 200, delete.text
    names = [item["name"] for item in _list(panel, node_id)]
    assert "renamed.txt" not in names
    assert names == ["media"]


def test_replication_job(panel, make_node):
    source = make_node("Source")
    target = make_node("Target", config={"root": "replica"})
    panel.post(
        "/api/files/upload",
        data={"node": str(source), "path": "/"},
        files={"files": ("big.bin", b"x" * 4096, "application/octet-stream")},
    )
    response = panel.post(
        "/api/files/replicate",
        json={
            "node": source,
            "path": "/big.bin",
            "targets": [{"node": target, "path": "/copy.bin"}],
        },
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]

    job = None
    for _ in range(80):
        payload = panel.get(f"/api/jobs/{job_id}")
        assert payload.status_code == 200
        job = payload.json()["job"]
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job is not None
    assert job["status"] == "done", job
    assert job["done"] == 4096
    assert any(item["name"] == "copy.bin" for item in _list(panel, target))


def test_replication_rejects_readonly_target(panel, make_node):
    source = make_node("Source")
    dropbox = make_node("Dropbox", kind="dropbox", config={})
    panel.post(
        "/api/files/upload",
        data={"node": str(source), "path": "/"},
        files={"files": ("a.txt", b"a", "text/plain")},
    )
    response = panel.post(
        "/api/files/replicate",
        json={"node": source, "path": "/a.txt", "targets": [{"node": dropbox, "path": "/a.txt"}]},
    )
    assert response.status_code == 400


def test_upload_requires_admin(panel, make_node):
    node_id = make_node()
    created = panel.post(
        "/api/users",
        json={"email": "uploader@example.com", "password": "uploader-pass", "role": "user"},
    )
    assert created.status_code == 200
    panel.post("/logout", follow_redirects=False)
    from cascade.security import login_limiter

    login_limiter.reset()
    login = panel.client.post(
        "/login",
        data={"email": "uploader@example.com", "password": "uploader-pass"},
        follow_redirects=True,
    )
    from tests.conftest import extract_csrf

    csrf = extract_csrf(login.text)
    response = panel.client.post(
        "/api/files/upload",
        data={"node": str(node_id), "path": "/"},
        files={"files": ("nope.txt", b"nope", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403


def test_node_update_keeps_secret(panel):
    created = panel.post(
        "/api/nodes",
        json={
            "name": "Files",
            "kind": "filebrowser",
            "config": {
                "base_url": "https://files.example.com",
                "username": "admin",
                "password": "super-secret",
            },
        },
    )
    assert created.status_code == 200, created.text
    node = created.json()["node"]
    assert node["secrets"]["password"] is True
    assert "password" not in node["config"]

    updated = panel.patch(f"/api/nodes/{node['id']}", json={"name": "Files 2", "config": {}})
    assert updated.status_code == 200
    assert updated.json()["node"]["secrets"]["password"] is True
    assert panel.get("/api/nodes").json()["nodes"][0]["name"] == "Files 2"


def test_local_node_health(panel, make_node):
    node_id = make_node()
    response = panel.post(f"/api/nodes/{node_id}/test")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["status"] == "online"


def test_api_key_creates_link(panel, file_server):
    created = panel.post("/api/keys", json={"name": "CI"})
    assert created.status_code == 200, created.text
    key = created.json()["key"]
    assert key.startswith("csd_")

    response = panel.client.post(
        "/api/v1/links",
        json={"url": f"{file_server}/clip.txt", "mode": "forward"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["urls"]["short"].endswith(payload["id"])

    bad = panel.client.post(
        "/api/v1/links",
        json={"url": f"{file_server}/clip.txt"},
        headers={"Authorization": "Bearer nope"},
    )
    assert bad.status_code == 401


def test_settings_validation_and_persistence(panel):
    original = panel.get("/api/settings").json()["settings"]
    try:
        bad = panel.post("/api/settings", json={"accent": "not-a-colour"})
        assert bad.status_code == 400
        good = panel.post(
            "/api/settings",
            json={"site_name": "Test Router", "accent": "#12ab34", "short_length": 6},
        )
        assert good.status_code == 200, good.text
        values = good.json()["settings"]
        assert values["site_name"] == "Test Router"
        assert values["accent"] == "#12ab34"
        assert values["short_length"] == "6"
    finally:
        panel.post(
            "/api/settings",
            json={
                "site_name": original["site_name"],
                "accent": original["accent"],
                "short_length": original["short_length"],
            },
        )


def test_short_id_uses_configured_length(panel, make_node, file_server):
    original = panel.get("/api/settings").json()["settings"]["short_length"]
    try:
        panel.post("/api/settings", json={"short_length": 6})
        response = panel.post("/api/links", json={"url": f"{file_server}/clip.txt"})
        assert response.status_code == 200
        assert len(response.json()["link"]["id"]) == 6
    finally:
        panel.post("/api/settings", json={"short_length": original})


def test_file_browser_page_bootstrap(panel, make_node):
    make_node("Local")
    response = panel.get("/admin/files")
    assert response.status_code == 200
    assert "initialNode" in response.text
    assert "Drop files here" in response.text


def test_dashboard_jobs_and_events(panel, make_node):
    make_node()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, status, total, done, payload, created_at, updated_at)"
            " VALUES ('job1234', 'replicate', 'running', 100, 50, '{}', 1, 1)"
        )
    response = panel.get("/admin")
    assert response.status_code == 200
    assert "Jobs" in response.text
