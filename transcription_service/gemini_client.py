#!/usr/bin/env python3
"""Gemini API client for audio transcription.

Two-pass pipeline:
  1. transcribe_raw()      — Pro model, audio in, verbatim text out (prose).
  2. analyze_transcript()  — Flash model, text in, JSON with title,
                             description, and (optionally) a polished
                             rewrite of the transcript out.
"""

import argparse
import json
import logging
import mimetypes
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel

try:
    from transcription_service.extract_audio import AudioProcessingError
    from transcription_service.chunk_audio import (
        MAX_CHUNK_SECONDS,
        chunk_audio,
        get_audio_duration,
    )
except ImportError:
    from extract_audio import AudioProcessingError
    from chunk_audio import MAX_CHUNK_SECONDS, chunk_audio, get_audio_duration

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_TYPES = {
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".opus": "audio/opus",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
}

DEFAULT_TRANSCRIBE_MODEL = "gemini-2.5-pro"
DEFAULT_ANALYZE_MODEL = "gemini-2.5-flash"

# Per-chunk output cap. At ~250 tokens/min observed for verbatim conversational
# transcription, a 15-min chunk needs ~3.75K tokens; 8K leaves ~2x headroom for
# fast/dense/multilingual speech while bounding worst-case spend if a chunk
# loops despite the chunk length cap.
MAX_TRANSCRIBE_OUTPUT_TOKENS = 8000

# Kept for backwards compatibility with callers that imported DEFAULT_MODEL.
DEFAULT_MODEL = DEFAULT_TRANSCRIBE_MODEL


TRANSCRIBE_PROMPT = """\
Transcribe the following audio file verbatim.

SPEAKER LABELS (most important — apply to every turn):
- Begin every speaker turn with a label on its own line: `Speaker 1:`, `Speaker 2:`, etc.
- Assign labels by voice. The first voice you hear is Speaker 1, the next new voice is Speaker 2, and so on.
- If only one voice is present throughout, still label the opening turn `Speaker 1:`.
- Never omit a label at a speaker change, even for a one-word interjection ("Yeah.", "Mhm.").

TRANSCRIPTION:
1. Transcribe every word exactly as spoken — do not summarize or paraphrase.
2. If the language changes mid-conversation, continue in whatever language is being spoken.
3. If a word or phrase is unclear, write [inaudible] in its place.

WHITESPACE:
- Add a blank line between speaker turns.
- Add two blank lines for a notably long pause or significant shift in topic/energy.
- Use paragraph breaks within a single speaker's turn for shorter natural pauses or breath points.

Do not add any commentary, headers, or metadata — only the transcript.

Example of the exact output shape:

Speaker 1:
So where did you grow up?

Speaker 2:
Outside Madrid, mostly. We moved when I was twelve.

Pues fue un cambio grande.

Speaker 1:
I bet.
"""


TRANSCRIBE_CONTINUATION_PROMPT = """\
You are continuing a transcription that was split across multiple audio segments.

PRIOR SEGMENT'S LAST TURNS (for speaker-label continuity — DO NOT include these in your output):
---
{prior_context}
---

Transcribe the following audio segment verbatim. Match each voice you hear to
the speaker numbering established above: whoever was Speaker 1 above is still
Speaker 1 here, same for Speaker 2, etc. Only assign a new speaker number if
you hear a voice that does not appear in the prior context.

Other rules:
- Begin every speaker turn with a label on its own line.
- Never omit a label at a speaker change, even for a one-word interjection.
- Transcribe every word exactly as spoken — do not summarize or paraphrase.
- If a word is unclear, write [inaudible] in its place.
- Add a blank line between speaker turns.
- Output ONLY the transcript for this segment. No headers, commentary, or repetition of the prior context.
"""


SUMMARY_PROMPT = """\
You will be given a verbatim transcript with speaker labels and blank-line spacing.
Produce two fields as JSON.

- title: a short sentence (≤10 words) capturing the gist of the conversation.
  Write it in the same language as the transcript. If the transcript mixes
  languages, use the language spoken most.
- description: 2-4 sentences OR 3-5 short bullet points summarizing what's discussed.
  Use the same language as the title (the transcript's dominant language).

Transcript:
---
{transcript}
---
"""


POLISH_PROMPT = """\
You will be given a verbatim transcript with speaker labels and blank-line spacing.
Produce three fields as JSON.

- title: a short sentence (≤10 words) capturing the gist of the conversation.
  Write it in the same language as the transcript. If the transcript mixes
  languages, use the language spoken most.
- description: 2-4 sentences OR 3-5 short bullet points summarizing what's discussed.
  Use the same language as the title (the transcript's dominant language).
- cleaned_polished: the transcript with disfluencies removed (um, uh, filler
  "like" / "you know", false starts), stutters and repetition smoothed, word
  fumbles resolved to the intended word, and fragments combined into complete
  sentences where intent is clear. Do NOT paraphrase, summarize, change meaning,
  or translate. Preserve speaker labels and language switches. Never collapse
  two speakers' turns into one. Never invent content. Whitespace may be
  re-derived from natural reading cadence.

Transcript:
---
{transcript}
---
"""


