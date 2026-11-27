"""One background performance: poetry → audio → robot, with durable status."""

from __future__ import annotations

import json
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from integrations import ModelSettings, call_model
from poetry import (
    CODE_FENCE,
    PoetryCheck,
    PoetrySpec,
    build_poetry_messages,
    normalize_poetry,
    read_poetry_spec,
)
from robot_poet import transport
from robot_poet.audio import prepare
from robot_poet.client import run_job
from robot_poet.model import (
    AutomaticTask,
    Config,
    Model,
    RobotInventory,
    SequenceEntry,
    load_config,
    save_model,
)
from robot_poet.wire import ACTION_NAMES, JsonObject
from seed_audio import PROMPT_LIMIT, configured_key, synthesize

JOB_ID = re.compile(r"job-[0-9a-f]{32}")
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
State = Literal[
    "checking",
    "generating",
    "planning",
    "synthesizing",
    "preparing",
    "playing",
    "finishing",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
]


class NarrationLine(Model):
    text: str
    direction: str
    start: float
    end: float


class Poem(Model):
    title: str
    author: str
    era: str
    source_note: str
    body: str
    tts_prompt: str
    duration_seconds: float
    segments: list[NarrationLine]
    checks: list[PoetryCheck]


class PerformancePlan(Model):
    sequence: list[SequenceEntry] = Field(min_length=1, max_length=200)


class ActionProgress(Model):
    name: str
    state: str


class Performance(Model):
    id: str = Field(pattern=r"^job-[0-9a-f]{32}$")
    spec: PoetrySpec
    state: State = "checking"
    message: str = "正在读取机器人能力…"
    poem: Poem | None = None
    sequence: list[SequenceEntry] = Field(default_factory=list)
    audio_ready: bool = False
    audio_format: Literal["mp3", "wav"] = "mp3"
    controls_possible: bool = False
    actions: dict[str, ActionProgress] = Field(default_factory=dict)


def narration_prompt(spec: PoetrySpec, poem: Poem) -> str:
    """Keep every verse; shorten directing notes before rejecting a long prompt."""
    prompt = poem.tts_prompt
    if len(prompt) > PROMPT_LIMIT:
        lines = [
            f"[{line.start:.1f}s:{line.end:.1f}s]{json.dumps(line.text, ensure_ascii=False)}"
            for line in poem.segments
        ]
        prompt = "\n\n".join(
            [
                spec.voice,
                spec.soundscape,
                "只朗诵下列诗句，不读时间标记；每句之间自然停顿约半秒，不添加解说。",
                *lines,
            ]
        )
    if len(prompt) > PROMPT_LIMIT or poem.duration_seconds > 120:
        raise ValueError("本次诗词超出单次音频生成限制（3000 字提示词、120 秒），请缩短篇幅。")
    return prompt


def plan_performance(
    poem: Poem,
    inventory: RobotInventory,
    llm: ModelSettings,
    cancel: threading.Event | None = None,
) -> PerformancePlan:
    actions = sorted((set(inventory.actions) & ACTION_NAMES) - {"自检", "自我介绍", "默认_body"})
    expressions = [
        name for name in inventory.expressions if name not in {"休眠", "关机", "自检", "自我介绍"}
    ]
    gestures = [name for name in actions if name != "手垂下"]
    if not gestures:
        raise ValueError("机器人没有可用的身体表演动作，请检查动作清单。")
    messages = [
        {
            "role": "system",
            "content": (
                "你是机器人吟诗导演。根据逐句诗意和语气安排表情与身体动作，只返回 JSON。"
                "严格保持分句数量与顺序，每句对应一个 sequence 项，不改写诗句。"
                "expression 必须来自提供的表情清单；action 只能来自动作清单或为 null。"
                "表情自然克制、连贯，避免无意义切换。身体动作稀疏，必须至少安排一个欢迎或指向动作。"
                "禁止选择休眠、关机、自检或自我介绍等系统表情。"
                "动作可跨多句持续，null 表示不发新动作而非停止；避免连续逐句启动动作。"
                '结构为 {"sequence":[{"expression":"表情名称","action":null}] }。'
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "title": poem.title,
                    "segments": [line.model_dump() for line in poem.segments],
                    "expressions": expressions,
                    "actions": actions,
                },
                ensure_ascii=False,
            ),
        },
    ]
    for attempt in range(3):
        if cancel is not None and cancel.is_set():
            raise PerformanceCancelled()
        raw = call_model(llm, messages, max_tokens=4096).strip()
        try:
            fence = CODE_FENCE.fullmatch(raw)
            plan = PerformancePlan.model_validate_json(fence.group(1) if fence else raw)
            if len(plan.sequence) != len(poem.segments):
                raise ValueError("表演计划与诗词分句数量不一致。")
            for entry in plan.sequence:
                if entry.expression not in expressions or (
                    entry.action is not None and entry.action not in actions
                ):
                    raise ValueError("表演计划包含机器人不支持的表情或动作。")
            if not any(entry.action in gestures for entry in plan.sequence):
                raise ValueError(
                    "遗漏身体动作：必须至少安排一个可用的欢迎或指向动作，不能全部为 null 或手垂下。"
                )
            return plan
        except ValueError as exc:
            if attempt == 2:
                raise ValueError(f"表演计划连续三次未通过检查：{exc}") from exc
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"方案未通过检查：{exc} 请重新生成完整 JSON，不要只返回修改项。",
                    },
                ]
            )
    raise AssertionError("Unreachable")


