from openrig_matrix_bridge.transcription import audio_suffix


def test_audio_suffix_is_preserved():
    assert audio_suffix("voice.ogg") == ".ogg"
    assert audio_suffix("Voice.MP4") == ".mp4"


def test_audio_suffix_falls_back_for_unsafe_name():
    assert audio_suffix("voice") == ".ogg"
    assert audio_suffix("voice.superlongsuffix") == ".ogg"
