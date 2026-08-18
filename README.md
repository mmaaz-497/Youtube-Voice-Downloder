# YouTube Audio Extractor (MP3)

A small, self-hosted web app that extracts the audio track from a YouTube video and
delivers it as an MP3. Paste a URL, pick a bitrate, watch the progress, download the file.

The server is a FastAPI application that shells out to **yt-dlp** (download) and
**FFmpeg** (transcode). The frontend is dependency-free HTML/CSS/JS served by the
same process, so there is nothing to build and nothing to deploy separately.

> **Note on use:** this tool is intended for content you own or that is licensed for
> download. Respect YouTube's Terms of Service and applicable copyright law.

---

## How it works

```
Browser (frontend/)                FastAPI (backend/)
  |                                   |
  |-- GET  /api/info?url=...  ------->|  probe metadata (title, duration, thumbnail)
  |<-- title / duration / thumb ------|
  |                                   |
  |-- POST /api/jobs ---------------->|  admission control -> queue -> worker pool
  |<-- job_id, queue_position --------|      |
  |                                   |      +--> yt-dlp   (phase: downloading)
  |-- GET  /api/jobs/{id} (poll) ---->|      +--> FFmpeg   (phase: converting)
  |<-- status / phase / progress -----|
  |                                   |
  |-- GET  /api/jobs/{id}/file ------>|  streams the MP3, then purges job + file
  |<-- audio/mpeg attachment ---------|
```

Work is **asynchronous**: creating a job returns immediately with a `job_id` and a
queue position, and the client polls for status. A bounded worker pool runs the
extractions, a background sweeper reclaims expired jobs and orphaned files, and each
phase has its own watchdog timeout.

### Design points worth knowing

- **Bounded everything.** A global queue limit, a per-origin cap, a disk floor, and
  per-phase timeouts mean the server degrades predictably instead of falling over.
- **Files are transient.** A finished result lives for `TTL_SECONDS` (default 15 min).
  Downloading it purges the job immediately — the file is deleted *after* the response
  finishes streaming, with the orphan sweep as a safety net.
- **Privacy-preserving fairness.** Per-origin limits key off
  `SHA-256(client IP + per-boot salt)`, truncated. The salt rotates every restart, so
  no durable client identifier is ever stored.
- **Honest OpenAPI.** FastAPI's automatic `422` responses are stripped from the schema
  because validation errors are rewritten into the app's own `400 INVALID_INPUT`
  envelope — the published schema matches actual behavior.

---

## Requirements

