#!/usr/bin/env python3
"""Gemini API client for audio transcription."""

import argparse
import io
import mimetypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai

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

TRANSCRIPTION_PROMPT = """\
Transcribe the following audio file verbatim. Follow these rules:

1. Transcribe every word exactly as spoken — do not summarize or paraphrase.
2. If there are multiple speakers, label them (e.g., Speaker 1, Speaker 2).
3. If the language changes mid-conversation, continue transcribing in whatever language is being spoken.
4. Use punctuation and paragraph breaks to reflect natural pauses and topic shifts.
5. If a word or phrase is unclear, write [inaudible] in its place.
6. Do not add any commentary, headers, or metadata — only the transcript.
"""

DEFAULT_MODEL = "gemini-2.5-flash"


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


def transcribe_file(file_path: Path, model: str = DEFAULT_MODEL) -> str:
    """Transcribe an audio file using the Gemini API.

    Loads credentials from the environment (GEMINI_API_KEY) and .env if present.

    Args:
        file_path: Path to the audio file to transcribe.
        model: Gemini model identifier to use.

    Returns:
        The transcript as a string.
    """
    load_dotenv()
    client = genai.Client()
    mime_type = get_mime_type(file_path)
    uploaded = upload_file(client, file_path, mime_type)
    print(f"Transcribing with {model}...")
    response = client.models.generate_content(
        model=model,
        contents=[TRANSCRIPTION_PROMPT, uploaded],
    )
    return response.text


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio using the Gemini API")
    parser.add_argument("audio_file", type=Path, help="Path to the audio file")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write transcript to a file instead of stdout",
    )
    args = parser.parse_args()

    if not args.audio_file.exists():
        sys.exit(f"Error: File not found: {args.audio_file}")

    if args.output:
        if args.output.is_dir():
            sys.exit(f"Error: Output path is a directory: {args.output}")
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            sys.exit(f"Error: Cannot create output directory '{args.output.parent}': {e}")
        try:
            args.output.touch()
        except OSError as e:
            sys.exit(f"Error: Cannot write to '{args.output}': {e}")

    transcript = transcribe_file(args.audio_file, args.model)

    if args.output:
        args.output.write_text(transcript, encoding="utf-8")
        print(f"Transcript written to {args.output}")
    else:
        print("\n--- Transcript ---\n")
        print(transcript)


if __name__ == "__main__":
    main()