class Summary(BaseModel):
    title: str
    description: str


class AnalysisResult(BaseModel):
    title: str
    description: str
    cleaned_polished: str | None = None


def get_mime_type(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in SUPPORTED_AUDIO_TYPES:
        return SUPPORTED_AUDIO_TYPES[suffix]
    mime, _ = mimetypes.guess_type(str(file_path))
    if mime:
        return mime
    raise AudioProcessingError(
        f"Could not determine MIME type for '{file_path.name}'"
    )


def upload_file(client: genai.Client, file_path: Path, mime_type: str):
    logger.info("uploading %s (%s)", file_path.name, mime_type)
    # Use display_name to pass an ASCII-safe name without loading the whole
    # file into memory \u2014 avoids encoding errors from Unicode chars like \u202f.
    safe_name = file_path.name.encode("ascii", "replace").decode("ascii")
    with open(file_path, "rb") as f:
        uploaded = client.files.upload(
            file=f,
            config={"mime_type": mime_type, "display_name": safe_name},
        )
    logger.info("upload complete: %s", uploaded.name)
    return uploaded


class EmptyTranscriptError(Exception):
    """Raised when transcription produces no text (silent audio, unreadable file, etc.)."""


class GeminiAPIError(Exception):
    """Raised when the Gemini API returns an unusable response (blocked, quota, etc.)."""


class TranscriptRepetitionError(GeminiAPIError):
    """Raised when the model output contains a repetition loop.

    Gemini at temperature=0 on long-form audio occasionally falls into a
    decoding loop, regurgitating a passage from the end of the conversation
    until it hits the output token cap. Detecting this post-hoc lets us fail
    loudly instead of persisting a corrupt transcript.
    """


def detect_repetition(
    text: str,
    *,
    min_turn_chars: int = 80,
    max_occurrences: int = 2,
) -> tuple[bool, str | None]:
    """Detect runaway repetition by counting duplicate speaker turns.

    Splits on blank lines (the prompt-enforced turn separator) and checks
    whether any non-trivial turn (>= `min_turn_chars`) appears more than
    `max_occurrences` times. Real conversation produces near-zero verbatim
    duplicates of substantive turns; a Gemini decoding loop produces many.
    """
    if not text:
        return (False, None)
    counts: dict[str, int] = {}
    for turn in text.split("\n\n"):
        normalized = turn.strip()
        if len(normalized) < min_turn_chars:
            continue
        c = counts.get(normalized, 0) + 1
        counts[normalized] = c
        if c > max_occurrences:
            preview = normalized[:80].replace("\n", " ")
            return (True, preview)
    return (False, None)


def _build_continuation_context(prior_text: str, turns: int = 4) -> str:
    """Take the last few speaker turns from a prior chunk for continuity prompting."""
    parts = [p for p in prior_text.strip().split("\n\n") if p.strip()]
    return "\n\n".join(parts[-turns:])


def _generate_chunk_text(
    client: genai.Client,
    file_path: Path,
    model: str,
    prior_context: str | None,
) -> str:
    """Run a single Gemini transcription pass over one chunk."""
    mime_type = get_mime_type(file_path)
    uploaded = upload_file(client, file_path, mime_type)
    if prior_context:
        prompt = TRANSCRIBE_CONTINUATION_PROMPT.format(prior_context=prior_context)
    else:
        prompt = TRANSCRIBE_PROMPT
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, uploaded],
            config={
                "temperature": 0.0,
                "max_output_tokens": MAX_TRANSCRIBE_OUTPUT_TOKENS,
            },
        )
    except Exception as exc:
        raise GeminiAPIError(f"Gemini generate_content failed: {exc}") from exc
    # Hitting the token cap on a 15-min chunk means we either (a) under-
    # transcribed legitimate speech or (b) caught a loop mid-flight. Either
    # way the result is suspect — fail loudly rather than persist a partial.
    finish = _finish_reason(response)
    if finish == "MAX_TOKENS":
        raise GeminiAPIError(
            f"Gemini hit max_output_tokens={MAX_TRANSCRIBE_OUTPUT_TOKENS} on a chunk; "
            f"transcript is truncated and cannot be trusted. "
            f"Either the audio is unusually dense or the model entered a loop."
        )
    try:
        text = response.text
    except Exception as exc:
        raise GeminiAPIError(f"Gemini returned no usable response: {exc}") from exc
    return text or ""


def _finish_reason(response) -> str | None:
    """Return the response's finish_reason as a plain string (or None if unavailable)."""
    try:
        reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    if reason is None:
        return None
    # SDK returns an enum; .name gives "MAX_TOKENS", "STOP", etc.
    return getattr(reason, "name", str(reason))


