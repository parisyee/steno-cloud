#!/usr/bin/env python3
"""Extract audio from video files using ffmpeg; pass audio files through unchanged."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".ts"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def extract_audio(input_path: Path, output_path: Path | None = None) -> Path:
    """Extract audio from a video file, or return the input path if already audio.

    Args:
        input_path: Path to the input audio or video file.
        output_path: Destination path for the extracted audio. If None, a temp
                     file is created. The caller is responsible for deleting it.

    Returns:
        Path to the audio file (may be a new temp file or the original input).
    """
    if not input_path.exists():
        sys.exit(f"Error: File not found: {input_path}")

    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg is not installed or not on PATH")

    if is_audio(input_path):
        # Already an audio file — return as-is (no temp file created).
        return input_path

    if not is_video(input_path):
        # Unknown extension — attempt extraction anyway; ffmpeg will error if unsupported.
        pass

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
        tmp.close()
        output_path = Path(tmp.name)

    print(f"Extracting audio from {input_path.name}...")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-vn",                  # no video
        "-acodec", "copy",      # copy audio stream if already encoded
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Try again with AAC re-encode in case copy fails (e.g. incompatible container)
        cmd_reencode = [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-vn",
            "-acodec", "aac",
            "-b:a", "192k",
            str(output_path),
        ]
        result2 = subprocess.run(cmd_reencode, capture_output=True, text=True)
        if result2.returncode != 0:
            sys.exit(f"Error: ffmpeg failed to extract audio:\n{result2.stderr}")

    print(f"Audio extracted to {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Extract audio from a video file")
    parser.add_argument("input_file", type=Path, help="Path to the input video or audio file")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output audio file path (default: <input_name>.m4a)",
    )
    args = parser.parse_args()

    output = args.output or args.input_file.with_suffix(".m4a")
    result = extract_audio(args.input_file, output)
    if result == args.input_file:
        print(f"{args.input_file} is already an audio file — no extraction needed.")


if __name__ == "__main__":
    main()
