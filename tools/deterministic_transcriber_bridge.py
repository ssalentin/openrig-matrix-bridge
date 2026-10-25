"""Run the Matrix bridge with a deterministic local transcriber for integration."""

import os
import time

from openrig_matrix_bridge.bridge import Transcriber
from openrig_matrix_bridge.cli import main


def deterministic_transcribe(self, audio: bytes, filename: str) -> str:
    if not audio:
        raise RuntimeError("deterministic transcriber received empty audio")
    # Injected latency, so later events arrive while transcription is in flight.
    time.sleep(float(os.environ.get("OPENRIG_FAKE_TRANSCRIBE_SECONDS", "0") or "0"))
    return "Deterministic local transcript"


Transcriber.transcribe = deterministic_transcribe

if __name__ == "__main__":
    main()
