"""Speech to text on the server, with faster-whisper, when it is installed.

Optional by design: without it the audit still runs, it just has no
transcript unless the platform supplied subtitles.
"""

from __future__ import annotations

import logging
import os
import threading
import wave
from pathlib import Path

log = logging.getLogger("post-audit")

_model = None
_model_lock = threading.Lock()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return os.environ.get("POST_AUDIT_TRANSCRIBE", "1") != "0"


def _load():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            # The Docker image downloads this model at build time into
            # WHISPER_MODEL_DIR, so a cold start never waits on the network.
            _model = WhisperModel(
                os.environ.get("WHISPER_MODEL", "base"),
                device="cpu",
                compute_type="int8",
                download_root=os.environ.get("WHISPER_MODEL_DIR") or None,
            )
    return _model


def _samples(audio: Path):
    """16 kHz mono PCM as float32, the form faster-whisper takes directly.

    Passing samples rather than a path skips faster-whisper's own decoder
    (PyAV), whose API has changed under it before; ffmpeg already produced
    exactly this format in media.extract_audio.
    """
    import numpy as np

    with wave.open(str(audio), "rb") as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("expected 16 kHz mono 16-bit audio")
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(audio: Path) -> str:
    """Return the spoken text, or "" if there is none or transcription fails."""
    if not available():
        return ""
    try:
        segments, _info = _load().transcribe(_samples(audio), vad_filter=True, beam_size=1)
        return " ".join(segment.text.strip() for segment in segments).strip()
    except Exception:  # noqa: BLE001 - a transcript is a bonus, never a reason to fail
        log.exception("transcription failed")
        return ""
