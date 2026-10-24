from openrig_matrix_bridge.transcription import (
    MODEL_SHA256,
    audio_suffix,
    model_directory,
    model_ready,
)


def test_audio_suffix_is_preserved():
    assert audio_suffix("voice.ogg") == ".ogg"
    assert audio_suffix("Voice.MP4") == ".mp4"


def test_audio_suffix_falls_back_for_unsafe_name():
    assert audio_suffix("voice") == ".ogg"
    assert audio_suffix("voice.superlongsuffix") == ".ogg"


def test_model_state_checks_all_required_files(tmp_path):
    model = model_directory(tmp_path)
    model.mkdir()
    assert model_ready(tmp_path) is False
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (model / name).touch()
    assert model_ready(tmp_path) is True


def test_parakeet_checksum_is_pinned():
    assert len(MODEL_SHA256) == 64
