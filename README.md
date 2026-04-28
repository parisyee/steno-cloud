# steno

A transcription backend for audio and video files using the Google Gemini API. Supports multiple speakers, mixed languages, and a variety of formats. Before sending audio to the API, it strips long silences to reduce file size and API costs.

Each upload runs through a two-pass pipeline:

1. **Transcribe (Gemini 2.5 Pro)** — verbatim transcript with speaker labels (`Speaker 1:`, `Speaker 2:`, …) and blank-line spacing that reflects pauses and topic shifts.
2. **Analyze (Gemini 2.5 Flash)** — derives a short `title` and a `description` from the transcript. If the caller opts in with `polish=true`, also generates `cleaned_polished` — a polished rewrite with disfluencies removed, stutters smoothed, and fragments combined into complete sentences. Polish is off by default to save tokens.

Steno has two interfaces:
- **REST API** — a FastAPI app deployed on Google Cloud Run, used by the iOS app and any future clients
- **CLI** — the original scripts, still usable standalone for local/dev use

## System architecture

```
iOS (steno-ios) ─────────────┐
                             │
Web (steno-nextjs) ──────────┤   HTTP/2 (h2c via Cloud Run --use-http2)
                             ▼
                  Cloud Run: steno API
                 (FastAPI + Hypercorn + ffmpeg + Gemini)
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
         Gemini API                Supabase (Postgres)
       (transcription)         (transcriptions table)
```

The Cloud Run service runs **Hypercorn** (not uvicorn) so the container can speak h2c, and is deployed with `--use-http2` so Cloud Run's front-end forwards HTTP/2 directly to the container. This bypasses the 32 MB request body cap that applies on the HTTP/1.1 forwarding path. Practical upload ceiling is 2 GB (Gemini's File API limit).

### Repos
- **`parisyee/steno`** — this repo, the backend API
- **`parisyee/steno-ios`** — iOS app with Share Extension
- **`parisyee/steno-nextjs`** — web client (Next.js)

### Infrastructure
- **Google Cloud Run** (`steno-prod` project, `us-central1`) — runs the containerized API
- **Google Artifact Registry** (`steno-prod/steno`) — stores Docker images
- **Supabase** — Postgres database for transcription storage and full-text search
- **GitHub Actions** — builds and deploys to Cloud Run on every push to `main`

> **For provisioning, IAM, and standing up new environments, see [INFRASTRUCTURE.md](./INFRASTRUCTURE.md).** That doc is the runbook for cloud setup; this README focuses on the application itself.

### Live URL
```
https://steno-836899141951.us-central1.run.app
```

---

## API

All endpoints require a `Bearer` token in the `Authorization` header (set via `STENO_API_KEY` env var). If `STENO_API_KEY` is not set, auth is disabled.

### `POST /transcribe`
Upload an audio or video file. Runs the full pipeline (extract → trim silence → transcribe → analyze) and stores the result in Supabase. Maximum upload size is 2 GB; uploads are streamed to disk so the server doesn't hold the whole body in memory.

```bash
curl -X POST https://steno-836899141951.us-central1.run.app/transcribe \
  -H "Authorization: Bearer YOUR_STENO_API_KEY" \
  -F "file=@recording.m4a"
```

Query params:
- `skip_trim=true` — skip silence trimming
- `polish=true` — also generate a polished rewrite of the transcript (off by default)
- `transcribe_model=gemini-2.5-pro` — override the model used for the transcription pass (default: `gemini-2.5-pro`)
- `analyze_model=gemini-2.5-flash` — override the model used for the analysis pass (default: `gemini-2.5-flash`)

Response:
```json
{
  "id": "uuid",
  "filename": "recording.m4a",
  "title": "...",
  "description": "...",
  "text": "Speaker 1:\nSo where did you grow up?\n\nSpeaker 2:\n...",
  "cleaned_polished": "...",
  "created_at": "2026-04-18T12:34:56.789+00:00"
}
```

`cleaned_polished` is `null` unless the request was made with `polish=true`.

### `GET /transcriptions`
List all transcriptions, newest first. Each row includes `id, filename, title, description, text, cleaned_polished, created_at`.

```bash
curl https://steno-836899141951.us-central1.run.app/transcriptions \
  -H "Authorization: Bearer YOUR_STENO_API_KEY"
```

Query params: `limit` (default 20, max 100), `offset` (default 0)

### `DELETE /transcriptions/{id}`
Delete a transcription by id. Returns `204 No Content` on success, `404` if the id doesn't exist.

```bash
curl -X DELETE https://steno-836899141951.us-central1.run.app/transcriptions/UUID \
  -H "Authorization: Bearer YOUR_STENO_API_KEY"
```

### `GET /search?q=...`
Full-text search across `filename`, `title`, `description`, and the raw `text`. The polished rewrite is deliberately excluded from the search index — it's largely redundant with the raw transcript, and a future embedding-based search will not need it either.

```bash
curl "https://steno-836899141951.us-central1.run.app/search?q=freedom+connection" \
  -H "Authorization: Bearer YOUR_STENO_API_KEY"
```

