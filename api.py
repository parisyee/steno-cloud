"""Steno REST API — transcribe audio and store results in Supabase."""

import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from supabase import create_client, Client

from api_client import DEFAULT_MODEL, transcribe_file
from extract_audio import extract_audio
from trim_deadspace import trim_deadspace

load_dotenv()

app = FastAPI(title="Steno", docs_url="/docs")

API_KEY = os.environ.get("STENO_API_KEY", "")

def _supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def _check_auth(authorization: str | None):
    if not API_KEY:
        return
    if not authorization or authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Transcribe
# ---------------------------------------------------------------------------

@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    skip_trim: bool = False,
    model: str = DEFAULT_MODEL,
):
    _check_auth(authorization)

    suffix = Path(file.filename).suffix if file.filename else ".m4a"
    filename = file.filename
    temp_files: list[Path] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            content = await file.read()
            tmp.write(content)
            input_path = Path(tmp.name)
            temp_files.append(input_path)

        audio_path = extract_audio(input_path)
        if audio_path != input_path:
            temp_files.append(audio_path)

        if skip_trim:
            trimmed_path = audio_path
        else:
            trimmed_path = trim_deadspace(audio_path)
            if trimmed_path != audio_path:
                temp_files.append(trimmed_path)

        transcript = transcribe_file(trimmed_path, model=model)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        for p in temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # Store in Supabase
    row = _supabase().table("transcriptions").insert({
        "filename": filename,
        "text": transcript,
    }).execute()

    return JSONResponse({"text": transcript, "id": row.data[0]["id"]})


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@app.get("/transcriptions")
def list_transcriptions(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
):
    _check_auth(authorization)

    rows = (
        _supabase()
        .table("transcriptions")
        .select("id, filename, text, created_at")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return JSONResponse({"transcriptions": rows.data})


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
    limit: int = Query(default=20, le=100),
):
    _check_auth(authorization)

    rows = (
        _supabase()
        .table("transcriptions")
        .select("id, filename, text, created_at")
        .text_search("search_vec", q)
        .execute()
    )
    return JSONResponse({"results": rows.data[:limit]})