def transcribe_raw(file_path: Path, model: str = DEFAULT_TRANSCRIBE_MODEL) -> str:
    """Pass 1: produce a verbatim transcript with speaker labels and whitespace.

    Long audio is split into silence-aligned chunks (see `chunk_audio`) and
    transcribed in sequence, with each later chunk receiving the prior
    chunk's last few turns as context so speaker numbering stays stable.

    The final stitched transcript is checked for runaway repetition before
    being returned; a hit raises `TranscriptRepetitionError` so corrupt
    output never reaches the database.
    """
    load_dotenv()
    client = genai.Client()

    duration = get_audio_duration(file_path)
    if duration <= MAX_CHUNK_SECONDS:
        logger.info("transcribing with %s (single-shot, %.1fs)", model, duration)
        text = _generate_chunk_text(client, file_path, model, prior_context=None)
    else:
        chunks = chunk_audio(file_path)
        logger.info(
            "transcribing with %s (%d chunks, %.1fs total)", model, len(chunks), duration,
        )
        try:
            pieces: list[str] = []
            for i, (chunk_path, start_offset) in enumerate(chunks):
                logger.info(
                    "transcribing chunk %d/%d (start=%.1fs)",
                    i + 1, len(chunks), start_offset,
                )
                prior = _build_continuation_context(pieces[-1]) if pieces else None
                chunk_text = _generate_chunk_text(client, chunk_path, model, prior)
                if chunk_text.strip():
                    pieces.append(chunk_text.strip())
            text = "\n\n".join(pieces)
        finally:
            for chunk_path, _ in chunks:
                if chunk_path != file_path:
                    chunk_path.unlink(missing_ok=True)

    if not text or not text.strip():
        raise EmptyTranscriptError(
            "No speech could be transcribed from the audio file."
        )

    looped, snippet = detect_repetition(text)
    if looped:
        raise TranscriptRepetitionError(
            f"Transcript contains a repetition loop (repeated passage near: {snippet!r}). "
            f"This is usually a Gemini decoding failure on long audio; retry or shorten the input."
        )

    logger.info("transcription complete: %d chars", len(text))
    return text


def analyze_transcript(
    transcript: str,
    model: str = DEFAULT_ANALYZE_MODEL,
    polish: bool = False,
) -> AnalysisResult:
    """Pass 2: derive a title and description from a transcript.

    When ``polish`` is true, also generate a polished rewrite of the transcript
    (`cleaned_polished`). Generating the polished version roughly doubles the
    Flash output tokens, so it's opt-in.
    """
    load_dotenv()
    client = genai.Client()
    logger.info("analyzing transcript with %s (polish=%s)", model, polish)
    prompt = POLISH_PROMPT if polish else SUMMARY_PROMPT
    schema = AnalysisResult if polish else Summary
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt.format(transcript=transcript)],
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
                "response_schema": schema,
            },
        )
    except Exception as exc:
        raise GeminiAPIError(f"Gemini generate_content failed: {exc}") from exc
    try:
        raw_text = response.text
    except Exception as exc:
        raise GeminiAPIError(f"Gemini returned no usable response: {exc}") from exc
    if not raw_text:
        raise GeminiAPIError("Gemini returned an empty analysis response")
    try:
        if polish:
            return AnalysisResult.model_validate_json(raw_text)
        summary = Summary.model_validate_json(raw_text)
        return AnalysisResult(title=summary.title, description=summary.description)
    except Exception as exc:
        raise GeminiAPIError(f"Failed to parse Gemini analysis JSON: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio using the Gemini API")
    parser.add_argument("audio_file", type=Path, help="Path to the audio file")
    parser.add_argument(
        "--transcribe-model",
        default=DEFAULT_TRANSCRIBE_MODEL,
        help=f"Gemini model for transcription (default: {DEFAULT_TRANSCRIBE_MODEL})",
    )
    parser.add_argument(
        "--analyze-model",
        default=DEFAULT_ANALYZE_MODEL,
        help=f"Gemini model for analysis pass (default: {DEFAULT_ANALYZE_MODEL})",
    )
    parser.add_argument(
        "--skip-analysis",
        action="store_true",
        help="Skip the analysis pass and only print the raw transcript",
    )
    parser.add_argument(
        "--polish",
        action="store_true",
        help="Also generate a polished rewrite of the transcript",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write JSON {transcript, title, description, cleaned_polished?} to a file",
    )
    args = parser.parse_args()

    if not args.audio_file.exists():
        sys.exit(f"Error: File not found: {args.audio_file}")

    transcript = transcribe_raw(args.audio_file, args.transcribe_model)

    if args.skip_analysis:
        if args.output:
            args.output.write_text(transcript, encoding="utf-8")
            print(f"Transcript written to {args.output}")
        else:
            print("\n--- Transcript ---\n")
            print(transcript)
        return

    analysis = analyze_transcript(transcript, args.analyze_model, polish=args.polish)
    payload = {"transcript": transcript, **analysis.model_dump(exclude_none=True)}

    if args.output:
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Result written to {args.output}")
    else:
        print("\n--- Title ---\n" + analysis.title)
        print("\n--- Description ---\n" + analysis.description)
        print("\n--- Transcript ---\n" + transcript)
        if analysis.cleaned_polished:
            print("\n--- Cleaned (polished) ---\n" + analysis.cleaned_polished)


if __name__ == "__main__":
    main()
