#!/usr/bin/env python3
"""Split long audio files into chunks aligned to silence boundaries.

Long single-shot transcriptions of audio with `temperature=0` are prone to
repetition loops in the model output. Splitting the input into ~10-minute
windows (cut at natural silences when possible) lets each Gemini call stay
in a regime where greedy decoding behaves.

The chunker returns a list of `(path, start_offset_seconds)` tuples in
chronological order. The caller is responsible for deleting the chunk
files after use.
"""

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from transcription_service.extract_audio import AudioProcessingError
except ImportError:
    from extract_audio import AudioProcessingError

logger = logging.getLogger(__name__)

# Target chunk length and the hard upper bound. We try to land each cut at a
# silence inside [target - SEARCH_BACK, max] so chunks come out ~10 min on
# average without ever exceeding ~15 min.
TARGET_CHUNK_SECONDS = 600.0
MAX_CHUNK_SECONDS = 900.0
SEARCH_BACK_SECONDS = 120.0

# Silence detection params for boundary finding. Looser than trim_deadspace's
# defaults because we only need *some* break to align to.
SILENCE_DB = -30.0
SILENCE_DUR = 0.5

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def get_audio_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe."""
    if not shutil.which("ffprobe"):
        raise AudioProcessingError("ffprobe is not installed or not on PATH")
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise AudioProcessingError(
            f"ffprobe failed to read duration for {path.name}:\n{result.stderr}"
        )
    return float(result.stdout.strip())


def _detect_silences(path: Path) -> list[tuple[float, float]]:
    """Return list of (silence_start, silence_end) in seconds via ffmpeg silencedetect."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_DUR}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # silencedetect always exits 0 and writes to stderr; tolerate non-zero too.
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(result.stderr)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(result.stderr)]
    # Pair them up by index; trailing unmatched starts are silences that run to EOF.
    pairs = list(zip(starts, ends))
    return pairs


def _plan_cut_points(duration: float, silences: list[tuple[float, float]]) -> list[float]:
    """Pick cut points so each chunk is at most MAX_CHUNK_SECONDS, prefer silence midpoints."""
    cut_points = [0.0]
    while duration - cut_points[-1] > MAX_CHUNK_SECONDS:
        last = cut_points[-1]
        target = last + TARGET_CHUNK_SECONDS
        window_lo = last + (TARGET_CHUNK_SECONDS - SEARCH_BACK_SECONDS)
        window_hi = last + MAX_CHUNK_SECONDS
        # Candidate cuts: midpoints of silences whose midpoint sits inside the window.
        candidates = [
            (s + e) / 2.0 for (s, e) in silences
            if window_lo <= (s + e) / 2.0 <= window_hi
        ]
        if candidates:
            cut = min(candidates, key=lambda c: abs(c - target))
        else:
            # No usable silence — hard cut at the upper bound to keep chunks bounded.
            cut = window_hi
            logger.warning(
                "chunk_audio: no silence in [%.1f, %.1f]s, hard-cutting at %.1f",
                window_lo, window_hi, cut,
            )
        cut_points.append(cut)
    cut_points.append(duration)
    return cut_points


def chunk_audio(
    audio_path: Path,
    *,
    long_threshold_seconds: float = MAX_CHUNK_SECONDS,
) -> list[tuple[Path, float]]:
    """Split `audio_path` into silence-aligned chunks.

    Returns a list of `(chunk_path, start_offset_seconds)` tuples. If the
    audio is shorter than `long_threshold_seconds`, returns
    `[(audio_path, 0.0)]` and creates no temp files.

    Caller must delete any returned chunk files that are not `audio_path`.
    """
    if not audio_path.exists():
        raise AudioProcessingError(f"File not found: {audio_path}")
    if not shutil.which("ffmpeg"):
        raise AudioProcessingError("ffmpeg is not installed or not on PATH")

    duration = get_audio_duration(audio_path)
    if duration <= long_threshold_seconds:
        logger.info("chunk_audio: %.1fs <= %.1fs, no chunking", duration, long_threshold_seconds)
        return [(audio_path, 0.0)]

    logger.info("chunk_audio: %.1fs duration, detecting silences", duration)
    silences = _detect_silences(audio_path)
    logger.info("chunk_audio: found %d silence regions", len(silences))

    cut_points = _plan_cut_points(duration, silences)
    logger.info(
        "chunk_audio: %d chunks, boundaries=%s",
        len(cut_points) - 1,
        [f"{p:.1f}" for p in cut_points],
    )

    chunks: list[tuple[Path, float]] = []
    suffix = audio_path.suffix or ".m4a"
    for i in range(len(cut_points) - 1):
        start = cut_points[i]
        end = cut_points[i + 1]
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.close()
        chunk_path = Path(tmp.name)
        # Accurate seek (-ss after -i) + AAC re-encode keeps boundaries precise
        # and avoids container/codec edge cases that arise with stream copy.
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-vn",
            "-c:a", "aac", "-b:a", "128k",
            str(chunk_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Clean up anything we already wrote so we don't leak temp files.
            for p, _ in chunks:
                if p != audio_path:
                    p.unlink(missing_ok=True)
            chunk_path.unlink(missing_ok=True)
            raise AudioProcessingError(
                f"ffmpeg failed to cut chunk {i} [{start:.1f}-{end:.1f}s]:\n{result.stderr}"
            )
        chunks.append((chunk_path, start))

    return chunks