- **Python 3.10+** (the code uses `X | None` syntax)
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** on `PATH`
- **[FFmpeg](https://ffmpeg.org/)** on `PATH` (must include `ffprobe`)

Verify both are visible to the server:

```bash
yt-dlp --version
ffmpeg -version
```

The `/api/health` endpoint reports whether each tool was found, so you can confirm
this after startup too.

---

## Quick start

```bash
# 1. Clone and enter the project
git clone <your-repo-url>
cd yt-voice-downloder

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. (Optional) configure
cp .env.example .env             # then uncomment what you want to override

# 5. Run
uvicorn backend.main:app --reload
```

Open <http://127.0.0.1:8000> — the frontend is mounted at the root. Interactive API
docs are at <http://127.0.0.1:8000/docs>.

---

## Using it

1. Paste a YouTube URL. The app fetches the title, channel, duration, and thumbnail so
   you can confirm you have the right video before committing to an extraction.
2. Choose a bitrate: **96**, **128**, **192** (default), or **320** kbps.
3. Start the job. You'll see queue position, then phase (`downloading` → `converting`)
   with a progress percentage.
4. Download the MP3 when it completes. The filename is derived from the video title.

Remember that the download link is single-use and the result expires after the TTL.

---

## API reference

All endpoints are under the `/api` prefix. Errors use a consistent envelope:

```json
{ "error": { "code": "INVALID_URL", "message": "Human-readable explanation." } }
```

### `GET /api/info?url=<youtube-url>`

Probes video metadata without starting an extraction.

```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "Video title",
  "channel": "Channel name",
  "duration_seconds": 213,
  "thumbnail_url": "https://i.ytimg.com/...",
  "available": true
}
```

### `POST /api/jobs`

Creates an extraction job. Body:

```json
{ "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "bitrate_kbps": 192 }
```

Returns `202` with:

```json
{ "job_id": "...", "status": "queued", "queue_position": 1 }
```

### `GET /api/jobs/{job_id}`

Polls job state. Fields that don't apply to the current state are omitted.

```json
{
  "job_id": "...",
  "status": "running",
  "phase": "downloading",
  "progress": 42
}
```

`status` is one of `queued`, `running`, `completed`, `failed`. A failed job carries an
`error` object with a `code` and `message`.

### `GET /api/jobs/{job_id}/file`

Streams the finished MP3 as an `audio/mpeg` attachment. **Delivery purges the job and
its files**, so a second request returns `JOB_NOT_FOUND`. Returns `409 NOT_READY` if
the job hasn't completed.

### `GET /api/health`

Operator signal — queue depths, capacity, free disk, tool availability, uptime, and a
`degraded_reasons` list when `status` is `degraded`.

```json
{
  "status": "ok",
  "running": 0,
  "queued": 0,
  "capacity": 8,
  "queue_limit": 80,
  "free_disk_bytes": 123456789012,
  "ytdlp_available": true,
  "ffmpeg_available": true,
  "uptime_seconds": 12.5,
  "degraded_reasons": []
}
```

### Error codes

| Code | Meaning |
| --- | --- |
| `INVALID_INPUT` | Request body or query failed validation |
| `INVALID_URL` | Not a recognizable YouTube URL |
| `INVALID_BITRATE` | Bitrate outside the published set (96/128/192/320) |
| `VIDEO_UNAVAILABLE` | Private, removed, region-blocked, or otherwise unavailable |
| `LIVE_STREAM` | Live streams cannot be extracted |
| `DURATION_EXCEEDED` | Longer than `MAX_DURATION_SECONDS` |
| `AT_CAPACITY` | Global queue is full |
| `CLIENT_LIMIT` | This origin already has `PER_ORIGIN_CAP` active jobs |
| `LOW_DISK` | Free disk below `DISK_FLOOR_BYTES` |
| `JOB_NOT_FOUND` | Unknown job, or already delivered/expired |
| `NOT_READY` | Download requested before the job completed |
| `TIMEOUT` | A phase exceeded its watchdog |
| `NETWORK_ERROR` | Metadata probe or download failed on the network |
| `EXTRACTION_FAILED` | yt-dlp failed |
| `TRANSCODE_FAILED` | FFmpeg failed |
| `FFMPEG_MISSING` | FFmpeg not found on `PATH` |
| `INTERNAL` | Unexpected server-side failure |

---

## Configuration

Every limit is operator-overridable via environment variables. Copy `.env.example` to
`.env` and uncomment what you want to change; defaults are shown below.

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORK_DIR` | `<system temp>/yt-audio-extractor` | Where transient artifacts are written |
| `MAX_CONCURRENCY` | `max(2, cpu_count)` | Concurrent extraction workers |
| `QUEUE_LIMIT` | `10 × MAX_CONCURRENCY` | Global admission bound |
| `PER_ORIGIN_CAP` | `3` | Active jobs allowed per origin |
| `DISK_FLOOR_BYTES` | `1073741824` (1 GiB) | Refuse work below this free space |
| `TTL_SECONDS` | `900` | How long an unretrieved result survives |
| `SWEEP_INTERVAL_SECONDS` | `60` | Interval between TTL/orphan sweeps |
| `DOWNLOAD_TIMEOUT_SECONDS` | `600` | Watchdog for the download phase |
| `TRANSCODE_TIMEOUT_SECONDS` | `300` | Watchdog for the transcode phase |
| `MAX_DURATION_SECONDS` | `3600` | Longest accepted video |
| `PROBE_TIMEOUT_SECONDS` | `15` | Metadata probe timeout |
| `TRUSTED_PROXY` | `0` | See below |
| `REAL_STACK` | `0` | Enables the real yt-dlp/FFmpeg smoke test |

### Running behind a reverse proxy

By default the client's socket IP is used for fairness accounting and
`X-Forwarded-For` is ignored entirely, because clients can forge it. Set
`TRUSTED_PROXY=1` **only** when the app sits behind exactly one trusted proxy — the
origin then comes from the *rightmost* `X-Forwarded-For` value, which is the only entry
that proxy actually vouches for.

---

## Project structure

```
backend/
  main.py              app assembly, security headers, origin hashing, static mount
  config.py            environment-driven configuration
  api/routes.py        /api endpoints
  models/schemas.py    Pydantic request/response models
  models/errors.py     error taxonomy and exception handlers
  services/
    youtube.py         yt-dlp interaction, URL parsing, filename sanitizing
    audio.py           FFmpeg transcoding
    jobs.py            job store, TTL/orphan sweeper
    pool.py            bounded worker pool
    runner.py          per-job execution pipeline
    health.py          yt-dlp/FFmpeg availability probing
frontend/
  index.html, app.js, style.css     dependency-free UI
tests/
  unit/ integration/ contract/
specs/                 feature specs, plans, and tasks
history/               prompt history records and ADRs
```

Static files are mounted **after** the `/api` router so the catch-all static handler
can never shadow an API route.

---

## Testing

```bash
pytest
```

Tests are split into `unit/` (config, URL validation, filename sanitizing, progress
mapping, log privacy), `integration/` (admission, lifecycle, progress, sweeps,
watchdogs, retries, error taxonomy, headers), and `contract/` (the generated OpenAPI
schema must match the published contract).

One test hits the real yt-dlp/FFmpeg stack and is excluded by default via the
`real_stack` marker. To run it locally — never in CI, since it makes real network calls:

```bash
REAL_STACK=1 pytest -m real_stack
```

---

## Security notes

- Non-API responses carry a strict `Content-Security-Policy` (`default-src 'self'`,
  images additionally allowed from `i.ytimg.com`) plus `X-Content-Type-Options: nosniff`.
- No client IPs are persisted; origin hashes are salted per boot and truncated.
- Output filenames are sanitized to an ASCII, quote-safe form before being placed in
  the `Content-Disposition` header.
- Never commit your `.env`. Use `.env.example` as the documented template.

---

## Development

This project follows Spec-Driven Development. Feature specs live in `specs/`,
architectural decisions in `history/adr/`, and prompt history records in
`history/prompts/`. See `CLAUDE.md` and `.specify/memory/constitution.md` for the
working conventions.