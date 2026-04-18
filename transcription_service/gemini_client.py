#!/usr/bin/env python3
"""Gemini API client for audio transcription.

Two-pass pipeline:
  1. transcribe_raw()      — Pro model, audio in, verbatim text out (prose).
  2. analyze_transcript()  — Flash model, text in, JSON with title /
                             description / cleaned variants out.
"""

import argparse
import io
import json
import mimetypes
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel

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


ANALYZE_PROMPT = """\
You will be given a verbatim transcript with speaker labels and blank-line spacing.
Produce four fields as JSON.

- title: a short sentence (≤10 words) capturing the gist of the conversation.
- description: 2-4 sentences OR 3-5 short bullet points summarizing what's discussed.
- cleaned_light: the same transcript with disfluencies removed (um, uh, filler "like" /
  "you know", obvious false starts). Keep every other word verbatim. Preserve speaker
  labels and the blank-line spacing exactly as in the input.
- cleaned_polished: also smooth stutters and repetition, resolve word fumbles to the
  intended word, and combine fragments into complete sentences where intent is clear.
  Do NOT paraphrase, summarize, change meaning, or translate. Preserve speaker labels
  and language switches. Whitespace may be re-derived from natural reading cadence.

Never collapse two speakers' turns into one. Never invent content.

Transcript:
---
{transcript}
---
"""


class AnalysisResult(BaseModel):
    title: str
    description: str
    cleaned_light: str
    cleaned_polished: str


def get_mime_type(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in SUPPORTED_AUDIO_TYPES:
        return SUPPORTED_AUDIO_TYPES[suffix]
    mime, _ = mimetypes.guess_type(str(file_path))
    if mime:
        return mime
    sys.exit(f"Error: Could not determine MIME type for '{file_path.name}'")


def upload_file(client: genai.Client, file_path: Path, mime_type: str):
    print(f"Uploading {file_path.name} ({mime_type})...")
    # Read into a BytesIO with an ASCII-safe name to avoid encoding errors
    # in HTTP headers (filenames may contain Unicode chars like \u202f).
    safe_name = file_path.name.encode("ascii", "replace").decode("ascii")
    buf = io.BytesIO(file_path.read_bytes())
    buf.name = safe_name
    uploaded = client.files.upload(file=buf, config={"mime_type": mime_type})
    print(f"Upload complete: {uploaded.name}")
    return uploaded


def transcribe_raw(file_path: Path, model: str = DEFAULT_TRANSCRIBE_MODEL) -> str:
    """Pass 1: produce a verbatim transcript with speaker labels and whitespace."""
    load_dotenv()
    client = genai.Client()
    mime_type = get_mime_type(file_path)
    uploaded = upload_file(client, file_path, mime_type)
    print(f"Transcribing with {model}...")
    response = client.models.generate_content(
        model=model,
        contents=[TRANSCRIBE_PROMPT, uploaded],
        config={"temperature": 0.0},
    )
    return response.text


def analyze_transcript(
    transcript: str, model: str = DEFAULT_ANALYZE_MODEL
) -> AnalysisResult:
    """Pass 2: derive title, description, and cleaned variants from a transcript."""
    load_dotenv()
    client = genai.Client()
    print(f"Analyzing transcript with {model}...")
    response = client.models.generate_content(
        model=model,
        contents=[ANALYZE_PROMPT.format(transcript=transcript)],
        config={
            "temperature": 0.0,
            "response_mime_type": "application/json",
            "response_schema": AnalysisResult,
        },
    )
    return AnalysisResult.model_validate_json(response.text)


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
        "--output",
        "-o",
        type=Path,
        help="Write JSON {transcript, title, description, cleaned_light, cleaned_polished} to a file",
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

    analysis = analyze_transcript(transcript, args.analyze_model)
    payload = {"transcript": transcript, **analysis.model_dump()}

    if args.output:
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Result written to {args.output}")
    else:
        print("\n--- Title ---\n" + analysis.title)
        print("\n--- Description ---\n" + analysis.description)
        print("\n--- Transcript ---\n" + transcript)
        print("\n--- Cleaned (light) ---\n" + analysis.cleaned_light)
        print("\n--- Cleaned (polished) ---\n" + analysis.cleaned_polished)


if __name__ == "__main__":
    main()
