#!/usr/bin/env python3
"""Orchestrate audio/video transcription: extract → trim silence → transcribe via Gemini API."""

import argparse
import sys
import tempfile
from pathlib import Path

from api_client import DEFAULT_MODEL, transcribe_file
from extract_audio import extract_audio, is_audio
from trim_deadspace import trim_deadspace


def run(
    input_path: Path,
    output_path: Path | None = None,
    model: str = DEFAULT_MODEL,
    silence_threshold: float = -35.0,
    min_silence_duration: float = 1.5,
    keep_edge_silence: float = 0.3,
    skip_trim: bool = False,
) -> Path:
    """Extract audio, optionally trim silence, transcribe, and write a .txt file.

    Args:
        input_path: Path to any audio or video file.
        output_path: Destination .txt file. Defaults to <input_stem>.txt next to
                     the input file.
        model: Gemini model identifier.
        silence_threshold: dB floor for silence detection (passed to trim_deadspace).
        min_silence_duration: Minimum silence length in seconds to remove.
        keep_edge_silence: Seconds of silence to preserve at cut boundaries.
        skip_trim: If True, skip the trim_deadspace step.

    Returns:
        Path to the written transcript file.
    """
    if not input_path.exists():
        sys.exit(f"Error: File not found: {input_path}")

    if output_path is None:
        output_path = input_path.with_suffix(".txt")

    temp_files: list[Path] = []

    try:
        # Step 1: extract audio (no-op for pure audio files)
        audio_path = extract_audio(input_path)
        if audio_path != input_path:
            temp_files.append(audio_path)

        # Step 2: trim dead space
        if skip_trim:
            trimmed_path = audio_path
        else:
            trimmed_path = trim_deadspace(
                audio_path,
                silence_threshold=silence_threshold,
                min_silence_duration=min_silence_duration,
                keep_edge_silence=keep_edge_silence,
            )
            if trimmed_path != audio_path:
                temp_files.append(trimmed_path)

        # Step 3: transcribe via Gemini API
        transcript = transcribe_file(trimmed_path, model=model)

    finally:
        for tmp in temp_files:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # Write output
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(transcript, encoding="utf-8")
    except OSError as e:
        sys.exit(f"Error: Cannot write transcript to '{output_path}': {e}")

    print(f"Transcript written to {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe any audio or video file to text using the Gemini API"
    )
    parser.add_argument("input_file", type=Path, help="Path to an audio or video file")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output .txt file path (default: <input_name>.txt)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--skip-trim",
        action="store_true",
        help="Skip the silence-trimming step",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=-35.0,
        metavar="DB",
        help="Silence threshold in dB for trimming (default: -35)",
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=1.5,
        metavar="SECONDS",
        help="Minimum silence duration to remove in seconds (default: 1.5)",
    )
    parser.add_argument(
        "--keep-edge",
        type=float,
        default=0.3,
        metavar="SECONDS",
        help="Seconds of silence to keep at cut boundaries (default: 0.3)",
    )
    args = parser.parse_args()

    run(
        input_path=args.input_file,
        output_path=args.output,
        model=args.model,
        silence_threshold=args.threshold,
        min_silence_duration=args.min_silence,
        keep_edge_silence=args.keep_edge,
        skip_trim=args.skip_trim,
    )


if __name__ == "__main__":
    main()
