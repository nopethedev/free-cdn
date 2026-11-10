# Cascade

**A CDN-shaped file router.** Cascade is not a CDN: it is a single lightweight control plane in front of the drives you already have. Connect local disk, a FileBrowser/Quantum instance, Dropbox, Google Drive, TeraBox or any plain HTTP host, then hand out short links that either stream through this server or forward straight to the source CDN.

```
visitor ── /s/abc123 ──► Cascade range proxy ──► node connector / resolved CDN link
visitor ── /f/abc123 ──► 302 ─────────────────► direct CDN URL
admin   ── /admin    ──► nodes · files · links · replication · people · settings
```

## Why it exists

- One short id, two delivery modes. `/s/` is a real proxy with byte-range passthrough (video seeking works). `/f/` is a 302 to the resolved direct URL, so the upstream CDN carries the transfer. Every link exposes both; the link's `mode` only decides what the bare `/{id}` does.
- Cloud share pages become direct URLs. Dropbox share links are rewritten to `dl.dropboxusercontent.com`, Google Drive goes through its confirm/uuid flow, TeraBox is handled with a page-API resolver plus a yt-dlp fallback, and any unknown host is probed and optionally handed to `yt-dlp`.
- FileBrowser and FileBrowser Quantum are first-class nodes: login, browse, upload, mkdir, rename, delete, download. The Quantum v2 move from `/api/raw` to `/api/resources/download` is handled by trying both routes and remembering which one works.
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
# edit CASCADE_ADMIN_EMAIL / CASCADE_ADMIN_PASSWORD

python -m cascade
```

Open <http://localhost:8000>. On a fresh database you get `/setup` and create the first admin; if `CASCADE_ADMIN_EMAIL` and `CASCADE_ADMIN_PASSWORD` are set they are seeded automatically instead.

For development with reload:

```bash
uvicorn cascade.main:app --reload --port 8000
```

### Docker

```bash
docker compose up -d --build
# or
docker build -t cascade .
docker run -d -p 8000:8000 -v cascade-data:/data \
  -e CASCADE_ADMIN_EMAIL=admin@example.com \
  -e CASCADE_ADMIN_PASSWORD=change-me \
  cascade
```

The image runs as UID 10001. Compose uses a named volume so permissions just work; if you prefer a bind mount, `chown -R 10001:10001 ./data` first.

SQLite lives in `data/cascade.db`, local files in `data/files/`, and the signing key in `data/secret.key` (unless `CASCADE_SECRET` is set). Back up the `data/` directory.

## Nodes

A node is one place files can live. Add them under **Nodes → Add node**.

| Kind | Browse | Stream | Upload | Link-only | Notes |
| --- | --- | --- | --- | --- | --- |
| Local disk | yes | yes | yes | — | A folder inside `data/files/`. Range requests are native. |
| FileBrowser / Quantum | yes | yes | yes | — | REST API. Needs a user with browse + create permissions. |
| Dropbox | with token | with token | — | yes | Share links work without a token via the dl. rewrite. |
| Google Drive | with token | with token | — | yes | Public share links resolve without a token. |
| TeraBox | — | — | — | yes | Resolver is best-effort; a cookie helps private shares. |
| HTTP / direct | — | yes | — | yes | Any host with plain file URLs. Optional extra headers. |

### FileBrowser / FileBrowser Quantum

Point the node at the instance root, e.g. `https://files.example.com`, and give the username/password of a FileBrowser user. Cascade calls `POST /api/login`, then uses `/api/resources` for browsing, upload and mkdir, `/api/raw` or `/api/resources/download` for downloads, `PATCH /api/resources/...` with `action: rename` for moves, and `DELETE` for deletion. Quantum's v2 API is detected automatically; if downloads fail, make sure the account has download permission for the configured source.

### Dropbox

Copy an access token from the Dropbox App Console (scopes: `files.metadata.read`, `files.content.read`, `sharing.read` if you want shared links). With a token the node is browsable and streams through the Content API. Without one, paste `dropbox.com/s/...` or `dropbox.com/scl/fi/...` share URLs into a link and Cascade rewrites them to a direct `dl.dropboxusercontent.com` URL.

### Google Drive

Paste an OAuth access token (scope `drive.readonly` is enough) and optionally a root folder id. Folder entries are addressed by file id, so they survive renames. Cascade does not implement an OAuth refresh flow: access tokens expire after about an hour, so paste a fresh one for browsing. Public "anyone with the link" files work without any token through the confirm/uuid resolution path.

### TeraBox

Use a share URL (`terabox.com/s/...`). Cascade first tries a page scrape (`share/list` with the share ids and cookie), then `yt-dlp` if it is installed, then a configurable direct URL template. If a share is private, paste the browser cookie (at least `ndus=...`) into the node. This resolver is inherently fragile because TeraBox changes its frontend; the template escape hatch lets you supply your own CDN pattern:

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
| `CASCADE_ADMIN_EMAIL` / `CASCADE_ADMIN_PASSWORD` | empty | Seed the first admin when the users table is empty. |
| `CASCADE_HOST` / `CASCADE_PORT` | `0.0.0.0` / `8000` | Listener used by `python -m cascade`. |
| `CASCADE_TRUST_PROXY` | `0` | Honour `X-Forwarded-For` and uvicorn proxy headers. |
| `CASCADE_COOKIE_SECURE` | `0` | Mark session cookies `Secure` (enable on HTTPS). |
| `CASCADE_YTDLP` | `yt-dlp` | Default binary for the resolver bridge. |

Runtime settings (admin panel): site name, tagline, accent colour, public base URL, default delivery mode, short id length, landing default, no-index, per-link rate limit, yt-dlp on/off and path, public sign-up, max links per user, session lifetime, max upload size.

## Security notes

- Change the seeded admin password. Set `CASCADE_SECRET` when running more than one instance.
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

The suite covers range parsing, provider rewrites, the yt-dlp bridge, cache behaviour, login/CSRF, page rendering, stream and forward delivery against a live local HTTP server, password/expiry/limit links, the file browser API, replication jobs, API keys and settings validation. Tests use an isolated data directory under `tests/.data`.

## Project layout

```
cascade/
  main.py            app, middleware, exception handlers
  config.py          environment + secret key
  db.py              SQLite schema and queries
  security.py        hashing, sessions, HMAC tokens, rate limits
  deps.py            auth and CSRF checks
  links.py           link creation and URL helpers
  serve.py           link → stream/forward glue
  resolvers.py       Dropbox/Drive/TeraBox/yt-dlp direct URL resolution
  nodes.py           local, FileBrowser, Dropbox, Drive, TeraBox, HTTP connectors
  remote.py          upstream streaming with range passthrough
  jobs.py            replication worker
  routers/           auth, admin pages, JSON API, public delivery
  templates/         Jinja pages
  static/            CSS, JS, favicon
tests/               pytest suite
```

## Known limits

- TeraBox resolution depends on an undocumented frontend API and can break; use the cookie/template escape hatch or yt-dlp.
- Google Drive browsing requires a manually supplied OAuth access token (no refresh flow).
- Cloud nodes are read-only for uploads; replication targets must be local disk or FileBrowser.
- Uploads in the admin panel are parsed by Starlette before being streamed to the node, so very large uploads use temporary disk space. Keep the max upload setting sane.
- Forwarding a local or FileBrowser link is impossible without a public URL, so `/f/` falls back to `/s/`. Cloud and HTTP links forward normally.
- No multi-instance coordination: the job queue and rate limiter are in-process. One Cascade process per SQLite database is the intended deployment.

## License

See [LICENSE](LICENSE).
