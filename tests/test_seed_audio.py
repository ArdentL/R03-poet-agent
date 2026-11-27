"""Speech credential compatibility and upstream error regressions."""

import base64
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import wave
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seed_audio
from integrations import IntegrationError


def wav_fixture(frames: bytes, *, channels: int = 1, width: int = 2, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(frames)
    return buffer.getvalue()


class SpeechTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.app = root / "app"
        self.app.mkdir()
        (root / "sdk").mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(seed_audio, "__file__", str(self.app / "seed_audio.py")).start()

    def configure(self, text: str) -> None:
        (self.app / ".env").write_text(text, encoding="utf-8")

    def test_legacy_audio_key_is_loaded(self) -> None:
        self.configure("DOUBAO_AUDIO_API_KEY=legacy-speech-key\n")
        self.assertEqual(seed_audio.configured_key(), "legacy-speech-key")

    def test_speech_key_has_priority_over_alias_and_ark(self) -> None:
        self.configure(
            "DOUBAO_SPEECH_API_KEY=current-speech-key\n"
            "DOUBAO_AUDIO_API_KEY=legacy-speech-key\n"
            "ARK_API_KEY=ark-key\n"
        )
        self.assertEqual(seed_audio.configured_key(), "current-speech-key")

    def test_environment_alias_has_priority_over_ark(self) -> None:
        os.environ["DOUBAO_AUDIO_API_KEY"] = "environment-speech-key"
        self.configure("ARK_API_KEY=ark-key\n")
        self.assertEqual(seed_audio.configured_key(), "environment-speech-key")

    def test_explicit_env_file_and_utf8_bom_are_supported(self) -> None:
        config = Path(self.folder.name) / "audio.env"
        config.write_text(
            'export DOUBAO_SPEECH_API_KEY="explicit-speech-key"\n',
            encoding="utf-8-sig",
        )
        os.environ["POET_AUDIO_ENV_FILE"] = str(config)
        self.configure("DOUBAO_SPEECH_API_KEY=local-speech-key\n")
        self.assertEqual(seed_audio.configured_key(), "explicit-speech-key")

    def test_existing_sibling_sdk_config_is_supported(self) -> None:
        (self.app.parent / "sdk" / ".env").write_text(
            "DOUBAO_SPEECH_API_KEY=sibling-speech-key\n",
            encoding="utf-8",
        )
        self.assertEqual(seed_audio.configured_key(), "sibling-speech-key")

    def test_existing_ark_fallback_is_preserved(self) -> None:
        self.configure("ARK_API_KEY=historical-key\n")
        self.assertEqual(seed_audio.configured_key(), "historical-key")

    def upstream_error(self, status: int, body: bytes) -> str:
        self.configure("DOUBAO_SPEECH_API_KEY=test-key\n")
        headers = Message()
        headers["X-Tt-Logid"] = "test-log-123"
        error = HTTPError(
            seed_audio.ENDPOINT,
            status,
            "upstream error",
            headers,
            io.BytesIO(body),
        )
        with patch.object(seed_audio, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(IntegrationError) as failure:
                seed_audio.synthesize("春眠不觉晓。")
        return str(failure.exception)

    def test_resource_permission_failure_is_identified(self) -> None:
        message = self.upstream_error(
            403,
            b'{"code":45000030,"message":"[resource_id=volc.service_type.10074] requested resource not granted"}',
        )
        self.assertIn("45000030", message)
        self.assertIn("开通管理", message)
        self.assertIn("test-log-123", message)
        self.assertNotIn("鉴权失败", message)

    def test_invalid_key_is_identified(self) -> None:
        message = self.upstream_error(401, b'{"code":45000010,"message":"Invalid X-Api-Key"}')
        self.assertIn("DOUBAO_SPEECH_API_KEY", message)
        self.assertIn("45000010", message)
        self.assertIn("401", message)

    def test_non_json_forbidden_response_stays_actionable(self) -> None:
        message = self.upstream_error(403, b"<html>Forbidden</html>")
        self.assertIn("403", message)
        self.assertIn("权限", message)
        self.assertNotIn("<html>", message)

    def test_generation_uses_speech_protocol_and_decodes_audio(self) -> None:
        self.configure("DOUBAO_SPEECH_API_KEY=test-key\n")
        audio = wav_fixture(struct.pack("<hhh", -1200, 0, 1200))
        response = io.BytesIO(
            json.dumps(
                {
                    "audio": base64.b64encode(audio).decode("ascii"),
                    "duration": 1.0,
                    "original_duration": 1.0,
                    "url": "https://example.invalid/sample.wav",
                }
            ).encode("utf-8")
        )
        with patch.object(seed_audio, "build_opener") as opener:
            opener.return_value.open.return_value = response
            result = seed_audio.synthesize("春眠不觉晓。")
            request = opener.return_value.open.call_args.args[0]
        self.assertEqual(result, audio)
        self.assertEqual(request.full_url, "https://openspeech.bytedance.com/api/v3/tts/create")
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(
            json.loads(request.data),
            {
                "model": "seed-audio-1.0",
                "text_prompt": "春眠不觉晓。",
                "audio_config": {"format": "wav", "sample_rate": 24000},
            },
        )

    def synthesize_response(self, audio: bytes) -> bytes:
        self.configure("DOUBAO_SPEECH_API_KEY=test-key\n")
        response = io.BytesIO(
            json.dumps(
                {
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            ).encode("utf-8")
        )
        with patch.object(seed_audio, "build_opener") as opener:
            opener.return_value.open.return_value = response
            return seed_audio.synthesize("春眠不觉晓。")

    def test_stereo_wav_is_mixed_to_mono_for_robot(self) -> None:
        audio = wav_fixture(
            struct.pack("<hhhhhh", 1000, 3000, -3000, -1000, 32767, -32768), channels=2
        )
        result = self.synthesize_response(audio)
        with wave.open(io.BytesIO(result), "rb") as output:
            self.assertEqual(output.getparams()[:4], (1, 2, 24000, 3))
            self.assertEqual(output.readframes(3), struct.pack("<hhh", 2000, -2000, -1))

    def test_mp3_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(IntegrationError, "WAV"):
            self.synthesize_response(b"ID3test-audio")

    def test_8_bit_wav_is_rejected(self) -> None:
        with self.assertRaisesRegex(IntegrationError, "WAV"):
            self.synthesize_response(wav_fixture(b"\x80\x81", width=1))

    def test_unexpected_sample_rate_is_rejected(self) -> None:
        with self.assertRaisesRegex(IntegrationError, "WAV"):
            self.synthesize_response(wav_fixture(b"\x00\x00", rate=44100))

    def test_truncated_wav_is_rejected(self) -> None:
        audio = wav_fixture(struct.pack("<hhh", -1200, 0, 1200))
        with self.assertRaisesRegex(IntegrationError, "WAV"):
            self.synthesize_response(audio[:-2])

    def test_empty_wav_is_rejected(self) -> None:
        with self.assertRaisesRegex(IntegrationError, "WAV"):
            self.synthesize_response(wav_fixture(b""))


if __name__ == "__main__":
    unittest.main()
