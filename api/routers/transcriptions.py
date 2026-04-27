"""Transcription endpoints — POST /transcribe, GET /transcriptions, DELETE /transcriptions/{id}."""

import logging
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Path as PathParam, Query, UploadFile
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response

from api.deps import check_auth, get_supabase
from transcription_service.extract_audio import AudioProcessingError, extract_audio
from transcription_service.gemini_client import (
    DEFAULT_ANALYZE_MODEL,
    DEFAULT_TRANSCRIBE_MODEL,
    EmptyTranscriptError,
    GeminiAPIError,
    analyze_transcript,
    transcribe_raw,
)
from transcription_service.trim_deadspace import trim_deadspace

logger = logging.getLogger(__name__)
router = APIRouter()

# Cloud Run's managed ingress caps request bodies at 32 MB; 25 MB leaves
# headroom for multipart overhead. Larger files need the signed-URL/GCS
# upload path (not yet implemented).
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _record_attempt(
    supabase,
    *,
    status: str,
    filename: str | None,
    file_size: int | None,
    content_type: str | None,
    transcribe_model: str,
    analyze_model: str,
    skip_trim: bool,
    polish: bool,
    duration_ms: int,
    transcription_id: str | None = None,
    error_stage: str | None = None,
    error_type: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Best-effort insert into transcription_attempts; swallows its own errors."""
    try:
        supabase.table("transcription_attempts").insert({
            "filename": filename,
            "file_size_bytes": file_size,
            "content_type": content_type,
            "transcribe_model": transcribe_model,
            "analyze_model": analyze_model,
            "skip_trim": skip_trim,
            "polish": polish,
            "status": status,
            "error_stage": error_stage,
            "error_type": error_type,
            "error_detail": error_detail[:2000] if error_detail else None,
            "duration_ms": duration_ms,
            "transcription_id": transcription_id,
        }).execute()
    except Exception:
        logger.warning("failed to record attempt to DB", exc_info=True)


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    skip_trim: bool = True,
    polish: bool = False,
    transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL,
    analyze_model: str = DEFAULT_ANALYZE_MODEL,
    _: None = Depends(check_auth),
):
    supabase = get_supabase()
    suffix = Path(file.filename).suffix if file.filename else ".m4a"
    filename = file.filename
    file_size = file.size
    content_type = file.content_type
    start = time.monotonic()
    stage = "upload"
    temp_files: list[Path] = []

    logger.info(
        "transcribe: received file=%s size=%s content_type=%s transcribe_model=%s analyze_model=%s skip_trim=%s polish=%s",
        filename, file_size, content_type, transcribe_model, analyze_model, skip_trim, polish,
    )

    def record(status, *, transcription_id=None, error_type=None, error_detail=None):
        _record_attempt(
            supabase,
            status=status,
            filename=filename,
            file_size=file_size,
            content_type=content_type,
            transcribe_model=transcribe_model,
            analyze_model=analyze_model,
            skip_trim=skip_trim,
            polish=polish,
            duration_ms=int((time.monotonic() - start) * 1000),
            transcription_id=transcription_id,
            error_stage=stage if status == "failure" else None,
            error_type=error_type,
            error_detail=error_detail,
        )

    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 25 MB.")

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            written = 0
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large. Maximum size is 25 MB.")
                tmp.write(chunk)
            input_path = Path(tmp.name)
            temp_files.append(input_path)

        stage = "extract_audio"
        audio_path = extract_audio(input_path)
        if audio_path != input_path:
            temp_files.append(audio_path)

        if skip_trim:
            trimmed_path = audio_path
        else:
            stage = "trim_deadspace"
            trimmed_path = trim_deadspace(audio_path)
            if trimmed_path != audio_path:
                temp_files.append(trimmed_path)

        stage = "transcribe"
        transcript = transcribe_raw(trimmed_path, model=transcribe_model)

        stage = "analyze"
        analysis = analyze_transcript(transcript, model=analyze_model, polish=polish)

        stage = "insert"
        row = supabase.table("transcriptions").insert({
            "filename": filename,
            "title": analysis.title,
            "description": analysis.description,
            "text": transcript,
            "cleaned_polished": analysis.cleaned_polished,
        }).execute()
        inserted = row.data[0]

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "transcribe: success file=%s id=%s duration_ms=%d",
            filename, inserted["id"], duration_ms,
        )
        record("success", transcription_id=inserted["id"])

        return JSONResponse({
            "id": inserted["id"],
            "filename": filename,
            "title": analysis.title,
            "description": analysis.description,
            "text": transcript,
            "cleaned_polished": analysis.cleaned_polished,
            "created_at": inserted["created_at"],
        })

    except EmptyTranscriptError as e:
        logger.warning(
            "transcribe: EmptyTranscriptError stage=%s file=%s detail=%s",
            stage, filename, e,
        )
        record("failure", error_type="EmptyTranscriptError", error_detail=str(e))
        raise HTTPException(status_code=422, detail=str(e))

    except AudioProcessingError as e:
        logger.error(
            "transcribe: AudioProcessingError stage=%s file=%s detail=%s",
            stage, filename, e,
        )
        record("failure", error_type="AudioProcessingError", error_detail=str(e))
        raise HTTPException(status_code=422, detail=str(e))

    except GeminiAPIError as e:
        logger.error(
            "transcribe: GeminiAPIError stage=%s file=%s detail=%s",
            stage, filename, e,
        )
        record("failure", error_type="GeminiAPIError", error_detail=str(e))
        raise HTTPException(status_code=502, detail=f"Transcription service error: {e}")

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(
            "transcribe: unexpected error stage=%s file=%s", stage, filename,
        )
        record("failure", error_type=type(e).__name__, error_detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        for p in temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


@router.get("/transcriptions")
def list_transcriptions(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
    _: None = Depends(check_auth),
):
    rows = (
        get_supabase()
        .table("transcriptions")
        .select("id, filename, title, description, text, cleaned_polished, created_at")
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
