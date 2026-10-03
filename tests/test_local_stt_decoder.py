"""Catch decoder API incompatibilities that mocked transcription tests miss."""
import wave

import pytest


def test_local_stt_decodes_audio_with_installed_pyav(tmp_path):
    decoder = pytest.importorskip("faster_whisper.audio")
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 1600)
    audio = decoder.decode_audio(str(path))
    assert len(audio) == 1600
    assert not audio.any()
