# steno

A command-line tool that transcribes audio files using the Google Gemini API. Supports multiple speakers, mixed languages, and a variety of audio/video formats.

## Supported formats

mp3, wav, flac, ogg, m4a, aac, wma, opus, webm, mp4

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
# Create a virtual environment
uv venv

# Install dependencies
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

```bash
# Print transcript to stdout
uv run python transcribe.py recording.mp3

# Save transcript to a file
uv run python transcribe.py recording.mp3 -o transcript.txt

# Use a specific model
uv run python transcribe.py recording.mp3 --model gemini-2.5-pro

# Output directories are created automatically
uv run python transcribe.py recording.mp3 -o output/transcripts/result.txt
```

### Options

| Option | Description |
|---|---|
| `audio_file` | Path to the audio file (required) |
| `-o`, `--output` | Write transcript to a file instead of stdout |
| `--model` | Gemini model to use (default: `gemini-2.5-flash`) |
