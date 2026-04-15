#!/usr/bin/env python3
"""Remove long silences and incomprehensible noise from an audio file using ffmpeg."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def trim_deadspace(
    input_path: Path,
    output_path: Path | None = None,
    silence_threshold: float = -35.0,
    min_silence_duration: float = 1.5,
    keep_edge_silence: float = 0.3,
) -> Path:
    """Remove long silences from an audio file.

    Args:
        input_path: Path to the input audio file.
        output_path: Destination path for cleaned audio. If None, a temp file is
                     created. The caller is responsible for deleting it.
        silence_threshold: Noise floor in dB below which audio is considered
                           silence (default: -35 dB).
        min_silence_duration: Minimum consecutive seconds of silence to remove
                              (default: 1.5 s). Shorter pauses are left intact.
        keep_edge_silence: Seconds of silence to leave at the start/end of each
                           retained segment so cuts don't feel abrupt (default: 0.3 s).

    Returns:
        Path to the cleaned audio file (may be a new temp file).
    """
    if not input_path.exists():
        sys.exit(f"Error: File not found: {input_path}")

    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg is not installed or not on PATH")

    if output_path is None:
        suffix = input_path.suffix or ".m4a"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.close()
        output_path = Path(tmp.name)

    # silenceremove filter:
    #   stop_periods=-1  → remove silences throughout the file (not just at edges)
    #   stop_threshold   → dB level considered silence
    #   stop_duration    → seconds that must be silent before removal kicks in
    #   stop_silence     → seconds of silence to keep at cut points (replaces "leave" in ffmpeg 8+)
    silence_filter = (
        f"silenceremove="
        f"stop_periods=-1:"
        f"stop_threshold={silence_threshold}dB:"
        f"stop_duration={min_silence_duration}:"
        f"stop_silence={keep_edge_silence}"
    )

    print(f"Trimming dead space from {input_path.name}...")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-af", silence_filter,
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"Error: ffmpeg failed to trim silence:\n{result.stderr}")

    original_mb = input_path.stat().st_size / 1_048_576
    trimmed_mb = output_path.stat().st_size / 1_048_576
    saved_pct = max(0.0, (1 - trimmed_mb / original_mb) * 100) if original_mb else 0.0
    print(
        f"Trimmed audio written to {output_path} "
        f"({original_mb:.1f} MB → {trimmed_mb:.1f} MB, {saved_pct:.0f}% reduction)"
    )
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Remove long silences from an audio file to reduce size and API costs"
    )
    parser.add_argument("input_file", type=Path, help="Path to the input audio file")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output file path (default: <input_name>_trimmed<ext>)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=-35.0,
        help="Silence threshold in dB (default: -35). Lower = more aggressive.",
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

    output = args.output or args.input_file.with_stem(args.input_file.stem + "_trimmed")
    trim_deadspace(
        args.input_file,
        output,
        silence_threshold=args.threshold,
        min_silence_duration=args.min_silence,
        keep_edge_silence=args.keep_edge,
    )


if __name__ == "__main__":
    main()
