# Cascade

**A CDN-shaped file router.** Cascade is not a CDN: it is a single lightweight control plane in front of the drives you already have. Connect local disk, S3-compatible object storage, WebDAV, a FileBrowser/Quantum instance, Dropbox, Google Drive, OneDrive, TeraBox or any plain HTTP host, then hand out short links that either stream through this server or forward straight to the source CDN.

```
visitor ── /s/abc123 ──► Cascade range proxy ──► node connector / resolved CDN link
visitor ── /f/abc123 ──► 302 ─────────────────► direct CDN URL
admin   ── /admin    ──► nodes · files · links · replication · people · settings
```

## Why it exists

- One short id, two delivery modes. `/s/` is a real proxy with byte-range passthrough (video seeking works). `/f/` is a 302 to the resolved direct URL, so the upstream CDN carries the transfer. Every link exposes both; the link's `mode` only decides what the bare `/{id}` does.
- Cloud share pages become direct URLs. Dropbox share links are rewritten to `dl.dropboxusercontent.com`, Google Drive goes through its confirm/uuid flow, TeraBox is handled with a page-API resolver plus a yt-dlp fallback, and any unknown host is probed and optionally handed to `yt-dlp`.
- FileBrowser and FileBrowser Quantum are first-class nodes: login, browse, upload, mkdir, rename, delete, download. The Quantum v2 move from `/api/raw` to `/api/resources/download` is handled by trying both routes and remembering which one works.
- S3-compatible storage (AWS S3, Cloudflare R2, Backblaze B2, MinIO) and WebDAV (Nextcloud, ownCloud) are writable nodes. S3 forwarding uses short-lived presigned URLs; WebDAV streams through Cascade.
- Replication is a background job with live byte progress. Copy local → FileBrowser, FileBrowser → local, local → local, and so on.
- Multi-user with a clear split: admins manage nodes/files/settings; regular users browse read-only, download and create their own links.
- Password links, expiry, download caps, landing pages, per-IP rate limits, an audit log, delivery events and API keys.
- SQLite and a single Python process. No Redis, no Node runtime, no build step.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # optional; Cascade reads .env automatically
# leave the admin values blank to create the first admin through /setup

python -m cascade
```

Open <http://localhost:8000>. On a fresh database you get `/setup` and create the first admin; if `CASCADE_ADMIN_EMAIL` and `CASCADE_ADMIN_PASSWORD` are set they are seeded automatically instead. Once any account exists, `/setup` redirects to `/login` because initialization is complete.

If you already created an admin and need to start over in development, stop Cascade and remove `data/cascade.db*`; this deletes accounts and links, but not files under `data/files/`.

For development with reload:

```bash
uvicorn cascade.main:app --reload --port 8000
```

### Docker

```bash
docker compose up -d --build
# The first boot with an empty volume opens /setup. Set
# CASCADE_ADMIN_EMAIL and CASCADE_ADMIN_PASSWORD only to seed the admin instead.
# or
docker build -t cascade .
docker run -d -p 8000:8000 -v cascade-data:/data \
  -e CASCADE_ADMIN_EMAIL=admin@example.com \
  -e CASCADE_ADMIN_PASSWORD=change-me \
  cascade
```

The image runs as UID 10001. Compose uses a named volume so permissions just work; if you prefer a bind mount, `chown -R 10001:10001 ./data` first.

The optional TeraBox browser helper is not installed in the base image, and it needs a real desktop session and a local Chromium install. Compose therefore leaves it off with `CASCADE_BROWSER_LOGIN=0`. The OAuth cloud drives, manual TeraBox cookies and all other node types work normally in Docker. Use the optional helper with normal Python on a desktop if you want the browser flow.

SQLite lives in `data/cascade.db`, local files in `data/files/`, and the signing key in `data/secret.key` (unless `CASCADE_SECRET` is set). Back up the `data/` directory.

## Nodes

A node is one place files can live. Add one under **Nodes → Add node**: choose a provider, give the node a name, and fill every field marked `*`. Local disk needs only a name; optional fields can stay blank and be filled in later from **Edit**. After saving, press **Test** on the node card.

| Kind | Browse | Stream | Upload | Notes |
| --- | --- | --- | --- | --- |
| Local disk | yes | yes | yes | A folder inside `data/files/`. Range requests are native. |
| FileBrowser / Quantum | yes | yes | yes | REST API. Needs a user with browse + create permissions. |
| S3-compatible | yes | yes | yes | AWS S3, R2, B2, MinIO. Forward links use 1-hour presigned URLs. |
| WebDAV | yes | yes | yes | Nextcloud, ownCloud or any WebDAV folder. |
| Dropbox | yes | yes | yes | OAuth sign-in or a manual access token. Share links work without a token via the dl. rewrite. |
| Google Drive | yes | yes | yes | OAuth sign-in or a manual access token. Public share links resolve without a token. |
| Microsoft OneDrive | yes | yes | yes | OAuth sign-in or a manual Graph token. |
| TeraBox | — | — | — | Resolver is best-effort. Browser sign-in captures the private-share cookie. |
| HTTP / direct | — | yes | — | Any host with plain file URLs. Optional extra headers. |

### FileBrowser / FileBrowser Quantum

Point the node at the instance root, e.g. `https://files.example.com`, and give the username/password of a FileBrowser user. Cascade calls `POST /api/login`, then uses `/api/resources` for browsing, upload and mkdir, `/api/raw` or `/api/resources/download` for downloads, `PATCH /api/resources/...` with `action: rename` for moves, and `DELETE` for deletion. Quantum's v2 API is detected automatically; if downloads fail, make sure the account has download permission for the configured source.