### `GET /health`
Returns `{"status": "ok"}`. No auth required.

---

## Local development

### Requirements
- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/)
- [Docker](https://www.docker.com/)

### Setup

```bash
brew install ffmpeg

uv venv
uv pip install -r requirements.txt
```

### Environment variables

Create a `.env` file in the project root:

```
GEMINI_API_KEY=...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=...
STENO_API_KEY=...        # optional, protects endpoints
```

### Run the API locally (Docker)

```bash
docker build -t steno .
docker run -p 8080:8080 --env-file .env steno
```

Then hit `http://localhost:8080`.

### Run the API locally (no Docker)

```bash
uv run hypercorn api.main:app --reload
```

Hypercorn is used instead of uvicorn so the container can speak h2c when Cloud Run is deployed with `--use-http2`.

---

## Deployment

Deploys automatically to Cloud Run on push to `main` via GitHub Actions (`.github/workflows/deploy.yml`).

The workflow:
1. Authenticates to GCP using the `github-deployer` service account (`GCP_SA_KEY` secret)
2. Builds the Docker image and pushes it to Artifact Registry, tagged with the git SHA
3. Deploys the new image to Cloud Run with secrets injected as env vars

### GitHub Actions secrets required
| Secret | Description |
|---|---|
| `GCP_SA_KEY` | JSON key for the `github-deployer` GCP service account |
| `GEMINI_API_KEY` | Google Gemini API key |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase service key |
| `STENO_API_KEY` | Bearer token for API auth |

### Manual deploy
```bash
gcloud run deploy steno \
  --source . \
  --region us-central1 \
  --project steno-prod \
  --use-http2 \
  --memory 4Gi \
  --timeout 3600
```

`--use-http2` is required for HTTP/2 end-to-end (lifts the 32 MB request body cap). 4 Gi memory and a 3600 s timeout accommodate uploads up to 2 GB.

---

## Database

Supabase project linked via `supabase link`. Migrations are in `supabase/migrations/`.

```bash
# Apply migrations to production
supabase db push
```

### Schema

```sql
transcriptions (
  id                uuid primary key,
  filename          text,
  title             text,     -- short sentence, generated by the analysis pass
  description       text,     -- 2-4 sentences or 3-5 bullets, generated by the analysis pass
  text              text,     -- raw verbatim transcript with speaker labels and whitespace
  cleaned_polished  text,     -- polished rewrite; null unless uploaded with polish=true
  search_vec        tsvector, -- generated from filename + title + description + text
  created_at        timestamptz
)
```

> **Future:** `search_vec` will be joined by an `embedding vector(768)` column for semantic search using Gemini's `text-embedding-004` model and `pgvector`.

---

## CLI (local use)

The original scripts remain usable standalone.

### Full pipeline

```bash
uv run python transcribe.py recording.m4a
uv run python transcribe.py recording.m4a --lock        # read-only output
uv run python transcribe.py recording.m4a -o out.txt
uv run python transcribe.py recording.m4a --skip-trim
uv run python transcribe.py recording.m4a --skip-analysis
uv run python transcribe.py recording.m4a --polish
uv run python transcribe.py recording.m4a --transcribe-model gemini-2.5-pro --analyze-model gemini-2.5-flash
uv run python transcribe.py recording.m4a --threshold -40 --min-silence 1.0
```

The CLI writes two files: `<input>.txt` (the raw transcript) and `<input>.json` (a sidecar containing `title`, `description`, `transcript`, and — when `--polish` is set — `cleaned_polished`). Pass `--skip-analysis` to skip the second pass and write only the `.txt`.

| Option | Default | Description |
|---|---|---|
| `input_file` | — | Path to any audio or video file |
| `-o`, `--output` | `<input>.txt` | Output transcript path (the JSON sidecar uses the same stem) |
| `--lock` | off | Make output files read-only after writing |
| `--transcribe-model` | `gemini-2.5-pro` | Model for the transcription pass |
| `--analyze-model` | `gemini-2.5-flash` | Model for the analysis pass |
| `--skip-trim` | off | Skip silence removal |
| `--skip-analysis` | off | Skip the second-pass analysis (no title, description, or polished rewrite) |
| `--polish` | off | Also generate a polished rewrite of the transcript |
| `--threshold DB` | `-35` | Silence floor in dB |
| `--min-silence SECONDS` | `1.5` | Minimum silence duration before removal |
| `--keep-edge SECONDS` | `0.3` | Silence to preserve at cut boundaries |

### Individual scripts

```bash
uv run python -m transcription_service.extract_audio clip.mp4 -o audio.m4a
uv run python -m transcription_service.trim_deadspace recording.m4a -o trimmed.m4a
uv run python -m transcription_service.gemini_client audio.m4a -o result.json
```

## Supported formats

**Audio:** mp3, wav, flac, ogg, m4a, aac, wma, opus

**Video:** mp4, mov, mkv, avi, webm, m4v, flv, wmv, ts
