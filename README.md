# steno

A command-line tool that transcribes audio and video files using the Google Gemini API. Supports multiple speakers, mixed languages, and a variety of formats. Before sending audio to the API, it optionally strips long silences to reduce file size and API costs.

## Architecture

The pipeline is made up of four scripts, each usable standalone or as an importable module:

- **`transcribe.py`** — orchestrator: runs the full pipeline and writes a `.txt` transcript
- **`extract_audio.py`** — extracts audio from video files; passes audio files through unchanged
- **`trim_deadspace.py`** — removes long silences and noise from audio using ffmpeg
- **`api_client.py`** — uploads audio to the Gemini Files API and returns the transcript

## Supported formats

**Audio:** mp3, wav, flac, ogg, m4a, aac, wma, opus

**Video:** mp4, mov, mkv, avi, webm, m4v, flv, wmv, ts

## Setup

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/), and [ffmpeg](https://ffmpeg.org/).

```bash
# Install ffmpeg (macOS)
brew install ffmpeg

# Create a virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt
```

### API key

Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

Set it as an environment variable:

```bash
export GEMINI_API_KEY=your-key-here
```

Or create a `.env` file in the project root:

```
GEMINI_API_KEY=your-key-here
```

## Usage

### Full pipeline (recommended)

```bash
# Transcribe an audio or video file (output defaults to <input_name>.txt)
uv run python transcribe.py recording.m4a

# Specify output path
uv run python transcribe.py recording.m4a -o transcripts/meeting.txt

# Use a specific Gemini model
uv run python transcribe.py recording.m4a --model gemini-2.5-pro

# Skip the silence-trimming step
uv run python transcribe.py recording.m4a --skip-trim

# Tune silence detection (more aggressive: lower threshold, shorter min duration)
uv run python transcribe.py recording.m4a --threshold -40 --min-silence 1.0

# Transcribe a video file directly
uv run python transcribe.py screen_recording.mp4 -o notes.txt
```

**Options:**

| Option | Default | Description |
|---|---|---|
| `input_file` | — | Path to any audio or video file (required) |
| `-o`, `--output` | `<input>.txt` | Output transcript path |
| `--model` | `gemini-2.5-flash` | Gemini model to use |
| `--skip-trim` | off | Skip silence removal |
| `--threshold DB` | `-35` | Silence floor in dB; lower = more aggressive |
| `--min-silence SECONDS` | `1.5` | Minimum silence duration before removal |
| `--keep-edge SECONDS` | `0.3` | Silence to preserve at cut boundaries |

---

### Individual scripts

Each script also runs standalone if you want to use just one stage.

**Extract audio from a video:**
```bash
uv run python extract_audio.py clip.mp4 -o audio.m4a
```

**Remove silences from an audio file:**
```bash
uv run python trim_deadspace.py recording.m4a -o recording_trimmed.m4a
uv run python trim_deadspace.py recording.m4a --threshold -40 --min-silence 1.0
```
Prints file size before and after so you can see the reduction.

**Transcribe an audio file directly (no extraction or trimming):**
```bash
uv run python api_client.py audio.m4a
uv run python api_client.py audio.m4a -o transcript.txt --model gemini-2.5-pro
```

---

## Temp file handling

When `transcribe.py` runs the full pipeline, intermediate files are created for each processing stage — one after audio extraction (if the input is a video) and one after silence trimming. These are never written to your working directory.

**Where they live:** Both temp files are created via Python's `tempfile.NamedTemporaryFile`, which places them in the OS temp directory (`/tmp` on macOS/Linux). They are named with a random suffix (e.g. `/tmp/tmpXXXXXX.m4a`).

**When they're deleted:** Cleanup runs in a `finally` block, so temp files are removed whether the run succeeds, fails, or is interrupted mid-pipeline. Only files that were actually created as intermediates are tracked and deleted — the original input is never touched, and any file you provide as an explicit `--output` path is kept.

**What's never deleted:** The final `.txt` transcript, the original input file, and any audio file passed directly to `api_client.py` or `trim_deadspace.py` as a standalone command (those scripts don't do their own cleanup — the caller decides).