### S3-compatible object storage

Use the provider's API endpoint, region, bucket and an access key/secret pair. For AWS leave the endpoint blank and choose **Virtual-hosted** addressing. For Cloudflare R2 use `https://<account>.r2.cloudflarestorage.com`, `auto` as the region and **Path-style**; MinIO and Backblaze B2 use their own endpoint and path-style addressing. An optional prefix scopes the node to a folder inside the bucket.

Cascade signs its own S3 requests, so no extra SDK is required. Browsing uses `ListObjectsV2`; uploads, folders, deletes and renames use the object API. Forward links (`/f/`) receive a presigned URL valid for one hour.

### WebDAV

Point the node at the folder URL your WebDAV client uses and provide a username and password. For Nextcloud that is normally `https://cloud.example.com/remote.php/dav/files/<user>`. Cascade uses `PROPFIND` for listings and `GET`/`PUT`/`MKCOL`/`DELETE`/`MOVE` for file operations. WebDAV nodes do not have a public URL, so `/f/` falls back to `/s/`.

### Cloud-drive sign-in (OAuth)

The Nodes page has **Connect Dropbox**, **Connect Google Drive** and **Connect Microsoft OneDrive**. Create an OAuth app with each provider, then set the client credentials and public callback origin in `.env`:

```bash
CASCADE_PUBLIC_BASE_URL=https://cascade.example.com
CASCADE_DROPBOX_CLIENT_ID=...
CASCADE_DROPBOX_CLIENT_SECRET=...
CASCADE_GOOGLE_CLIENT_ID=...
CASCADE_GOOGLE_CLIENT_SECRET=...
CASCADE_MICROSOFT_CLIENT_ID=...
CASCADE_MICROSOFT_CLIENT_SECRET=...
```

Register these exact callback URLs in the provider apps:

- Dropbox: `https://cascade.example.com/oauth/dropbox/callback`
- Google Drive: `https://cascade.example.com/oauth/gdrive/callback`
- OneDrive: `https://cascade.example.com/oauth/onedrive/callback`

Clicking a Connect button signs in, creates a node, and stores the access and refresh tokens in the local SQLite database. Cascade refreshes expired access tokens automatically, so you do not paste tokens or cookies for cloud drives. Client secrets stay in the environment and are never stored in the database. If the provider does not return a refresh token, reconnect when the access token expires.

Manual access-token fields remain in the Add node dialog as a fallback for provider-specific setups.

### Dropbox

Use **Connect Dropbox** for the normal OAuth flow. The app needs `account_info.read`, `files.metadata.read`, `files.metadata.write`, `files.content.read` and `files.content.write` scopes; Cascade requests them during sign-in. You can browse, stream, upload, create folders, rename and delete. Share links also work without a token by pasting `dropbox.com/s/...` or `dropbox.com/scl/fi/...`; Cascade rewrites them to a direct `dl.dropboxusercontent.com` URL.

### Google Drive

Use **Connect Google Drive** for OAuth; Cascade requests the full `drive` scope so it can browse, upload and organize files in the selected account. Folder entries are addressed by file id, so they survive renames. Public "anyone with the link" files still work without a token through the confirm/uuid resolution path.

### Microsoft OneDrive

Use **Connect Microsoft OneDrive** for OAuth; Cascade requests `Files.ReadWrite.All` plus `offline_access`. It lists `/me/drive/root/children` by default and addresses folders by item ID. You can browse, stream, upload, create folders, rename and delete, and `/f/` still returns a temporary download URL when available.

### Optional browser sign-in for TeraBox

Dropbox, Google Drive and OneDrive use browser OAuth because their APIs require Bearer tokens. TeraBox has no equivalent OAuth API for private shares, so Cascade can optionally use a short-lived local Chromium session to capture the session cookie. **A normal web page cannot read another origin's HttpOnly cookies**, so this runs on the machine hosting Cascade, not inside the visitor's browser.

