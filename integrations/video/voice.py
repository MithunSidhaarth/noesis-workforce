"""Voiceover for reels. Providers, first configured wins (NOESIS_VOICE picks one):

  elevenlabs  ELEVENLABS_API_KEY (+ ELEVENLABS_VOICE_ID). Best voices; paid
              after the free monthly credits.
  gemini      GEMINI_API_KEY, Google's TTS model (NOESIS_TTS_MODEL,
              default gemini-2.5-flash-preview-tts). Free tier with rate limits.
  none        silent reel with captions and sound effects only.

Each line is spoken separately so the renderer knows exactly how long every
shot lasts. Audio-direction tags like [excited] are kept for ElevenLabs v3
models and turned into a style instruction for Gemini.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path

import httpx

from workforce.policies.engine import IntegrationError, IntegrationRevoked

TAG = re.compile(r"\[([^\]]{1,40})\]")


def plain(text: str) -> str:
    return " ".join(TAG.sub(" ", text).split())


class ElevenLabsVoice:
    name = "elevenlabs"

    def __init__(self, api_key: str, voice_id: str | None = None, model: str | None = None, transport=None):
        self.key, self.voice = api_key, voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or "JOUWtlfdRuffI74LLmML"
        self.model = model or os.environ.get("ELEVENLABS_MODEL") or "eleven_v3"
        self.client = httpx.Client(transport=transport, timeout=90)

    def say(self, text: str, out: Path) -> Path:
        tags_ok = self.model in ("eleven_v3", "eleven_v4")
        body = {"text": text if tags_ok else plain(text), "model_id": self.model}
        r = self.client.post(f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice}?output_format=mp3_44100_128",
                             headers={"xi-api-key": self.key}, json=body)
        if r.status_code in (401, 403):
            e = IntegrationRevoked(f"ElevenLabs refused the request (HTTP {r.status_code}): {r.text[:160]}")
            e.integration = "elevenlabs"  # type: ignore[attr-defined]
            raise e
        if r.status_code >= 300:
            raise IntegrationError(f"ElevenLabs TTS failed HTTP {r.status_code}: {r.text[:200]}")
        out.write_bytes(r.content)
        return out


class GeminiVoice:
    name = "gemini"

    def __init__(self, api_key: str, voice: str | None = None, model: str | None = None, transport=None):
        self.key = api_key
        self.voice = voice or os.environ.get("GEMINI_TTS_VOICE") or "Puck"
        self.model = model or os.environ.get("NOESIS_TTS_MODEL") or "gemini-2.5-flash-preview-tts"
        self.client = httpx.Client(transport=transport, timeout=90)

    def say(self, text: str, out: Path) -> Path:
        tags = ", ".join(TAG.findall(text)) or "energetic, punchy, like a top tech creator on Instagram"
        prompt = f"Say this as a fast, confident short-video narrator ({tags}): {plain(text)}"
        r = self.client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                             headers={"x-goog-api-key": self.key},
                             json={"contents": [{"parts": [{"text": prompt}]}],
                                   "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                                       "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}}})
        if r.status_code in (401, 403):
            raise IntegrationRevoked(f"Gemini refused the TTS request (HTTP {r.status_code})")
        if r.status_code >= 300:
            raise IntegrationError(f"Gemini TTS failed HTTP {r.status_code}: {r.text[:200]}")
        try:
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            pcm = base64.b64decode(part["data"])
        except (KeyError, IndexError, ValueError) as e:
            raise IntegrationError(f"Gemini TTS returned no audio: {r.text[:200]}") from e
        rate = int((re.search(r"rate=(\d+)", part.get("mimeType", "")) or [0, 24000])[1])
        p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                            "-c:a", "libmp3lame", "-b:a", "160k", str(out)], input=pcm, capture_output=True, timeout=60)
        if p.returncode != 0:
            raise IntegrationError(f"could not encode Gemini audio: {p.stderr[-200:]!r}")
        return out


def from_env():
    choice = (os.environ.get("NOESIS_VOICE") or "auto").lower()
    if choice in ("none", "off"):
        return None
    if choice in ("auto", "elevenlabs") and os.environ.get("ELEVENLABS_API_KEY"):
        return ElevenLabsVoice(os.environ["ELEVENLABS_API_KEY"])
    if choice in ("auto", "gemini") and os.environ.get("GEMINI_API_KEY"):
        return GeminiVoice(os.environ["GEMINI_API_KEY"])
    return None


def duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=30)
    try:
        return float(p.stdout.strip())
    except ValueError as e:
        raise IntegrationError(f"can't read audio length of {path.name}") from e
