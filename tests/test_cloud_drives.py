from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from cascade import db
from cascade import oauth as oauth_module
from cascade.nodes import DropboxConnector, GDriveConnector, OneDriveConnector


def _row(kind: str, config: dict) -> dict:
    return {
        "id": 1,
        "name": "Cloud test",
        "kind": kind,
        "config": json.dumps(config),
        "enabled": 1,
    }


class FakeResponse:
    def __init__(self, status: int = 200, payload=None, headers=None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = json.dumps(self._payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, calls: list, responses: list[FakeResponse]) -> None:
        self.calls = calls
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _call(self, method: str, url: str, **kwargs):
        body = kwargs.get("content")
        if body is not None and hasattr(body, "__aiter__") and not isinstance(body, bytes):
            async for _chunk in body:
                pass
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0) if self.responses else FakeResponse()

    async def post(self, url, **kwargs):
        return await self._call("POST", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._call("PUT", url, **kwargs)

    async def get(self, url, **kwargs):
        return await self._call("GET", url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._call("PATCH", url, **kwargs)

    async def delete(self, url, **kwargs):
        return await self._call("DELETE", url, **kwargs)


class FakeClientFactory:
    def __init__(self, monkeypatch, responses: list[FakeResponse]) -> None:
        self.calls: list[tuple] = []
        self.responses = responses
        monkeypatch.setattr(
            "cascade.nodes.httpx.AsyncClient",
            lambda *args, **kwargs: FakeClient(self.calls, self.responses),
        )


async def _body(data: bytes):
    yield data


def test_cloud_drives_report_write_capabilities():
    for connector in (
        DropboxConnector(_row("dropbox", {"access_token": "token"})),
        GDriveConnector(_row("gdrive", {"access_token": "token"})),
        OneDriveConnector(_row("onedrive", {"access_token": "token"})),
    ):
        assert "write" in connector.capabilities


def test_dropbox_upload_creates_file(monkeypatch):
    factory = FakeClientFactory(monkeypatch, [FakeResponse(200, {"id": "rev"})])
    connector = DropboxConnector(_row("dropbox", {"access_token": "token"}))

    async def run():
        return await connector.upload("/media/hello.txt", _body(b"hello"), size=5, mime="text/plain")

    assert asyncio.run(run()) == 5
    method, url, kwargs = factory.calls[0]
    assert method == "POST"
    assert url.endswith("/files/upload")
    assert json.loads(kwargs["headers"]["Dropbox-API-Arg"])["path"] == "/media/hello.txt"


def test_google_drive_upload_uses_resumable_session(monkeypatch):
    factory = FakeClientFactory(
        monkeypatch,
        [
            FakeResponse(200, {}, {"location": "https://upload.example/session"}),
            FakeResponse(200, {"id": "file-id"}),
        ],
    )
    connector = GDriveConnector(
        _row("gdrive", {"access_token": "token", "folder_id": "root-folder"})
    )

    async def run():
        return await connector.upload("/folder/new.txt", _body(b"hello"), size=5)

    assert asyncio.run(run()) == 5
    assert factory.calls[0][1].endswith("/upload/drive/v3/files")
    assert factory.calls[0][2]["json"]["parents"] == ["folder"]
    assert factory.calls[1][1] == "https://upload.example/session"


def test_onedrive_upload_creates_file(monkeypatch):
    factory = FakeClientFactory(monkeypatch, [FakeResponse(201, {"id": "item"})])
    connector = OneDriveConnector(_row("onedrive", {"access_token": "token"}))

    async def run():
        return await connector.upload("/hello.txt", _body(b"hello"), size=5)

    assert asyncio.run(run()) == 5
    method, url, kwargs = factory.calls[0]
    assert method == "PUT"
    assert url.endswith("/me/drive/root:/hello.txt:/content")
    assert kwargs["headers"]["Content-Length"] == "5"


def test_dropbox_folder_operations(monkeypatch):
    factory = FakeClientFactory(
        monkeypatch,
        [FakeResponse(200), FakeResponse(200), FakeResponse(200)],
    )
    connector = DropboxConnector(_row("dropbox", {"access_token": "token"}))

    async def run():
        await connector.mkdir("/media")
        await connector.rename("/media", "/library")
        await connector.delete("/library")

    asyncio.run(run())
    assert [call[1].rsplit("/", 1)[-1] for call in factory.calls] == [
        "create_folder_v2",
        "move_v2",
        "delete_v2",
    ]


def test_oauth_refresh_uses_stored_refresh_token(monkeypatch):
    async def fake_refresh(kind, token):
        assert kind == "gdrive"
        assert token == "refresh-token"
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr(oauth_module, "refresh_access_token", fake_refresh)
    connector = GDriveConnector(
        _row(
            "gdrive",
            {
                "access_token": "old-token",
                "refresh_token": "refresh-token",
                "token_expires_at": 1,
            },
        )
    )

    async def run():
        return await connector._ensure_token()

    assert asyncio.run(run()) == "new-token"


def test_oauth_start_and_callback_create_writable_node(panel, monkeypatch):
    monkeypatch.setenv("CASCADE_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("CASCADE_GOOGLE_CLIENT_SECRET", "client-secret")

    start = panel.get("/oauth/gdrive/start", follow_redirects=False)
    assert start.status_code == 303
    assert "accounts.google.com" in start.headers["location"]
    assert "redirect_uri=" in start.headers["location"]

    async def fake_exchange(kind, code, redirect_uri):
        assert kind == "gdrive"
        assert code == "code-123"
        assert redirect_uri.endswith("/oauth/gdrive/callback")
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }

    monkeypatch.setattr("cascade.routers.oauth.exchange_code", fake_exchange)
    user = db.fetchone("SELECT id FROM users WHERE email = ?", ("admin@example.com",))
    state = oauth_module.sign_state(int(user["id"]), "gdrive", "Team Drive")
    callback = panel.get(
        f"/oauth/gdrive/callback?code=code-123&state={quote(state)}",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert "oauth=connected" in callback.headers["location"]

    node = db.fetchone("SELECT * FROM nodes WHERE kind = 'gdrive'")
    assert node is not None
    config = json.loads(node["config"])
    assert config["access_token"] == "access-token"
    assert config["refresh_token"] == "refresh-token"
    assert config["oauth"] is True

    listed = panel.get("/api/nodes").json()["nodes"][0]
    assert listed["secrets"]["refresh_token"] is True
    assert "refresh_token" not in listed["config"]
    assert "write" in listed["capabilities"]
