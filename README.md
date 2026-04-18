# steno

A transcription backend for audio and video files using the Google Gemini API. Supports multiple speakers, mixed languages, and a variety of formats. Before sending audio to the API, it strips long silences to reduce file size and API costs.

Each upload runs through a two-pass pipeline:

1. **Transcribe (Gemini 2.5 Pro)** — verbatim transcript with speaker labels (`Speaker 1:`, `Speaker 2:`, …) and blank-line spacing that reflects pauses and topic shifts.
2. **Analyze (Gemini 2.5 Flash)** — derives a short `title`, a `description`, and two cleaned variants of the transcript (`light` removes disfluencies; `polished` also smooths repetition and word fumbles) without re-uploading the audio.

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
Upload an audio or video file. Runs the full pipeline (extract → trim silence → transcribe → analyze) and stores the result in Supabase.

```bash
curl -X POST https://steno-836899141951.us-central1.run.app/transcribe \
  -H "Authorization: Bearer YOUR_STENO_API_KEY" \
  -F "file=@recording.m4a"
```

Query params:
- `skip_trim=true` — skip silence trimming
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
  "cleaned": {
    "light": "...",
    "polished": "..."
  }
}
```

### `GET /transcriptions`
List all transcriptions, newest first. Each row includes `id, filename, title, description, text, cleaned, created_at`.

```bash
curl https://steno-836899141951.us-central1.run.app/transcriptions \
  -H "Authorization: Bearer YOUR_STENO_API_KEY"
```

Query params: `limit` (default 20, max 100), `offset` (default 0)

### `GET /search?q=...`
Full-text search across `filename`, `title`, `description`, and the raw `text`. Cleaned variants are deliberately excluded from the search index — they're largely redundant with the raw transcript, and a future embedding-based search will not need them either.

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
  title       text,        -- short sentence, generated by the analysis pass
  description text,        -- 2-4 sentences or 3-5 bullets, generated by the analysis pass
  text        text,        -- raw verbatim transcript with speaker labels and whitespace
  cleaned     jsonb,       -- { "light": "...", "polished": "..." } — extensible map of cleaned variants
  search_vec  tsvector,    -- generated from filename + title + description + text
  created_at  timestamptz
)
```

`cleaned` is intentionally a JSON column so new polish tiers can be added without a schema migration.

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
uv run python transcribe.py recording.m4a --transcribe-model gemini-2.5-pro --analyze-model gemini-2.5-flash
uv run python transcribe.py recording.m4a --threshold -40 --min-silence 1.0
```

The CLI writes two files: `<input>.txt` (the raw transcript) and `<input>.json` (a sidecar containing `title`, `description`, `transcript`, and `cleaned.{light,polished}`). Pass `--skip-analysis` to skip the second pass and write only the `.txt`.

| Option | Default | Description |
|---|---|---|
| `input_file` | — | Path to any audio or video file |
| `-o`, `--output` | `<input>.txt` | Output transcript path (the JSON sidecar uses the same stem) |
| `--lock` | off | Make output files read-only after writing |
| `--transcribe-model` | `gemini-2.5-pro` | Model for the transcription pass |
| `--analyze-model` | `gemini-2.5-flash` | Model for the analysis pass |
| `--skip-trim` | off | Skip silence removal |
| `--skip-analysis` | off | Skip the second-pass analysis (no title, description, or cleaned versions) |
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
