"""Transcription endpoints — POST /transcribe, GET /transcriptions."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from api.deps import check_auth, get_supabase
from transcription_service.extract_audio import extract_audio
from transcription_service.gemini_client import DEFAULT_MODEL, transcribe_file
from transcription_service.trim_deadspace import trim_deadspace

router = APIRouter()


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    skip_trim: bool = False,
    model: str = DEFAULT_MODEL,
    _: None = Depends(check_auth),
):
    suffix = Path(file.filename).suffix if file.filename else ".m4a"
    filename = file.filename
    temp_files: list[Path] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
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

    row = get_supabase().table("transcriptions").insert({
        "filename": filename,
        "text": transcript,
    }).execute()

    return JSONResponse({"text": transcript, "id": row.data[0]["id"]})


@router.get("/transcriptions")
def list_transcriptions(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
    _: None = Depends(check_auth),
):
    rows = (
        get_supabase()
        .table("transcriptions")
        .select("id, filename, text, created_at")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return JSONResponse({"transcriptions": rows.data})
