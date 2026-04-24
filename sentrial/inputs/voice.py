"""
Voice input — Deepgram Nova-3 streaming transcription from microphone audio.

Used by the menubar app. Holding Right-Option opens a VoiceSession; releasing it
calls .stop() which returns the finalized transcript.

Runs in a background thread. Audio is captured via sounddevice → 16-bit PCM 16 kHz
mono → streamed to Deepgram via WebSocket. Interim transcripts fire as the user
speaks; the final transcript accumulates as each utterance is finalized.

Fails gracefully if DEEPGRAM_API_KEY is missing, if deepgram-sdk or sounddevice
aren't installed, or if mic permission is denied.

Talks directly to Deepgram's /v1/listen WebSocket — we don't use deepgram-sdk's
connect() because 6.0.2's Fern-generated URL builder serializes Python booleans
as capital "True"/"False", which Deepgram rejects as HTTP 400. `deepgram-sdk` is
still imported only to fail-fast when missing; actual transport is `websockets`.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)


def _ensure_ssl_certs() -> None:
    """Belt-and-suspenders: point SSL at certifi if not already configured."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
        bundle = certifi.where()
    except Exception:  # noqa: BLE001
        return
    os.environ["SSL_CERT_FILE"] = bundle
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)

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
        _ensure_ssl_certs()

        # websockets + sounddevice are hard requirements; fail fast if missing.
        try:
            import websockets.sync.client as ws_client
        except ImportError as e:
            log.warning("websockets not installed: %s", e)
            self._on_error("websockets missing — pip install websockets")
            return

        try:
            import sounddevice as sd
        except ImportError as e:
            log.warning("sounddevice not installed: %s", e)
            self._on_error(
                "sounddevice missing — run: ./.venv/bin/pip install sounddevice"
            )
            return

        import json as _json
        from urllib.parse import urlencode

        # Build the Deepgram /v1/listen query. Booleans must be lowercase "true"/
        # "false" — passing Python bools through urlencode yields "True" which
        # Deepgram rejects as HTTP 400 (and deepgram-sdk 6.0.2 has this bug).
        params = {
            "model": self.model,
            "language": self.language,
            "encoding": "linear16",
            "channels": CHANNELS,
            "sample_rate": SAMPLE_RATE,
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "endpointing": 300,
        }
        url = "wss://api.deepgram.com/v1/listen?" + urlencode(params)
        headers = {"Authorization": f"Token {self.api_key}"}

        try:
            with ws_client.connect(url, additional_headers=headers) as ws:
                recv_done = threading.Event()

                def recv_loop() -> None:
                    try:
                        for raw in ws:
                            if not isinstance(raw, (str, bytes)):
                                continue
                            if isinstance(raw, bytes):
                                try:
                                    raw = raw.decode("utf-8", "replace")
                                except Exception:  # noqa: BLE001
                                    continue
                            try:
                                msg = _json.loads(raw)
                            except ValueError:
                                continue
                            # Only care about the Results frames — skip Metadata/
                            # UtteranceEnd/SpeechStarted for now.
                            if msg.get("type") != "Results":
                                continue
                            try:
                                sentence = (
                                    msg["channel"]["alternatives"][0].get("transcript") or ""
                                ).strip()
                            except (KeyError, IndexError, TypeError):
                                continue
                            if not sentence:
                                continue
                            is_final = bool(msg.get("is_final")) or bool(msg.get("speech_final"))
                            if is_final:
                                self._final_text = sentence
                                self._on_final(sentence)
                            else:
                                self._on_interim(sentence)
                    except Exception as recv_err:  # noqa: BLE001
                        if not self._stop.is_set():
                            log.warning("deepgram recv error: %s", recv_err)
                    finally:
                        recv_done.set()

                recv_thread = threading.Thread(target=recv_loop, daemon=True)
                recv_thread.start()

                def audio_cb(indata, _frames, _time, status) -> None:
                    if status:
                        log.debug("mic status: %s", status)
                    if self._stop.is_set():
                        raise sd.CallbackStop
                    try:
                        # indata is a cffi buffer (RawInputStream); bytes() copies it out.
                        ws.send(bytes(indata))
                    except Exception as send_err:  # noqa: BLE001
                        log.warning("deepgram send failed: %s", send_err)

                # RawInputStream avoids sounddevice's numpy dependency by handing
                # us a plain CFFI buffer instead of an ndarray.
                with sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    blocksize=BLOCK_SIZE,
                    callback=audio_cb,
                ):
                    while not self._stop.wait(0.05):
                        pass

                # Tell Deepgram we're done so it flushes remaining transcripts.
                try:
                    ws.send(_json.dumps({"type": "CloseStream"}))
                except Exception:  # noqa: BLE001
                    pass

                # Wait for the receiver to drain (up to 3s).
                recv_done.wait(timeout=3.0)

        except Exception as e:  # noqa: BLE001
            log.exception("voice session error")
            self._on_error(str(e)[:300])
