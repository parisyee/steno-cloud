#!/usr/bin/env python3
"""Orchestrate audio/video transcription: extract → trim silence → transcribe via Gemini API.

Two-pass pipeline:
  1. Pro model produces a verbatim transcript with speaker labels and whitespace.
  2. Flash model derives a title, description, and cleaned variants from that transcript.

Writes a JSON sidecar (`<input>.json`) alongside the `.txt` transcript so the polished
versions and metadata are available without re-running the pipeline.
"""

import argparse
import json
import sys
from pathlib import Path

from transcription_service.gemini_client import (
    DEFAULT_ANALYZE_MODEL,
    DEFAULT_TRANSCRIBE_MODEL,
    analyze_transcript,
    transcribe_raw,
)
from transcription_service.extract_audio import extract_audio
from transcription_service.trim_deadspace import trim_deadspace


def run(
    input_path: Path,
    output_path: Path | None = None,
    transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL,
    analyze_model: str = DEFAULT_ANALYZE_MODEL,
    silence_threshold: float = -35.0,
    min_silence_duration: float = 1.5,
    keep_edge_silence: float = 0.3,
    skip_trim: bool = False,
    skip_analysis: bool = False,
    lock: bool = False,
) -> Path:
    """Extract audio, optionally trim silence, transcribe, analyze, and write output files.

    Returns the path to the written `.txt` transcript file. When analysis is enabled,
    a `.json` sidecar with title / description / cleaned variants is written alongside.
    """
    if not input_path.exists():
        sys.exit(f"Error: File not found: {input_path}")

    if output_path is None:
        output_path = input_path.with_suffix(".txt")

    temp_files: list[Path] = []

    try:
        audio_path = extract_audio(input_path)
        if audio_path != input_path:
            temp_files.append(audio_path)

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

        transcript = transcribe_raw(trimmed_path, model=transcribe_model)
        analysis = None
        if not skip_analysis:
            analysis = analyze_transcript(transcript, model=analyze_model)

    finally:
        for tmp in temp_files:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(transcript, encoding="utf-8")
        if lock:
            output_path.chmod(0o444)
    except OSError as e:
        sys.exit(f"Error: Cannot write transcript to '{output_path}': {e}")

    print(f"Transcript written to {output_path}")

    if analysis is not None:
        sidecar_path = output_path.with_suffix(".json")
        payload = {
            "title": analysis.title,
            "description": analysis.description,
            "transcript": transcript,
            "cleaned": {
                "light": analysis.cleaned_light,
                "polished": analysis.cleaned_polished,
            },
        }
        try:
            sidecar_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if lock:
                sidecar_path.chmod(0o444)
            print(f"Analysis written to {sidecar_path}")
        except OSError as e:
            sys.exit(f"Error: Cannot write analysis to '{sidecar_path}': {e}")

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
        help="Output .txt file path (default: <input_name>.txt). A .json sidecar is "
             "written alongside unless --skip-analysis is set.",
    )
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
        "--lock",
        action="store_true",
        help="Make the output files read-only after writing",
    )
    parser.add_argument(
        "--skip-trim",
        action="store_true",
        help="Skip the silence-trimming step",
    )
    parser.add_argument(
        "--skip-analysis",
        action="store_true",
        help="Skip the second-pass analysis (no title, description, or cleaned versions)",
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
        transcribe_model=args.transcribe_model,
        analyze_model=args.analyze_model,
        silence_threshold=args.threshold,
        min_silence_duration=args.min_silence,
        keep_edge_silence=args.keep_edge,
        skip_trim=args.skip_trim,
        skip_analysis=args.skip_analysis,
        lock=args.lock,
    )


if __name__ == "__main__":
    main()
