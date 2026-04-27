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
except ImportError:
    from extract_audio import AudioProcessingError

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


def transcribe_raw(file_path: Path, model: str = DEFAULT_TRANSCRIBE_MODEL) -> str:
    """Pass 1: produce a verbatim transcript with speaker labels and whitespace."""
    load_dotenv()
    client = genai.Client()
    mime_type = get_mime_type(file_path)
    uploaded = upload_file(client, file_path, mime_type)
    logger.info("transcribing with %s", model)
    try:
        response = client.models.generate_content(
            model=model,
            contents=[TRANSCRIBE_PROMPT, uploaded],
            config={"temperature": 0.0},
        )
    except Exception as exc:
        raise GeminiAPIError(f"Gemini generate_content failed: {exc}") from exc
    try:
        text = response.text
    except Exception as exc:
        raise GeminiAPIError(f"Gemini returned no usable response: {exc}") from exc
    if not text or not text.strip():
        raise EmptyTranscriptError(
            "No speech could be transcribed from the audio file."
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