**The base `requirements.txt` does not install Playwright or Chromium. Manual cookies always work without it.** Add a TeraBox node and paste the `ndus=...` cookie from a signed-in browser into the **Browser cookie** field. That is the default path and needs no extra downloads.

To enable the optional browser capture instead:

```bash
pip install -r requirements-browser.txt
python -m playwright install chromium
python -m cascade
```

Then add TeraBox from **Nodes → Add node** and press **Sign in with browser**. Cascade opens Chromium, waits up to 10 minutes while you sign in normally, saves only the TeraBox session cookies (`ndus` / `BDUSS`) into the node config, and closes the window. The cookie never comes back to the admin page, is not logged, and the temporary browser profile is discarded. When the helper is not installed, the browser button is hidden and the manual cookie field remains.

On Linux, if Chromium reports missing shared libraries, install its system dependencies with `python -m playwright install --with-deps chromium` (this may ask for sudo). The browser flow is an optional add-on to the ordinary Python app, not a Docker-only feature.

It needs a desktop session on the machine running Cascade. On a headless server, use the manual cookie field or run Cascade on a desktop and expose it through a tunnel. Set `CASCADE_BROWSER_LOGIN=0` to hide the optional browser option, or change `CASCADE_BROWSER_LOGIN_TIMEOUT` (seconds, default `600`). The Add-node dialog also has a **Sign in with browser** button for each OAuth cloud drive; those buttons open the provider's OAuth page in a new window.

### TeraBox

Use a share URL (`terabox.com/s/...`). Cascade first tries a page scrape (`share/list` with the share ids and cookie), then `yt-dlp` if it is installed, then a configurable direct URL template. Paste the browser cookie (at least `ndus=...`) into the node for private shares; the optional browser capture above can fill that field for you. This resolver is inherently fragile because TeraBox changes its frontend; the template escape hatch lets you supply your own CDN pattern:

```
https://d.terabox.com/file/{id}
```

`{id}` is the share id and `{url}` is the URL-encoded original link. yt-dlp is optional; install it with `pip install yt-dlp` (the Docker image includes it).

### HTTP / direct

Set a base URL such as `https://cdn.example.com/files/` and use node paths like `/movie.mp4`. Absolute URLs in a path are also accepted, which makes this a handy generic forwarder. Extra headers are sent upstream by both `/s/` and the resolver (useful for a required `Referer`).

## Links

Create a link from the file browser (per-row link button), from the Links page (URL or node path), from the dashboard quick form, or through the API.

Every link gets three URLs:

| URL | Behaviour |
| --- | --- |
| `/{id}` | The short link. Redirects to the mode's endpoint, or shows a landing page if enabled/locked. |
| `/s/{id}` | Streams through Cascade, preserving `Range`, `Content-Range` and `Accept-Ranges`. |
| `/f/{id}` | 302 to the resolved direct URL. Local/FileBrowser sources have no public URL, so this falls back to `/s/`. |

Options: password, expiry in hours, max downloads, landing page, per-endpoint enable/disable, note. The bare short link is the only place the default `stream`/`forward` mode matters.

### Resolution cache

Resolved direct URLs are cached in SQLite for 20 minutes (15 minutes for yt-dlp/TeraBox results) so repeated hits do not re-scrape the provider. Passwords and cookies are never stored in the cache key or exposed to the browser.

## API

Create an API key in **Settings → API keys**. The key is shown once. Keys are user-scoped and inherit that user's link quota.

```bash
curl -X POST http://localhost:8000/api/v1/links \
  -H "Authorization: Bearer csd_..." \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.dropbox.com/scl/fi/.../movie.mp4?rlkey=...&dl=0","mode":"forward"}'
```

Response:

```json
{
  "id": "aB3xY9kL",
  "name": "movie.mp4",
  "mode": "forward",
  "urls": {
    "short": "http://localhost:8000/aB3xY9kL",
    "stream": "http://localhost:8000/s/aB3xY9kL",
    "forward": "http://localhost:8000/f/aB3xY9kL"
  },
  "expires_at": null,
  "max_downloads": null
}
```

The same body accepts `node_id` + `path` instead of `url`, plus `password`, `expires_hours`, `max_downloads`, `landing`, `allow_stream`, `allow_forward` and `note`. `GET /healthz` is unauthenticated and returns the version.

