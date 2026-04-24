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

        # websockets is a hard requirement; sounddevice is no longer used — we
        # capture mic via the native Swift helper bundled in Sentrial.app, which
        # lives at the path in SENTRIAL_MIC_HELPER and emits 16 kHz mono int16
        # PCM on its stdout.
        try:
            import websockets.sync.client as ws_client
        except ImportError as e:
            log.warning("websockets not installed: %s", e)
            self._on_error("websockets missing — pip install websockets")
            return

        import os as _os
        import subprocess as _sp
        helper = _os.environ.get("SENTRIAL_MIC_HELPER")
        if not helper or not _os.path.exists(helper):
            log.warning("SENTRIAL_MIC_HELPER missing: %r", helper)
            self._on_error(
                "Mic helper missing — rebuild Sentrial.app: ./scripts/build_app.sh"
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
                log.info("deepgram WS connected")
                recv_done = threading.Event()
                bytes_sent = [0]           # closed-over counter
                nonzero_bytes_sent = [0]   # mic-producing-audio counter
                msg_count = [0]

                def recv_loop() -> None:
                    try:
                        for raw in ws:
                            msg_count[0] += 1
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
                            mtype = msg.get("type")
                            if mtype != "Results":
                                # Non-Results frames: log type for debugging (Metadata,
                                # SpeechStarted, UtteranceEnd, Warning, Error, …).
                                if mtype in ("Error", "Warning"):
                                    log.warning("deepgram %s: %s", mtype, raw[:300])
                                else:
                                    log.debug("deepgram frame type=%s", mtype)
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
                            log.info("deepgram transcript (final=%s): %s", is_final, sentence[:120])
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

                # Spawn the Swift mic helper. Its stdout is 16 kHz mono int16
                # PCM, read in BLOCK_SIZE*2-byte chunks (50 ms at 16 kHz).
                chunk_bytes = BLOCK_SIZE * 2
                try:
                    proc = _sp.Popen(
                        [helper],
                        stdin=_sp.PIPE,
                        stdout=_sp.PIPE,
                        stderr=_sp.PIPE,
                        bufsize=0,
                    )
                except OSError as spawn_err:
                    log.warning("mic helper spawn failed: %s", spawn_err)
                    self._on_error(f"mic helper failed: {spawn_err}")
                    return

                # Drain stderr in a thread so its pipe buffer doesn't block.
                def stderr_loop():
                    try:
                        for line in proc.stderr:  # type: ignore[union-attr]
                            text = line.decode("utf-8", "replace").rstrip()
                            if text:
                                log.warning("sentrial-mic: %s", text)
                    except Exception:  # noqa: BLE001
                        pass
                threading.Thread(target=stderr_loop, daemon=True).start()

                chunk_count = [0]

                try:
                    while not self._stop.is_set():
                        if proc.stdout is None:
                            break
                        buf = proc.stdout.read(chunk_bytes)
                        if not buf:
                            # Helper exited early — permission denied or engine crash.
                            rc = proc.poll()
                            log.warning("mic helper exited (rc=%s) before stop", rc)
                            if rc == 2:
                                self._on_error(
                                    "Microphone permission denied — grant access to Sentrial "
                                    "in System Settings → Privacy → Microphone"
                                )
                            break
                        try:
                            ws.send(buf)
                        except Exception as send_err:  # noqa: BLE001
                            log.warning("deepgram send failed: %s", send_err)
                            break
                        bytes_sent[0] += len(buf)
                        peak = 0
                        for i in range(0, len(buf), 2):
                            s = buf[i] | (buf[i+1] << 8)
                            if s >= 0x8000:
                                s -= 0x10000
                            a = -s if s < 0 else s
                            if a > peak:
                                peak = a
                        if peak > 200:
                            nonzero_bytes_sent[0] += len(buf)
                        chunk_count[0] += 1
                        if chunk_count[0] % 40 == 0:
                            log.info(
                                "voice heartbeat: sent=%dB chunks=%d peak=%d frames_received=%d",
                                bytes_sent[0], chunk_count[0], peak, msg_count[0],
                            )
                finally:
                    # Close stdin to signal shutdown, then terminate if needed.
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        proc.wait(timeout=1.0)
                    except _sp.TimeoutExpired:
                        proc.terminate()
                        try:
                            proc.wait(timeout=1.0)
                        except _sp.TimeoutExpired:
                            proc.kill()

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
