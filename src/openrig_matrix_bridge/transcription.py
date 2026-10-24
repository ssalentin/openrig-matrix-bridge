from __future__ import annotations

import hashlib
import logging
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
import wave
from pathlib import Path

import numpy as np

from .config import TranscriptionConfig

LOGGER = logging.getLogger("openrig-matrix-bridge.transcription")

MODEL_NAME = "parakeet-tdt-0.6b-v3"
MODEL_DIR_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MODEL_ARCHIVE = f"{MODEL_DIR_NAME}.tar.bz2"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_ARCHIVE}"
)
MODEL_SHA256 = "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf"
MODEL_FILES = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "joiner.int8.onnx",
    "tokens.txt",
)


def audio_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix and len(suffix) <= 8 else ".ogg"


def model_directory(download_root: Path) -> Path:
    return download_root / MODEL_DIR_NAME


def model_ready(download_root: Path) -> bool:
    directory = model_directory(download_root)
    return all((directory / name).is_file() for name in MODEL_FILES)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(download_root: Path) -> Path:
    destination = model_directory(download_root)
    if model_ready(download_root):
        return destination

    download_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = download_root / MODEL_ARCHIVE
    if not archive.is_file() or _sha256(archive) != MODEL_SHA256:
        temporary = archive.with_suffix(archive.suffix + ".part")
        LOGGER.info("downloading NVIDIA Parakeet model (about 465 MB)")
        with (
            urllib.request.urlopen(MODEL_URL, timeout=60) as response,
            temporary.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        actual = _sha256(temporary)
        if actual != MODEL_SHA256:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Parakeet download checksum mismatch: {actual}")
        temporary.replace(archive)

    with tarfile.open(archive, "r:bz2") as bundle:
        bundle.extractall(download_root, filter="data")
    archive.unlink(missing_ok=True)
    if not model_ready(download_root):
        raise RuntimeError("Parakeet model archive did not contain all required files")
    return destination


class Transcriber:
    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self._recognizer = None
        self._model_lock = threading.Lock()

    def _get_recognizer(self):
        if not self.config.enabled:
            raise RuntimeError("audio transcription is disabled")
        if self._recognizer is not None:
            return self._recognizer
        with self._model_lock:
            if self._recognizer is None:
                import sherpa_onnx

                model = ensure_model(self.config.download_root)
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(model / "encoder.int8.onnx"),
                    decoder=str(model / "decoder.int8.onnx"),
                    joiner=str(model / "joiner.int8.onnx"),
                    tokens=str(model / "tokens.txt"),
                    num_threads=self.config.num_threads,
                    provider="cpu",
                    model_type="nemo_transducer",
                    decoding_method="greedy_search",
                    debug=False,
                )
        return self._recognizer

    def transcribe(self, audio: bytes, filename: str) -> str:
        suffix = audio_suffix(filename)
        with tempfile.TemporaryDirectory(prefix="openrig-parakeet-") as temp_dir:
            source = Path(temp_dir) / f"source{suffix}"
            wav = Path(temp_dir) / "audio.wav"
            source.write_bytes(audio)
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-sample_fmt",
                    "s16",
                    str(wav),
                ],
                check=True,
            )
            with wave.open(str(wav), "rb") as handle:
                samples = (
                    np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
                    .astype(np.float32)
                    / 32768.0
                )
            recognizer = self._get_recognizer()
            stream = recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            recognizer.decode_stream(stream)
            return str(stream.result.text).strip()
