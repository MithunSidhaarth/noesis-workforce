"""Speech to text for voice notes (mock interviews, quick requests).

Uses Groq's hosted Whisper (OpenAI-compatible /audio/transcriptions) with the
GROQ_API_KEY you may already have for the model router's free tier. Read-only
processing of your own audio, like a model call: nothing is sent to anyone else.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"


class SttUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


def transcribe(path: str | Path, transport: httpx.BaseTransport | None = None, language: str | None = "en") -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise SttUnavailable("voice notes need GROQ_API_KEY in .env (free at console.groq.com)")
    p = Path(path)
    data = {"model": MODEL, "response_format": "json", "temperature": "0"}
    if language:
        data["language"] = language
    try:
        with httpx.Client(transport=transport, timeout=60) as c:
            r = c.post(URL, headers={"Authorization": f"Bearer {key}"}, data=data,
                       files={"file": (p.name, p.read_bytes(), "audio/ogg")})
    except httpx.HTTPError as e:
        raise SttUnavailable(f"transcription service unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise SttUnavailable(f"transcription failed (HTTP {r.status_code})")
    return str(r.json().get("text") or "").strip()
