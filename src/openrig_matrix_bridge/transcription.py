from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from .config import TranscriptionConfig


def audio_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix and len(suffix) <= 8 else ".ogg"


class Transcriber:
    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self._model = None
        self._model_lock = threading.Lock()

    def _get_model(self):
        if not self.config.enabled:
            raise RuntimeError("audio transcription is disabled")
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self.config.download_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._model = WhisperModel(
                    self.config.model,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.config.download_root),
                )
        return self._model

    def transcribe(self, audio: bytes, filename: str) -> str:
        suffix = audio_suffix(filename)
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
            handle.write(audio)
            handle.flush()
            segments, _info = self._get_model().transcribe(
                handle.name,
                language=self.config.language,
                vad_filter=True,
                beam_size=5,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()

