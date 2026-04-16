# steno

A transcription backend for audio and video files using the Google Gemini API. Supports multiple speakers, mixed languages, and a variety of formats. Before sending audio to the API, it strips long silences to reduce file size and API costs.

Steno has two interfaces:
- **REST API** — a FastAPI app deployed on Google Cloud Run, used by the iOS app and any future clients
- **CLI** — the original scripts, still usable standalone for local/dev use

## System architecture

```
iOS (steno-ios) ──────────────────────────────────────────┐
                                                           ▼
                                              Cloud Run: steno API
                                             (FastAPI + ffmpeg + Gemini)
                                                           │
                                          ┌────────────────┴────────────────┐
                                          ▼                                 ▼
                                  Gemini API                         Supabase (Postgres)
                               (transcription)                    (transcriptions table)
```

### Repos
- **`parisyee/steno`** — this repo, the backend API
- **`parisyee/steno-ios`** — iOS app with Share Extension

### Infrastructure
- **Google Cloud Run** (`steno-prod` project, `us-central1`) — runs the containerized API
- **Google Artifact Registry** (`steno-prod/steno`) — stores Docker images
- **Supabase** — Postgres database for transcription storage and full-text search
- **GitHub Actions** — builds and deploys to Cloud Run on every push to `main`

### Live URL
```
https://steno-836899141951.us-central1.run.app
```

---

## API

All endpoints require a `Bearer` token in the `Authorization` header (set via `STENO_API_KEY` env var). If `STENO_API_KEY` is not set, auth is disabled.

### `POST /transcribe`
Upload an audio or video file. Runs the full pipeline (extract → trim silence → transcribe) and stores the result in Supabase.

```bash
curl -X POST https://steno-836899141951.us-central1.run.app/transcribe \
  -H "Authorization: Bearer YOUR_STENO_API_KEY" \
  -F "file=@recording.m4a"
```

Query params:
- `skip_trim=true` — skip silence trimming
- `model=gemini-2.5-pro` — override the Gemini model (default: `gemini-2.5-flash`)

Response:
```json
{ "text": "...", "id": "uuid" }
```

### `GET /transcriptions`
List all transcriptions, newest first.

```bash
curl https://steno-836899141951.us-central1.run.app/transcriptions \
  -H "Authorization: Bearer YOUR_STENO_API_KEY"
```

Query params: `limit` (default 20, max 100), `offset` (default 0)

### `GET /search?q=...`
Full-text search across transcription text and filenames.

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
uv run uvicorn api:app --reload
```

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
  --project steno-prod
```

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
  id          uuid primary key,
  filename    text,
  text        text,
  search_vec  tsvector,   -- generated from filename + text, indexed for full-text search
  created_at  timestamptz
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
uv run python transcribe.py recording.m4a --model gemini-2.5-pro
uv run python transcribe.py recording.m4a --threshold -40 --min-silence 1.0
```

| Option | Default | Description |
|---|---|---|
| `input_file` | — | Path to any audio or video file |
| `-o`, `--output` | `<input>.txt` | Output transcript path |
| `--lock` | off | Make output read-only after writing |
| `--model` | `gemini-2.5-flash` | Gemini model |
| `--skip-trim` | off | Skip silence removal |
| `--threshold DB` | `-35` | Silence floor in dB |
| `--min-silence SECONDS` | `1.5` | Minimum silence duration before removal |
| `--keep-edge SECONDS` | `0.3` | Silence to preserve at cut boundaries |

### Individual scripts

```bash
uv run python extract_audio.py clip.mp4 -o audio.m4a
uv run python trim_deadspace.py recording.m4a -o trimmed.m4a
uv run python api_client.py audio.m4a -o transcript.txt
```

## Supported formats

**Audio:** mp3, wav, flac, ogg, m4a, aac, wma, opus

**Video:** mp4, mov, mkv, avi, webm, m4v, flv, wmv, ts
