from __future__ import annotations

import base64
import json

from cascade.nodes import S3Connector, WebDAVConnector


def _row(kind: str, config: dict) -> dict:
    return {
        "id": 1,
        "name": "Test provider",
        "kind": kind,
        "config": json.dumps(config),
        "enabled": 1,
    }


def test_new_providers_appear_in_the_nodes_dialog(panel):
    response = panel.get("/admin/nodes")
    assert response.status_code == 200
    for label in ("S3-compatible", "WebDAV", "Microsoft OneDrive"):
        assert label in response.text
    assert "Adding a node" in response.text
    assert "Required fields are marked" in response.text


def test_s3_requires_bucket_and_credentials(panel):
    response = panel.post(
        "/api/nodes",
        json={"name": "R2", "kind": "s3", "config": {"bucket": "media"}},
    )
    assert response.status_code == 400
    assert "Access key ID is required" in response.text


def test_webdav_requires_password(panel):
    response = panel.post(
        "/api/nodes",
        json={
            "name": "Nextcloud",
            "kind": "webdav",
            "config": {
                "base_url": "https://cloud.example.com/remote.php/dav/files/me",
                "username": "me",
            },
        },
    )
    assert response.status_code == 400
    assert "Password is required" in response.text


def test_onedrive_requires_token(panel):
    response = panel.post(
        "/api/nodes",
        json={"name": "OneDrive", "kind": "onedrive", "config": {}},
    )
    assert response.status_code == 400
    assert "Graph access token is required" in response.text


def test_s3_secrets_are_hidden_after_save(panel):
    response = panel.post(
        "/api/nodes",
        json={
            "name": "R2",
            "kind": "s3",
            "config": {
                "endpoint": "https://account.r2.cloudflarestorage.com",
                "bucket": "media",
                "access_key": "key-id",
                "secret_key": "super-secret",
            },
        },
    )
    assert response.status_code == 200, response.text
    node = response.json()["node"]
    assert node["secrets"]["secret_key"] is True
    assert "secret_key" not in node["config"]
    assert node["capabilities"] == ["direct", "list", "read", "write"]


def test_s3_presigns_path_style_object_urls():
    connector = S3Connector(
        _row(
            "s3",
            {
                "bucket": "media",
                "prefix": "library",
                "access_key": "key-id",
                "secret_key": "secret",
            },
        )
    )
    url = connector._presign("GET", connector._object_key("/folder/movie name.mp4"))
    assert url.startswith(
        "https://s3.us-east-1.amazonaws.com/media/library/folder/movie%20name.mp4?"
    )
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Signature=" in url
    assert connector._object_key("/other/file.bin") == "library/other/file.bin"


def test_webdav_builds_encoded_urls_and_basic_auth():
    connector = WebDAVConnector(
        _row(
            "webdav",
            {
                "base_url": "https://cloud.example.com/remote.php/dav/files/me",
                "username": "me",
                "password": "secret",
            },
        )
    )
    assert connector._url("/folder/my file.txt") == (
        "https://cloud.example.com/remote.php/dav/files/me/folder/my%20file.txt"
    )
    expected = "Basic " + base64.b64encode(b"me:secret").decode("ascii")
    assert connector._auth_headers()["Authorization"] == expected
