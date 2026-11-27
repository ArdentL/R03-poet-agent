"""One-command analysis, full-audio preparation, robot checks and execution."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from . import transport
from .audio import analyze, prepare
from .model import AutomaticTask, Config, Job, load_config, load_task
from .wire import JsonObject, ValidationError


def preview(job: Job) -> None:
    print(f"Task {job.job_id}: {len(job.segments)} sentence(s)", flush=True)
    for segment in job.segments:
        print(
            f"  {segment.id}: {segment.start_ms}..{segment.end_ms} ms | "
            f"{segment.expression} | action={segment.action}",
            flush=True,
        )
    print(
        f"Full audio: {job.audio.frames / job.audio.sample_rate:.3f}s; "
        "one Audio2Face session, scheduled from player START.",
        flush=True,
    )
    if job.audio_level.input_peak_dbfs is None:
        print("Audio: digital silence; no gain applied.", flush=True)
    else:
        print(
            f"Audio peak: {job.audio_level.input_peak_dbfs:.1f} → -1.0 dBFS; "
            f"automatic gain {job.audio_level.applied_db:+.1f} dB.",
            flush=True,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("analyze", "prepare", "run"))
    parser.add_argument("task", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="prepare: new output directory")
    parser.add_argument("--yes", action="store_true", help="run: skip confirmation")
    parser.add_argument(
        "--check", action="store_true", help="run: upload and query inventory, no playback"
    )
    parser.add_argument("--keep-remote", action="store_true", help="run: retain uploaded job files")
    parser.add_argument("--report", type=Path, help="run: local JSONL report path")
    parser.add_argument(
        "--timeline", type=Path, help="editable analysis cache; default TASK.timeline.json"
    )
    args = parser.parse_args(argv)
    if args.command == "prepare" and (
        not args.output or args.check or args.yes or args.keep_remote or args.report
    ):
        parser.error("prepare requires --output and does not accept run options")
    if args.command == "run" and args.output:
        parser.error("--output is only used by prepare")
    if args.command == "analyze" and (
        args.output or args.check or args.yes or args.keep_remote or args.report
    ):
        parser.error("analyze accepts --timeline, not prepare/run options")
    try:
        task = load_task(args.task)
        config = load_config(args.config)
        job_id = "job-" + uuid.uuid4().hex
        timeline_path = (args.timeline or args.task.with_suffix(".timeline.json")).resolve()
        if timeline_path == args.task.resolve() or timeline_path == args.config.resolve():
            raise ValidationError("Timeline must not overwrite the task or configuration")
        with tempfile.TemporaryDirectory(prefix="robot-poet-") as temporary:
            scratch = Path(temporary)
            if args.command == "analyze":
                analyzed = analyze(task, args.task.resolve().parent, config, scratch, timeline_path)
                print(analyzed.timeline.model_dump_json(indent=2))
                count = len(analyzed.timeline.regions)
                print(
                    f"Preview: {timeline_path}; {count} sentence(s), full audio {analyzed.info.duration:.3f}s"
                )
                if isinstance(task, AutomaticTask) and count != len(task.sequence):
                    print(
                        f"Count mismatch: sequence={len(task.sequence)}; edit regions before prepare/run.",
                        file=sys.stderr,
                    )
                    return 1
                return 0
            bundle = args.output.resolve() if args.output else scratch / "bundle"
            job = prepare(
                task, args.task.resolve().parent, config, bundle, scratch, job_id, timeline_path
            )
            preview(job)
            if args.command == "prepare":
                print(f"Prepared offline: {bundle}")
                return 0
            if not args.check and not args.yes:
                if not sys.stdin.isatty():
                    raise ValidationError(
                        "Interactive confirmation requires a terminal; use --yes for unattended execution"
                    )
                if input("Upload to robot and start? [y/N] ").strip().lower() != "y":
                    print("Cancelled before SSH connection.")
                    return 130
            report_path = args.report or Path(".robot-poet/reports") / (job_id + ".jsonl")
            return run_job(
                config,
                bundle,
                job,
                scratch / "upload.tar",
                report_path,
                check_only=args.check,
                keep_remote=args.keep_remote,
            )

    except KeyboardInterrupt:
        print("Cancelled; any in-flight playback outcome is unknown.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


class JobReport:
    """Write remote JSONL verbatim and retain its terminal state for cleanup decisions."""

    def __init__(
        self, stream: TextIO, on_event: Callable[[JsonObject], None] | None = None
    ) -> None:
        self.stream = stream
        self.on_event = on_event
        self.state: str | None = None

    def record(self, value: JsonObject) -> None:
        self.stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.stream.flush()
        if self.on_event is not None:
            self.on_event(value)

    def on_line(self, line: str) -> None:
        print(line, flush=True)
        try:
            value = json.loads(line)
        except ValueError:
            self.record({"type": "remote_output", "text": line})
            return
        if not isinstance(value, dict):
            self.record({"type": "remote_output", "value": value})
            return
        self.record(value)
        if value.get("type") == "job" and value.get("state") in {
            "succeeded",
            "validated",
            "failed",
            "unknown",
            "cancelled",
        }:
            self.state = value["state"]


def run_job(
    config: Config,
    bundle: Path,
    job: Job,
    archive_path: Path,
    report_path: Path,
    *,
    check_only: bool = False,
    keep_remote: bool = False,
    cancel: threading.Event | None = None,
    on_event: Callable[[JsonObject], None] | None = None,
) -> int:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as stream:
        report = JobReport(stream, on_event)
        report.record(
            {"type": "local", "job_id": job.job_id, "state": "prepared", "check_only": check_only}
        )
        staged = False
        try:
            if cancel is not None and cancel.is_set():
                report.record({"type": "local", "state": "cancelled"})
                return 130
            transport.stage(config, bundle, job.job_id, archive_path)
            staged = True
            report.record({"type": "local", "state": "uploaded"})
            if cancel is not None and cancel.is_set():
                report.record({"type": "local", "state": "cancelled"})
                transport.cleanup(config, job.job_id)
                return 130
            code, cancelled = transport.run(
                config, job.job_id, report.on_line, check_only, cancel=cancel
            )
            if code == 255 or report.state is None:
                report.record(
                    {
                        "type": "local",
                        "state": "unknown",
                        "error": "SSH lost or no final executor result; not replayed",
                    }
                )
                print(
                    "Remote outcome unknown; not replayed. Uploaded files retained.",
                    file=sys.stderr,
                )
                return 1
            # Retain evidence when the executor itself cannot confirm the outcome.
            if not keep_remote and report.state not in {"unknown", "cancelled"}:
                try:
                    transport.cleanup(config, job.job_id)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    report.record({"type": "local", "state": "cleanup_failed", "error": str(exc)})
                else:
                    report.record({"type": "local", "state": "cleaned"})
            return (
                130
                if cancelled
                else (0 if code == 0 and report.state in {"succeeded", "validated"} else 1)
            )
        except BaseException as exc:
            report.record(
                {
                    "type": "local",
                    "state": "unknown" if staged else "upload_failed",
                    "error": str(exc),
                }
            )
            print(
                f"Remote work directory: {config.remote.work_dir}/{job.job_id}; files may remain after interruption.",
                file=sys.stderr,
            )
            raise
        finally:
            print(f"Local report: {report_path.resolve()}", flush=True)