## Configuration

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CASCADE_DATA_DIR` | `./data` | SQLite, local files and generated secret live here. |
| `CASCADE_SECRET` | generated | HMAC key for link tokens. Set this for multi-instance or reproducible deployments. |
| `CASCADE_ADMIN_EMAIL` / `CASCADE_ADMIN_PASSWORD` | empty | Seed the first admin when the users table is empty. Leave blank to use `/setup`. |
| `CASCADE_HOST` / `CASCADE_PORT` | `0.0.0.0` / `8000` | Listener used by `python -m cascade`. |
| `CASCADE_TRUST_PROXY` | `0` | Honour `X-Forwarded-For` and uvicorn proxy headers. |
| `CASCADE_COOKIE_SECURE` | `0` | Mark session cookies `Secure` (enable on HTTPS). |
| `CASCADE_BROWSER_LOGIN` | `1` | Enable the optional local Chromium cookie sign-in for TeraBox when the browser helper is installed. Compose defaults it to `0`. |
| `CASCADE_BROWSER_LOGIN_TIMEOUT` | `600` | Seconds before the browser sign-in window closes (120–1800). |
| `CASCADE_YTDLP` | `yt-dlp` | Default binary for the resolver bridge. |

Runtime settings (admin panel): site name, tagline, accent colour, public base URL, default delivery mode, short id length, landing default, no-index, per-link rate limit, yt-dlp on/off and path, public sign-up, max links per user, session lifetime, max upload size.

## Security notes

- If you seed the admin with `CASCADE_ADMIN_EMAIL` / `CASCADE_ADMIN_PASSWORD`, change that password after first login. Set `CASCADE_SECRET` when running more than one instance.
- Serve behind HTTPS and set `CASCADE_COOKIE_SECURE=1`; set `CASCADE_TRUST_PROXY=1` only behind a trusted reverse proxy.
- Sessions are server-side (SQLite) with HttpOnly, SameSite=Lax cookies. Mutating API calls require a per-session CSRF token plus a same-origin check; admin pages are HTML-escaped by Jinja.
- Passwords use PBKDF2-HMAC-SHA256 (240k iterations). Link passwords are hashed and exchanged for a short-lived HMAC token (`?t=...`) so video segment requests do not resend the password.
- Regular users can create URL links; the server will fetch those URLs when the link is played. Only give accounts to people you trust with outbound network access, or keep URL-based links admin-only by policy.
- The audit log records logins, node changes, link changes, user changes and settings changes. Delivery events record IP, user agent and bytes.
- Keep the `data/` directory private: it contains the SQLite database, provider tokens in node configs and the signing key.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite covers range parsing, provider rewrites, the yt-dlp bridge, cache behaviour, login/CSRF, page rendering, stream and forward delivery against a live local HTTP server, password/expiry/limit links, the file browser API, provider configuration and validation, S3 URL signing, WebDAV URL building, replication jobs, API keys and settings validation. Tests use an isolated data directory under `tests/.data`.

## Project layout

```
cascade/
  main.py            app, middleware, exception handlers
  config.py          environment + secret key
  browser_login.py   expiring local Chromium cookie sign-in for TeraBox
  db.py              SQLite schema and queries
  security.py        hashing, sessions, HMAC tokens, rate limits
  deps.py            auth and CSRF checks
  links.py           link creation and URL helpers
  serve.py           link → stream/forward glue
  resolvers.py       Dropbox/Drive/TeraBox/yt-dlp direct URL resolution
  nodes.py           local, FileBrowser, S3, WebDAV, Dropbox, Drive, OneDrive, TeraBox, HTTP
  remote.py          upstream streaming with range passthrough
  jobs.py            replication worker
  routers/           auth, admin pages, JSON API, public delivery
  templates/         Jinja pages
  static/            CSS, JS, favicon
tests/               pytest suite
```

## Known limits

- TeraBox resolution depends on an undocumented frontend API and can break; use the browser cookie capture, cookie/template escape hatch or yt-dlp.
- Browser cookie capture is not a network proxy and cannot be used from a remote visitor's browser. It launches Chromium on the Cascade host; headless servers should use the manual cookie field.
- Cloud-drive OAuth requires you to create a provider app and set its client ID, secret and exact callback URL; Cascade cannot register those apps for you.
- Replication targets must be writable: local disk, FileBrowser, WebDAV, S3-compatible storage, or a connected cloud drive. TeraBox and HTTP are read-only sources.
- Cloud-drive OAuth tokens are refreshed automatically when the provider returns a refresh token. If a provider declines offline access, reconnect when its access token expires.
- Uploads in the admin panel are parsed by Starlette before being streamed to the node, so very large uploads use temporary disk space. Keep the max upload setting sane.
- Forwarding a local, FileBrowser or WebDAV link is impossible without a public URL, so `/f/` falls back to `/s/`. Cloud, S3 and HTTP links forward normally.
- No multi-instance coordination: the job queue and rate limiter are in-process. One Cascade process per SQLite database is the intended deployment.

## License

See [LICENSE](LICENSE).
