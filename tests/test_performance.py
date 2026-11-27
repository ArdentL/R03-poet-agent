"""Exercise orchestration boundaries without cloud requests or robot controls."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from integrations import ModelConnection, ModelSettings
from performance import PerformanceManager, Poem, plan_performance
from robot_poet.model import BundleAudio, Config, Job, RobotInventory, Segment

ROBOT = {
    "ssh": {"host": "test-robot"},
    "remote": {
        "ros_setup": "/ros/setup.bash",
        "interfaces_setup": "/hal/setup.bash",
        "work_dir": "/tmp/test-poet",
        "domain_id": 100,
    },
}
POEM = {
    "title": "春晓",
    "author": "孟浩然",
    "era": "唐",
    "body": "春眠不觉晓。",
    "source_note": "已有诗词",
    "sound_opening": "轻柔音乐开场。",
    "sound_ending": "音乐渐弱。",
    "segments": [{"text": "春眠不觉晓。", "direction": "温柔舒缓。", "start": 0.3, "end": 3.0}],
}
PLAN = {"sequence": [{"expression": "微笑", "action": "欢迎"}]}
INVENTORY = RobotInventory(expressions=["微笑"], actions=["欢迎", "自检"])
REQUEST = {"id": "job-" + "a" * 32, "topic": "春晓"}


class PerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        config = self.root / "robot.json"
        config.write_text(json.dumps(ROBOT), encoding="utf-8")
        self.manager = PerformanceManager(self.root / "jobs", config)
        self.addCleanup(self.manager.close)
        self.addCleanup(patch.stopall)
        patch("performance.configured_key", return_value="test-key").start()
        patch("performance.transport.inventory", return_value=INVENTORY).start()
        self.llm = patch(
            "performance.call_model", side_effect=[json.dumps(POEM), json.dumps(PLAN)]
        ).start()
        patch("performance.synthesize", return_value=b"ID3test-audio").start()
        prepared = Job(
            job_id=REQUEST["id"],
            source_version=2,
            playback=Config.model_validate(ROBOT).playback,
            audio=BundleAudio(frames=96000, sha256="0" * 64),
            audio_level={"input_peak_dbfs": -8.0, "applied_db": 7.0},
            segments=[
                Segment(
                    id="sentence-0001", start_ms=300, end_ms=3000, expression="微笑", action="欢迎"
                )
            ],
        )
        patch("performance.prepare", return_value=prepared).start()

    def finish(self) -> None:
        assert self.manager.worker is not None
        self.manager.worker.join(timeout=3)
        self.assertFalse(self.manager.worker.is_alive())

    def test_success_persists_plan_and_duplicate_request_does_not_replay(self) -> None:
        def run(*args: Any, **kwargs: Any) -> int:
            kwargs["on_event"]({"type": "player", "state": "START"})
            kwargs["on_event"]({"type": "segment", "state": "running", "id": "sentence-0001"})
            kwargs["on_event"]({"type": "job", "state": "succeeded"})
            return 0

        with patch("performance.run_job", side_effect=run) as execute:
            self.manager.start(
                REQUEST,
                ModelSettings(
                    deepseek=ModelConnection(
                        base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test"
                    )
                ),
            )
            self.finish()
            result = self.manager.get()
            assert result is not None
            self.assertEqual(result.state, "succeeded")
            task = json.loads((self.root / "jobs" / result.id / "task.json").read_text())
            self.assertEqual(task["sequence"], PLAN["sequence"])
            self.assertEqual(self.manager.start(REQUEST, ModelSettings()).state, "succeeded")
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(self.llm.call_count, 2)

    def test_unknown_result_survives_restart_without_replay(self) -> None:
        def run(*args: Any, **kwargs: Any) -> int:
            kwargs["on_event"]({"type": "local", "state": "uploaded"})
            kwargs["on_event"]({"type": "local", "state": "unknown", "error": "SSH lost"})
            return 1

        with patch("performance.run_job", side_effect=run) as execute:
            self.manager.start(
                REQUEST,
                ModelSettings(
                    deepseek=ModelConnection(
                        base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test"
                    )
                ),
            )
            self.finish()
            restored = PerformanceManager(self.root / "jobs", self.root / "robot.json")
            result = restored.get()
            assert result is not None
            self.assertEqual(result.state, "unknown")
            self.assertEqual(restored.start(REQUEST, ModelSettings()).state, "unknown")
            self.assertEqual(execute.call_count, 1)

    def test_cancel_during_generation_never_starts_audio_or_robot(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def generate(*args: Any, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return json.dumps(POEM)

        self.llm.side_effect = generate
        with patch("performance.run_job") as execute, patch("performance.synthesize") as audio:
            self.manager.start(
                REQUEST,
                ModelSettings(
                    deepseek=ModelConnection(
                        base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test"
                    )
                ),
            )
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(self.manager.start(REQUEST, ModelSettings()).id, REQUEST["id"])
            with self.assertRaises(ValueError):
                self.manager.start(
                    REQUEST | {"id": "job-" + "b" * 32},
                    ModelSettings(
                        deepseek=ModelConnection(
                            base_url="https://api.deepseek.com",
                            model="deepseek-flash",
                            api_key="test",
                        )
                    ),
                )
            self.manager.cancel(REQUEST["id"])
            release.set()
            self.finish()
            result = self.manager.get()
            assert result is not None
            self.assertEqual(result.state, "cancelled")
            execute.assert_not_called()
            audio.assert_not_called()

    def test_plan_rejects_unsupported_expression(self) -> None:
        poem = Poem(
            title="春晓",
            author="孟浩然",
            era="唐",
            source_note="",
            body="春眠不觉晓。",
            tts_prompt="",
            duration_seconds=3.0,
            segments=POEM["segments"],
            checks=[],
        )
        self.llm.side_effect = [
            json.dumps({"sequence": [{"expression": "不存在", "action": None}]})
        ] * 3
        with self.assertRaisesRegex(ValueError, "不支持"):
            plan_performance(poem, INVENTORY, ModelSettings())

    def test_missing_actions_are_returned_to_model_before_audio(self) -> None:
        missing = {"sequence": [{"expression": "微笑", "action": None}]}
        self.llm.side_effect = [json.dumps(POEM), json.dumps(missing), json.dumps(PLAN)]
        with patch("performance.run_job", return_value=130):
            self.manager.start(
                REQUEST,
                ModelSettings(
                    deepseek=ModelConnection(
                        base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test"
                    )
                ),
            )
            self.finish()
        self.assertEqual(self.llm.call_count, 3)
        messages = self.llm.call_args.args[1]
        self.assertIn("遗漏身体动作", messages[-1]["content"])
        result = self.manager.get()
        assert result is not None
        self.assertEqual(result.sequence[0].action, "欢迎")

    def test_repeated_missing_actions_stop_before_synthesis(self) -> None:
        missing = {"sequence": [{"expression": "微笑", "action": None}]}
        self.llm.side_effect = [json.dumps(POEM)] + [json.dumps(missing)] * 3
        with patch("performance.synthesize") as audio, patch("performance.run_job") as execute:
            self.manager.start(
                REQUEST,
                ModelSettings(
                    deepseek=ModelConnection(
                        base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test"
                    )
                ),
            )
            self.finish()
            result = self.manager.get()
            assert result is not None
            self.assertEqual(result.state, "failed")
            self.assertIn("连续三次", result.message)
            audio.assert_not_called()
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
