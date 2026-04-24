"""
Voice input — Deepgram Nova-3 streaming transcription from microphone audio.

Used by the menubar app. Holding Right-Option opens a VoiceSession; releasing it
calls .stop() which returns the finalized transcript.

Runs in a background thread. Audio is captured via sounddevice → 16-bit PCM 16 kHz
mono → streamed to Deepgram via WebSocket. Interim transcripts fire as the user
speaks; the final transcript accumulates as each utterance is finalized.

Fails gracefully if DEEPGRAM_API_KEY is missing, if deepgram-sdk or sounddevice
aren't installed, or if mic permission is denied.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 800  # ~50ms at 16kHz — low latency, low overhead

OnText = Callable[[str], None]
OnErr = Callable[[str], None]


class VoiceSession:
    """One listening session. Construct → start() → feed lifecycle → stop() → final text."""

    def __init__(
        self,
        api_key: str,
        on_interim: Optional[OnText] = None,
        on_final: Optional[OnText] = None,
        on_error: Optional[OnErr] = None,
        model: str = "nova-3",
        language: str = "en-US",
    ):
        self.api_key = api_key
        self.model = model
        self.language = language
        self._on_interim = on_interim or (lambda _t: None)
        self._on_final = on_final or (lambda _t: None)
        self._on_error = on_error or (lambda _e: None)
        self._stop = threading.Event()
        self._final_text = ""
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._final_text = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> str:
        """Signal the capture thread to end, wait up to 3s, return final transcript."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        return self._final_text.strip()

    # ------------------- internals -------------------

    def _run(self) -> None:
        try:
            from deepgram import (
                DeepgramClient,
                LiveOptions,
                LiveTranscriptionEvents,
            )
        except ImportError as e:
            log.warning("deepgram-sdk not installed: %s", e)
            self._on_error("deepgram-sdk not installed")
            return

        try:
            import sounddevice as sd
        except ImportError as e:
            log.warning("sounddevice not installed: %s", e)
            self._on_error("sounddevice not installed (pip install sounddevice)")
            return

        connection = None
        try:
            dg = DeepgramClient(self.api_key)
            connection = dg.listen.websocket.v("1")

            def on_message(_client, result, **_kw):
                try:
                    alt = result.channel.alternatives[0]
                    sentence = (alt.transcript or "").strip()
                except (AttributeError, IndexError):
                    return
                if not sentence:
                    return
                # Continuous mode: fire on_final per-utterance, not accumulated.
                # speech_final is stricter (endpointing-triggered) than is_final.
                if getattr(result, "speech_final", False) or getattr(result, "is_final", False):
                    self._final_text = sentence
                    self._on_final(sentence)
                else:
                    self._on_interim(sentence)

            def on_err(_client, error, **_kw):
                msg = str(error)[:300]
                log.warning("deepgram error: %s", msg)
                self._on_error(msg)

            connection.on(LiveTranscriptionEvents.Transcript, on_message)
            connection.on(LiveTranscriptionEvents.Error, on_err)

            opts = LiveOptions(
                model=self.model,
                language=self.language,
                encoding="linear16",
                channels=CHANNELS,
                sample_rate=SAMPLE_RATE,
                interim_results=True,
                punctuate=True,
                smart_format=True,
                endpointing=300,
            )
            if not connection.start(opts):
                self._on_error("deepgram failed to start")
                return

            def audio_cb(indata, _frames, _time, status):
                if status:
                    log.debug("mic status: %s", status)
                if self._stop.is_set():
                    raise sd.CallbackStop
                try:
                    connection.send(bytes(indata))
                except Exception as e:  # noqa: BLE001
                    log.warning("deepgram send failed: %s", e)

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCK_SIZE,
                callback=audio_cb,
            ):
                while not self._stop.wait(0.05):
                    pass
        except Exception as e:  # noqa: BLE001
            log.exception("voice session error")
            self._on_error(str(e)[:300])
        finally:
            if connection is not None:
                try:
                    connection.finish()
                except Exception:  # noqa: BLE001
                    pass
