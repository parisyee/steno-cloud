"""Transcription endpoints — POST /transcribe, GET /transcriptions, DELETE /transcriptions/{id}."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Path as PathParam, Query, UploadFile
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response

from api.deps import check_auth, get_supabase
from transcription_service.extract_audio import extract_audio
from transcription_service.gemini_client import (
    DEFAULT_ANALYZE_MODEL,
    DEFAULT_TRANSCRIBE_MODEL,
    analyze_transcript,
    transcribe_raw,
)
from transcription_service.trim_deadspace import trim_deadspace

router = APIRouter()


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    skip_trim: bool = False,
    transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL,
    analyze_model: str = DEFAULT_ANALYZE_MODEL,
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

        transcript = transcribe_raw(trimmed_path, model=transcribe_model)
        analysis = analyze_transcript(transcript, model=analyze_model)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        for p in temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    cleaned = {
        "light": analysis.cleaned_light,
        "polished": analysis.cleaned_polished,
    }
    row = get_supabase().table("transcriptions").insert({
        "filename": filename,
        "title": analysis.title,
        "description": analysis.description,
        "text": transcript,
        "cleaned": cleaned,
    }).execute()

    inserted = row.data[0]
    return JSONResponse({
        "id": inserted["id"],
        "filename": filename,
        "title": analysis.title,
        "description": analysis.description,
        "text": transcript,
        "cleaned": cleaned,
        "created_at": inserted["created_at"],
    })


@router.get("/transcriptions")
def list_transcriptions(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
    _: None = Depends(check_auth),
):
    rows = (
        get_supabase()
        .table("transcriptions")
        .select("id, filename, title, description, text, cleaned, created_at")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return JSONResponse({"transcriptions": rows.data})


@router.delete("/transcriptions/{transcription_id}")
def delete_transcription(
    transcription_id: str = PathParam(..., min_length=1),
    _: None = Depends(check_auth),
):
    result = (
        get_supabase()
        .table("transcriptions")
        .delete()
        .eq("id", transcription_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Transcription not found")
    return Response(status_code=204)
