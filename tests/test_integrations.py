"""Provider switching, credential migration and the Doubao request contract."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations import ModelSettings, call_model, public_config, update_config
from settings_store import SettingsStore


class ModelTests(unittest.TestCase):
    def test_switching_keeps_independent_credentials_and_hides_them(self) -> None:
        config = update_config(ModelSettings(), {"api_key": "deepseek-test"})
        config = update_config(config, {"provider": "doubao", "api_key": "doubao-test"})
        config = update_config(config, {"provider": "deepseek", "api_key": ""})
        self.assertEqual(config.connection.api_key, "deepseek-test")
        self.assertEqual(config.doubao.api_key, "doubao-test")
        public = json.dumps(public_config(config))
        self.assertNotIn("deepseek-test", public)
        self.assertNotIn("doubao-test", public)

    def test_doubao_uses_ark_chat_completions_and_selected_key(self) -> None:
        config = update_config(ModelSettings(), {"provider": "doubao", "api_key": "ark-test"})
        reply = {
            "choices": [{"message": {"content": '{"body":"春眠不觉晓"}'}, "finish_reason": "stop"}]
        }
        with patch("integrations.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(reply).encode())
            self.assertIn("春眠不觉晓", call_model(config, [{"role": "user", "content": "春晓"}]))
            request = opener.return_value.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer ark-test")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "doubao-seed-2-1-turbo-260628")
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_legacy_settings_migrate_only_model_fields(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder))
            stored = json.dumps(
                {
                    "api_key": "old-test",
                    "model": "deepseek-flash",
                    "base_url": "https://api.deepseek.com",
                    "smtp_password": "removed",
                }
            ).encode()
            with patch("settings_store.dpapi", side_effect=lambda content, **kwargs: content):
                store.path.write_bytes(stored)
                migrated = SettingsStore(Path(folder))
                migrated.save({})
                self.assertEqual(migrated.get().connection.api_key, "old-test")
                self.assertNotIn("smtp", store.path.read_text())


if __name__ == "__main__":
    unittest.main()
