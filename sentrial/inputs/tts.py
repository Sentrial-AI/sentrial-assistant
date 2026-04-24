"""
Text-to-speech playback for Voice Mode. Two paths:

1. Deepgram Aura-2 (REST) — human-sounding, low latency. Default: `aura-2-orion-en`.
2. macOS `say` fallback — works with no API key; voice "Daniel" by default (British, warm).

Blocks until playback ends. One utterance at a time — a new call stops any prior
playback first. Audio is written to a temp file and played via `afplay` for Aura,
or handed to `say` directly for the system path.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_VOICE = "aura-2-orion-en"       # Masculine, confident, crisp — Jarvis-ish.
DEFAULT_SYS_VOICE = "Daniel"            # macOS system fallback.
API_URL = "https://api.deepgram.com/v1/speak"

_current_proc: subprocess.Popen | None = None


def stop_playback() -> None:
    global _current_proc
    if _current_proc is not None:
        try:
            _current_proc.send_signal(signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
        _current_proc = None


def speak(text: str, api_key: Optional[str] = None, voice: str = DEFAULT_VOICE) -> None:
    text = (text or "").strip()
    if not text:
        return
    stop_playback()

    if api_key:
        try:
            _speak_aura(text, api_key, voice)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Aura TTS failed, falling back to `say`: %s", e)
    _speak_system(text)


def _speak_aura(text: str, api_key: str, voice: str) -> None:
    import requests
    params = {"model": voice}
    r = requests.post(
        API_URL,
        params=params,
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={"text": text},
        timeout=30,
        stream=True,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Aura {r.status_code}: {r.text[:200]}")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
        path = f.name

    try:
        _play_mp3(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _play_mp3(path: str) -> None:
    global _current_proc
    player = shutil.which("afplay")  # macOS
    if player is None:
        log.warning("no afplay available; skipping playback")
        return
    _current_proc = subprocess.Popen(
        [player, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _current_proc.wait()
    finally:
        _current_proc = None


def _speak_system(text: str, voice: str = DEFAULT_SYS_VOICE) -> None:
    if sys.platform != "darwin":
        log.info("TTS (text-only): %s", text)
        return
    say = shutil.which("say")
    if not say:
        log.info("TTS (text-only): %s", text)
        return
    global _current_proc
    _current_proc = subprocess.Popen(
        [say, "-v", voice, "-r", "205", text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _current_proc.wait()
    finally:
        _current_proc = None
