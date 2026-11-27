#!/usr/bin/env python3
"""Robot-side full-audio Audio2Face executor; no model deployment and no audio_play."""

from __future__ import annotations

import argparse
import fcntl
import json
import select
import signal
import sys
import threading
import time
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Literal, Protocol

try:
    from .wire import (
        ACTION_NAMES,
        CHUNK_MS,
        PLAYBACK_RATE,
        SEND_INTERVAL_SECONDS,
        JsonObject,
        ValidationError,
        file_sha256,
        read_json,
        validate_schema,
    )
except ImportError:
    from wire import (
        ACTION_NAMES,
        CHUNK_MS,
        PLAYBACK_RATE,
        SEND_INTERVAL_SECONDS,
        JsonObject,
        ValidationError,
        file_sha256,
        read_json,
        validate_schema,
    )


class Cancelled(RuntimeError):
    pass


class OutcomeUnknown(RuntimeError):
    pass


def emit(kind: str, **data: Any) -> None:
    print(json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


def validate_bundle(directory: Path) -> JsonObject:
    directory = directory.resolve()
    job = read_json(directory / "job.json")
    validate_schema(job, read_json(directory / "job.schema.json"))
    previous_end = 0
    ids: set[str] = set()
    for segment in job["segments"]:
        if segment["start_ms"] < previous_end or segment["end_ms"] <= segment["start_ms"]:
            raise ValidationError("Timeline must be ordered, nonoverlapping and nonempty")
        if segment["id"] in ids:
            raise ValidationError("Duplicate segment id")
        if segment["action"] is not None and segment["action"] not in ACTION_NAMES:
            raise ValidationError("Unsupported body action")
        ids.add(segment["id"])
        previous_end = segment["end_ms"]
    audio = job["audio"]
    if audio["file"] != "audio.wav":
        raise ValidationError("Invalid audio filename")
    path = directory / audio["file"]
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 129 * 1024 * 1024:
        raise ValidationError("Audio must be a regular file within size limit")
    if file_sha256(path) != audio["sha256"]:
        raise ValidationError("Audio checksum mismatch")
    with wave.open(str(path), "rb") as stream:
        channels, width, rate, frames, codec, _ = stream.getparams()
        if not (
            type(audio["frames"]) is int
            and frames == audio["frames"] > 0
            and type(audio["sample_rate"]) is int
            and rate == audio["sample_rate"] == PLAYBACK_RATE
            and type(audio["channels"]) is int
            and channels == audio["channels"] == 1
            and width == 2
            and codec == "NONE"
            and frames <= rate * 3600
        ):
            raise ValidationError("Invalid playback PCM metadata")
        actual = 0
        while True:
            data = stream.readframes(65536)
            if not data:
                break
            actual += len(data)
        if actual != frames * 2:
            raise ValidationError("Truncated PCM audio")
    for segment in job["segments"]:
        if segment["end_ms"] * rate > frames * 1000:
            raise ValidationError("Timeline exceeds full audio")
    return job


class Control:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.reason = "cancelled"

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self.cancelled.set()

    def check(self) -> None:
        if self.cancelled.is_set():
            if self.reason.startswith("SSH"):
                raise OutcomeUnknown(self.reason)
            raise Cancelled(self.reason)

    def watch_stdin(self, timeout: float) -> None:
        def watch() -> None:
            last = time.monotonic()
            # Use raw reads: text buffering plus select can hide queued heartbeats.
            pending = b""
            while not self.cancelled.is_set():
                if time.monotonic() - last > timeout:
                    self.cancel("SSH heartbeat expired")
                    return
                ready, _, _ = select.select([sys.stdin.buffer], [], [], 0.2)
                if not ready:
                    continue
                import os

                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    self.cancel("SSH input disconnected")
                    return
                pending += chunk
                if len(pending) > 8192:
                    self.cancel("Invalid control input")
                    return
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line == b"heartbeat":
                        last = time.monotonic()
                    else:
                        self.cancel("Cancellation requested")
                        return

        threading.Thread(target=watch, daemon=True).start()


class ServiceResponse(Protocol):
    success: bool
    message: str
    str_results: Sequence[str]


class ServiceFuture(Protocol):
    def done(self) -> bool: ...
    def result(self) -> ServiceResponse | None: ...


@dataclass
class PendingRequest:
    kind: str
    future: ServiceFuture
    deadline: float
    segment: JsonObject | None = None

    def accepted(self) -> bool:
        future = self.future
        if not future.done():
            if time.monotonic() >= self.deadline:
                raise OutcomeUnknown(f"{self.kind}: response timed out; no retry")
            return False
        try:
            response = future.result()
        except Exception as exc:
            raise OutcomeUnknown(f"{self.kind}: response lost") from exc
        if response is None:
            raise OutcomeUnknown(f"{self.kind}: response missing")
        if not response.success:
            raise RuntimeError(f"{self.kind} rejected: {response.message}")
        return True


@dataclass
class ActionState:
    action_id: str
    name: str
    deadline: float
    state: Literal["requested", "started", "ended", "interrupted"] = "requested"
    started: bool = False


class TimelineDriver(Protocol):
    player_start: float | None
    active_action: str | None
    current_expression: str | None

    def begin_call(
        self, command: str, strings: Sequence[str] = (), ints: Sequence[int] = ()
    ) -> ServiceFuture: ...


class TimelineRunner:
    """All service futures are polled; the same pump keeps feeding audio and observing events."""

    def __init__(self, driver: TimelineDriver, job: JsonObject) -> None:
        self.driver, self.job = driver, job
        self.options = job["playback"]
        self.expression_pending: PendingRequest | None = None
        self.action_pending: PendingRequest | None = None
        self.expression_index = self.action_index = 0
        self.expressions = job["segments"]
        self.actions = [s for s in job["segments"] if s.get("action") is not None]
        self.current_action: ActionState | None = None
        self.action_records: dict[str, ActionState] = {}

    def request(
        self,
        kind: str,
        command: str,
        strings: Sequence[str],
        ints: Sequence[int] = (),
        segment: JsonObject | None = None,
    ) -> PendingRequest:
        return PendingRequest(
            kind,
            self.driver.begin_call(command, strings, ints),
            time.monotonic() + self.options["service_timeout"],
            segment,
        )

    def start_action(self, segment: JsonObject) -> None:
        action_id = self.job["job_id"] + "/action/" + segment["id"]
        record = ActionState(
            action_id, segment["action"], time.monotonic() + self.options["action_timeout"]
        )
        self.current_action = record
        self.action_records[action_id] = record
        # Set before sending: cancellation must STOP even if the start response is lost.
        self.driver.active_action = action_id
        self.action_pending = self.request(
            "action_start", "servo_action", (action_id, segment["action"]), (1,), segment
        )
        emit("action", action_id=action_id, name=segment["action"], state="requested")

    def on_action(self, action_id: str, status: int) -> None:
        record = self.action_records.get(action_id)
        if record is None or record is not self.current_action or record.state == "interrupted":
            return  # Late status from a replaced action cannot finish the current one.
        if record.state == "ended":
            return
        if status == 1:
            record.started = True
            record.state = "started"
        elif status == 4:
            if not record.started:
                raise OutcomeUnknown(f"{action_id}: END without START")
            record.state = "ended"
            self.driver.active_action = None
        elif status == 5:
            emit("action", action_id=action_id, name=record.name, state="failed", status=status)
            raise RuntimeError(f"{action_id}: servo FAIL")
        else:
            return
        emit("action", action_id=action_id, name=record.name, state=record.state, status=status)

    def due(
        self, entries: list[JsonObject], index: int, origin: float, offset: int, now: float
    ) -> bool:
        if index >= len(entries):
            return False
        boundary = origin + max(0, (entries[index]["start_ms"] + offset) / 1000)
        if now < boundary:
            return False
        if index + 1 < len(entries):
            following = origin + max(0, (entries[index + 1]["start_ms"] + offset) / 1000)
            if now >= following:
                raise OutcomeUnknown("Missed control boundary; expired controls will not be queued")
        return True

    def pump(self) -> None:
        now = time.monotonic()
        if self.expression_pending and self.expression_pending.accepted():
            segment = self.expression_pending.segment
            assert segment is not None
            emit(
                "expression",
                id=segment["id"],
                name=segment["expression"],
                accepted=True,
                completed=False,
            )
            self.expression_pending = None
        if self.action_pending and self.action_pending.accepted():
            pending, self.action_pending = self.action_pending, None
            if pending.kind == "action_stop":
                old = self.current_action
                assert old is not None
                old.state = "interrupted"
                emit(
                    "action",
                    action_id=old.action_id,
                    name=old.name,
                    state="interrupted",
                    stop_accepted=True,
                    mechanical_return_confirmed=False,
                )
                self.driver.active_action = None
                # Do not launch a target whose next action boundary already arrived.
                origin = self.driver.player_start
                if self.action_index < len(self.actions):
                    assert origin is not None
                    limit = origin + max(
                        0,
                        (
                            self.actions[self.action_index]["start_ms"]
                            + self.options["action_offset_ms"]
                        )
                        / 1000,
                    )
                    if now >= limit:
                        raise OutcomeUnknown(
                            "STOP completed after next action boundary; expired action skipped"
                        )
                assert pending.segment is not None
                self.start_action(pending.segment)
            else:
                assert self.current_action is not None
                emit("action_request", action_id=self.current_action.action_id, accepted=True)
        if self.current_action and self.current_action.state not in ("ended", "interrupted"):
            if now >= self.current_action.deadline:
                raise OutcomeUnknown("Body action timed out waiting for START/END")
        origin = self.driver.player_start
        if origin is None:
            return
        if self.due(
            self.expressions,
            self.expression_index,
            origin,
            self.options["expression_offset_ms"],
            now,
        ):
            if self.expression_pending:
                raise OutcomeUnknown("Previous expression request unresolved at next boundary")
            segment = self.expressions[self.expression_index]
            self.expression_index += 1
            self.driver.current_expression = segment["expression"]
            strings = (
                self.job["job_id"] + "/" + segment["id"],
                segment["expression"],
                "",
                "",
                "",
                "",
                "LOCAL",
                "",
            )
            self.expression_pending = self.request(
                "expression", "head_command", strings, segment=segment
            )
            emit(
                "segment",
                id=segment["id"],
                state="running",
                timeline_ms=segment["start_ms"],
                timing="player_START",
            )
        if self.due(self.actions, self.action_index, origin, self.options["action_offset_ms"], now):
            if self.action_pending:
                raise OutcomeUnknown(
                    "STOP/start request unresolved at next action boundary; no queued replay"
                )
            segment = self.actions[self.action_index]
            self.action_index += 1
            if self.current_action and self.current_action.state != "ended":
                self.action_pending = self.request(
                    "action_stop",
                    "servo_action",
                    (self.current_action.action_id, self.current_action.name),
                    (2,),
                    segment,
                )
            else:
                self.start_action(segment)

    def finished(self) -> bool:
        return (
            self.expression_index == len(self.expressions)
            and self.action_index == len(self.actions)
            and self.expression_pending is None
            and self.action_pending is None
            and (self.current_action is None or self.current_action.state == "ended")
        )


class RosDriver:
    def __init__(self, options: JsonObject, control: Control, check_only: bool = False) -> None:
        import rclpy
        from dgrobot_msgs.msg import DialogueCtrl, DialogueStatus
        from hal_interfaces.msg import HalUnifiedMsg
        from hal_interfaces.srv import HalUnifiedCall
        from rclpy.node import Node

        self.ros, self.Dialogue, self.Call = rclpy, DialogueCtrl, HalUnifiedCall
        self.options, self.control = options, control
        self.active_session: str | None = None
        self.active_action: str | None = None
        self.current_expression: str | None = None
        self.player_start: float | None = None
        self.player_end: float | None = None
        self.runner: TimelineRunner | None = None
        self.node: Any = None
        self.frame_index = 0
        self.monitoring = False
        self.topology_checked = 0
        rclpy.init()
        try:
            self.node = Node("robot_poet_ssh_executor")
            self.subscription = self.node.create_subscription(
                HalUnifiedMsg, "/hal/unified/event", self.on_event, 100
            )
            self.player_subscription = self.node.create_subscription(
                DialogueStatus, "/dgrobot/dialogue_status", self.on_player, 100
            )
            self.client = self.node.create_client(HalUnifiedCall, "/hal/unified/call")
            self.publisher = (
                None
                if check_only
                else self.node.create_publisher(DialogueCtrl, "/dgrobot/dialogue_ctrl", 50)
            )
        except BaseException:
            self.close()
            raise

    def on_event(self, message: Any) -> None:
        if (
            message.msg_type == "expression_status"
            and message.expr_expression == self.current_expression
        ):
            emit(
                "expression_status",
                expression=message.expr_expression,
                status=int(message.expr_status),
            )
        elif message.msg_type == "servo_status" and self.runner:
            self.runner.on_action(message.action_id, int(message.status))

    def on_player(self, message: Any) -> None:
        if not self.active_session or message.dialogue_id != self.active_session:
            return
        if message.status == 1 and self.player_start is None:
            self.player_start = time.monotonic()
            emit(
                "player",
                dialogue_id=self.active_session,
                state="START",
                monotonic=self.player_start,
            )
        elif message.status == 2 and self.player_end is None:
            if self.player_start is None:
                raise OutcomeUnknown("Player END without START")
            self.player_end = time.monotonic()
            emit("player", dialogue_id=self.active_session, state="END")

    def topology_ready(self) -> bool:
        # Confirm the existing combined path, not merely an arbitrary subscriber.
        checks = (
            ("/dgrobot/dialogue_ctrl", False, "audio2face_stream"),
            ("/dgrobot/dialogue_ctrl_audio2face", True, "audio2face_stream"),
            ("/dgrobot/dialogue_ctrl_audio2face", False, "s1_control_center_node"),
            ("/s1/gateway/audio_sync_data", True, "s1_control_center_node"),
            ("/s1/gateway/audio_sync_data", False, "hal_audio_player_node"),
            ("/head_node/gateway/audio_face_clip", True, "s1_control_center_node"),
            ("/head_node/gateway/audio_face_clip", False, "head_node"),
            ("/dgrobot/dialogue_status", True, "hal_audio_player_node"),
            ("/hal/unified/event", True, "hal_unified_gateway"),
        )
        for topic, publisher, name in checks:
            method = (
                self.node.get_publishers_info_by_topic
                if publisher
                else self.node.get_subscriptions_info_by_topic
            )
            if not any(endpoint.node_name == name for endpoint in method(topic)):
                return False
        return self.client.service_is_ready()

    def tick(self, seconds: float = 0.01) -> None:
        self.control.check()
        if not self.ros.ok():
            raise OutcomeUnknown("ROS connection lost; no replay")
        self.ros.spin_once(self.node, timeout_sec=seconds)
        self.control.check()
        if self.monitoring and time.monotonic() - self.topology_checked >= 0.5:
            self.topology_checked = time.monotonic()
            if not self.topology_ready():
                raise OutcomeUnknown("Audio2Face/player/head/SDK connection disappeared")

    def begin_call(
        self, command: str, strings: Sequence[str] = (), ints: Sequence[int] = ()
    ) -> ServiceFuture:
        self.control.check()
        if not self.client.service_is_ready():
            raise OutcomeUnknown(f"{command}: SDK service unavailable")
        request = self.Call.Request()
        request.cmd, request.str_args, request.int_args = command, list(strings), list(ints)
        return self.client.call_async(request)

    def call(self, command: str) -> ServiceResponse:
        deadline = time.monotonic() + self.options["discovery_timeout"]
        while not self.client.service_is_ready():
            if time.monotonic() >= deadline:
                raise RuntimeError("SDK service unavailable before controls")
            self.tick()
        pending = PendingRequest(
            command, self.begin_call(command), time.monotonic() + self.options["service_timeout"]
        )
        while not pending.accepted():
            self.tick()
        response = pending.future.result()
        assert response is not None
        return response

    def discover_audio(self) -> None:
        deadline = time.monotonic() + self.options["discovery_timeout"]
        while not self.topology_ready():
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Existing Audio2Face combined output path is unavailable; no controls sent"
                )
            self.tick()
        emit("topology", state="ready", combined_output="/dgrobot/dialogue_ctrl_audio2face")

    def send(self, state: int, data: bytes = b"", check: bool = True) -> None:
        if check:
            self.control.check()
        message = self.Dialogue()
        message.dialogue_id, message.state = self.active_session, state
        message.freme_index = self.frame_index
        message.audio_data = list(data)
        message.tts_meta = json.dumps(
            {"sample_rate": PLAYBACK_RATE, "channels": 1, "fmt": "pcm_s16le"}
        )
        self.publisher.publish(message)
        self.frame_index += 1

    def play_sequence(self, job: JsonObject, directory: Path) -> None:
        self.runner = TimelineRunner(self, job)
        self.active_session = job["job_id"] + "/dialogue"
        self.monitoring = True
        sent_at = time.monotonic()
        duration = job["audio"]["frames"] / PLAYBACK_RATE

        def pump(seconds: float = 0) -> None:
            self.tick(seconds)
            self.runner.pump()
            now = time.monotonic()
            if self.player_start is None and now - sent_at >= self.options["start_timeout"]:
                raise OutcomeUnknown("Player START timeout; no replay")
            if self.player_start is not None and self.player_end is None:
                if now >= self.player_start + duration + self.options["end_timeout"]:
                    raise OutcomeUnknown("Player END timeout; no replay")

        def wait_until(deadline: float) -> None:
            while time.monotonic() < deadline:
                pump(min(0.01, max(0, deadline - time.monotonic())))

        self.send(self.Dialogue.FREME_START)
        emit(
            "audio",
            state="submitted",
            dialogue_id=self.active_session,
            frames=job["audio"]["frames"],
            sample_rate=PLAYBACK_RATE,
            continuous=True,
        )
        with wave.open(str(directory / "audio.wav"), "rb") as stream:
            chunk_frames = max(1, PLAYBACK_RATE * CHUNK_MS // 1000)
            while True:
                data = stream.readframes(chunk_frames)
                if not data:
                    break
                if self.player_end is not None:
                    raise OutcomeUnknown("Player END before full input was sent")
                self.send(self.Dialogue.FREME_PROCESSING, data)
                wait_until(time.monotonic() + SEND_INTERVAL_SECONDS)
                pump()
        self.send(self.Dialogue.FREME_END)
        # No estimated completion: only the matching player's END can finish audio.
        while self.player_end is None or not self.runner.finished():
            pump(0.01)
        for segment in job["segments"]:
            emit(
                "segment",
                id=segment["id"],
                state="succeeded",
                audio_completion="player_END",
                expression_completion="accepted",
            )
        self.active_session = None
        self.monitoring = False

    def interrupt(self) -> None:
        if not self.active_session and not self.active_action:
            return
        self.runner = None
        # Submit BOTH interrupts before waiting on either response, even after cancellation.
        stop_future = None
        if self.active_session and self.publisher is not None:
            try:
                self.send(self.Dialogue.INTERRUPT, check=False)
                emit(
                    "interrupt",
                    target="dialogue",
                    dialogue_id=self.active_session,
                    sent=True,
                    completion_confirmed=False,
                )
            except Exception as exc:
                emit("interrupt", target="dialogue", sent=False, error=str(exc))
        if self.active_action:
            try:
                request = self.Call.Request()
                request.cmd, request.int_args = "servo_action", [2]
                request.str_args = [self.active_action, ""]
                stop_future = self.client.call_async(request)
            except Exception as exc:
                emit("interrupt", target="body", sent=False, error=str(exc))
        if stop_future is not None:
            deadline = time.monotonic() + min(2, self.options["service_timeout"])
            try:
                while not stop_future.done() and self.ros.ok() and time.monotonic() < deadline:
                    self.ros.spin_once(self.node, timeout_sec=0.02)
                response = stop_future.result() if stop_future.done() else None
                emit(
                    "interrupt",
                    target="body",
                    action_id=self.active_action,
                    sent=True,
                    stop_accepted=bool(response and response.success),
                    mechanical_return_confirmed=False,
                )
            except Exception as exc:
                emit(
                    "interrupt",
                    target="body",
                    sent=True,
                    error=str(exc),
                    mechanical_return_confirmed=False,
                )
        self.active_session = self.active_action = None

    def close(self) -> None:
        if self.node is not None:
            self.node.destroy_node()
        if self.ros.ok():
            self.ros.shutdown()


def execute(
    job: JsonObject, directory: Path, driver: RosDriver, control: Control, check_only: bool = False
) -> None:
    expressions = list(driver.call("get_expression_list").str_results)
    actions = list(driver.call("get_action_list").str_results)
    unknown_expressions = {s["expression"] for s in job["segments"]} - set(expressions)
    unknown_actions = {
        s.get("action") for s in job["segments"] if s.get("action") is not None
    } - set(actions)
    if unknown_expressions or unknown_actions:
        raise ValidationError(
            f"Unknown robot expressions={sorted(unknown_expressions)}, actions={sorted(unknown_actions)}"
        )
    emit("inventory", expressions=expressions, actions=actions)
    driver.discover_audio()
    if check_only:
        emit("job", state="validated", controls_sent=False)
        return
    control.check()
    driver.play_sequence(job, directory)
    emit("job", state="succeeded", audio_completion="player_END", body_completion="servo_END")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--validate", action="store_true", help="Offline bundle validation; no ROS")
    parser.add_argument("--check", action="store_true", help="Query inventory; no control messages")
    parser.add_argument("--managed", action="store_true", help="Require SSH stdin heartbeats")
    args = parser.parse_args(argv)
    control, driver = Control(), None
    lock = None
    try:
        job = validate_bundle(args.bundle)
        if args.validate:
            emit("job", state="bundle_validated", segments=len(job["segments"]))
            return 0
        if not args.managed:
            raise ValidationError("Execution requires --managed and SSH heartbeat input")
        lock = (args.bundle.resolve().parent / ".robot-poet.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another robot-poet job is running in this work directory") from exc

        def cancel_signal(signum: int, frame: FrameType | None) -> None:
            control.cancel(f"Signal {signum}")

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, cancel_signal)
        control.watch_stdin(job["playback"]["lease_timeout"])
        driver = RosDriver(job["playback"], control, args.check)
        execute(job, args.bundle, driver, control, args.check)
        return 0
    except (Cancelled, KeyboardInterrupt) as exc:
        if driver is not None:
            driver.interrupt()
        emit("job", state="cancelled", error=str(exc), current_completion="unknown")
        return 130
    except Exception as exc:
        if driver is not None:
            driver.interrupt()
        emit(
            "job", state="unknown" if isinstance(exc, OutcomeUnknown) else "failed", error=str(exc)
        )
        return 1
    finally:
        if driver is not None:
            try:
                driver.interrupt()
            finally:
                driver.close()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