class PerformanceCancelled(Exception):
    pass


class PerformanceManager:
    """Serialize performances without a queue; a repeated request ID never replays."""

    def __init__(self, directory: Path, config_path: Path) -> None:
        self.directory = directory
        self.config_path = config_path
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current: Performance | None = None
        for path in sorted(
            directory.glob("job-*/performance.json"), key=lambda p: p.stat().st_mtime
        ):
            job = Performance.model_validate_json(path.read_text(encoding="utf-8"))
            if job.state not in TERMINAL:
                job.state = "unknown" if job.controls_possible else "cancelled"
                job.message = "上次运行中断，未自动重播；请确认机器人状态。"
                save_model(path, job)
            self.current = job

    def get(self, job_id: str | None = None) -> Performance | None:
        with self.lock:
            if job_id is None or (self.current is not None and self.current.id == job_id):
                return self.current.model_copy(deep=True) if self.current else None
            if JOB_ID.fullmatch(job_id) is None:
                raise ValueError("任务 ID 无效。")
            path = self.directory / job_id / "performance.json"
            return Performance.model_validate_json(path.read_text(encoding="utf-8"))

    def start(self, data: JsonObject, llm: ModelSettings) -> Performance:
        job = Performance(id=data.get("id"), spec=read_poetry_spec(data))
        if not (job.spec.topic or job.spec.source_text):
            raise ValueError("请填写诗词题目、创作主题或原文。")
        if job.spec.target_seconds is not None and job.spec.target_seconds > 120:
            raise ValueError("机器人吟诗单次最多支持 120 秒。")
        with self.lock:
            path = self.directory / job.id / "performance.json"
            if path.exists():
                previous = self.get(job.id)
                assert previous is not None
                if previous.spec != job.spec:
                    raise ValueError("该请求 ID 已用于另一份诗词任务。")
                return previous
            if self.worker is not None and self.worker.is_alive():
                raise ValueError("机器人已有任务，请等待完成或停止当前任务。")
            if not llm.connection.api_key:
                raise ValueError("请先在模型设置中填写当前服务的 API Key。")
            if not self.config_path.is_file():
                raise ValueError(
                    "请先复制 config/robot.example.json 为 config/robot.json 并填写连接信息。"
                )
            config = load_config(self.config_path)
            configured_key()
            self.current = job
            save_model(path, job)
            self.cancel_event = threading.Event()
            self.worker = threading.Thread(
                target=self._work,
                args=(job.model_copy(deep=True), config, llm, self.cancel_event),
                name="robot-performance",
                daemon=True,
            )
            self.worker.start()
            return job.model_copy(deep=True)

    def cancel(self, job_id: str) -> Performance:
        with self.lock:
            job = self.get(job_id)
            if job is None:
                raise ValueError("任务不存在。")
            if self.current is not None and job.id == self.current.id and job.state not in TERMINAL:
                self.cancel_event.set()
                self._update(message="正在停止；生成中的服务请求返回后不会继续执行。")
                return self.current.model_copy(deep=True)
            return job

    def close(self) -> None:
        self.cancel_event.set()
        if self.worker is not None:
            self.worker.join(timeout=6)

    def _update(self, **changes: Any) -> None:
        with self.lock:
            assert self.current is not None
            self.current = Performance.model_validate(self.current.model_dump() | changes)
            save_model(self.directory / self.current.id / "performance.json", self.current)

    def _work(
        self, job: Performance, config: Config, llm: ModelSettings, cancel: threading.Event
    ) -> None:
        directory = self.directory / job.id
        outcome: str | None = None
        detail = ""
        cleanup_warning = ""

        def checkpoint() -> None:
            if cancel.is_set():
                raise PerformanceCancelled()

        def on_event(event: JsonObject) -> None:
            nonlocal outcome, detail, cleanup_warning
            kind, state = event.get("type"), event.get("state")
            if kind == "job" and state in TERMINAL:
                outcome, detail = state, event.get("error", "")
            elif kind == "local" and state in {"unknown", "cancelled", "upload_failed"}:
                outcome = "failed" if state == "upload_failed" else state
                detail = event.get("error", "")
            elif kind == "local" and state == "cleanup_failed":
                cleanup_warning = "远端文件清理失败，已保留报告。"
            elif kind == "local" and state == "uploaded":
                self._update(
                    state="checking", controls_possible=True, message="已上传，正在检查播放链路…"
                )
            elif kind == "player":
                self._update(
                    state="playing" if state == "START" else "finishing",
                    message="机器人正在吟诗…"
                    if state == "START"
                    else "朗诵结束，等待最后的动作完成…",
                )
            elif kind == "segment" and state == "running":
                index = int(event["id"].rsplit("-", 1)[-1]) - 1
                self._update(message=f"正在吟诵：{poem.segments[index].text}")
            elif kind == "action":
                current = self.get()
                assert current is not None
                progress = ActionProgress(name=event["name"], state=state)
                self._update(actions=current.actions | {event["action_id"]: progress})

        try:
            checkpoint()
            inventory = transport.inventory(config)
            if not (
                (set(inventory.actions) & ACTION_NAMES)
                - {"手垂下", "自检", "自我介绍", "默认_body"}
            ):
                raise ValueError("机器人没有可用的身体表演动作，请检查动作清单。")
            checkpoint()
            self._update(state="generating", message="正在生成诗词与朗诵方案…")
            messages = build_poetry_messages(job.spec)
            messages[0]["content"] += (
                "本次将自动合成并由机器人朗诵。朗诵总长不超过120秒，逐句语气说明简短，"
                "总提示词控制在3000字符以内；不添加标题、作者或解说的朗读。"
            )
            raw = call_model(llm, messages, max_tokens=16000)
            checkpoint()
            poem = Poem.model_validate(normalize_poetry(job.spec, raw))
            poem.tts_prompt = narration_prompt(job.spec, poem)
            self._update(state="planning", poem=poem, message="正在按诗意安排表情与动作…")
            plan = plan_performance(poem, inventory, llm, cancel)
            checkpoint()
            self._update(state="synthesizing", sequence=plan.sequence, message="正在合成朗诵音频…")
            audio = synthesize(poem.tts_prompt)
            checkpoint()
            (directory / "audio.wav").write_bytes(audio)
            self._update(
                state="preparing",
                audio_ready=True,
                audio_format="wav",
                message="正在检测实际停顿并准备表演…",
            )
            task = AutomaticTask(version=2, audio="audio.wav", sequence=plan.sequence)
            save_model(directory / "task.json", task)
            with tempfile.TemporaryDirectory(prefix="prepare-", dir=directory) as scratch:
                prepared = prepare(
                    task,
                    directory,
                    config,
                    directory / "bundle",
                    Path(scratch),
                    job.id,
                    directory / "task.timeline.json",
                )
            if prepared.audio.frames / prepared.audio.sample_rate > 120:
                raise ValueError("生成音频超过 120 秒，未发送给机器人。")
            checkpoint()
            self._update(state="checking", message="正在上传并检查机器人…")
            code = run_job(
                config,
                directory / "bundle",
                prepared,
                directory / "upload.tar",
                directory / "report.jsonl",
                cancel=cancel,
                on_event=on_event,
            )
            state = outcome or ("cancelled" if code == 130 else "unknown")
            message = {
                "succeeded": "吟诗与动作已完成。",
                "cancelled": "任务已停止；如已开始表演，停止受理不代表机械回位完成。",
                "unknown": "机器人执行结果未知，未自动重播；请确认实机状态。",
                "failed": "任务失败，未自动重播。",
            }[state]
            self._update(
                state=state, message=" ".join(filter(None, [message, detail, cleanup_warning]))
            )
        except PerformanceCancelled:
            self._update(state="cancelled", message="任务已取消，未启动机器人表演。")
        except Exception as exc:
            current = self.get()
            unknown = current is not None and current.controls_possible and outcome not in TERMINAL
            if cancel.is_set() and current is not None and not current.controls_possible:
                self._update(state="cancelled", message="任务已取消，未启动机器人表演。")
                return
            self._update(
                state=outcome or ("unknown" if unknown else "failed"),
                message=f"{exc}；未自动重播。" if unknown else str(exc),
            )
