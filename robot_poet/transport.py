"""OpenSSH transport, with quoted commands and a bounded stdin heartbeat lease."""

from __future__ import annotations

import json
import queue
import shlex
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from .model import Config, RobotInventory
from .wire import BUNDLE_FILES, ValidationError

STAGE_CODE = r"""
import json, pathlib, re, shutil, sys, tarfile
root = pathlib.Path(sys.argv[1]).resolve()
job_id = sys.argv[2]
if not re.fullmatch(r"job-[0-9a-f]{32}", job_id):
    raise ValueError("Invalid job id")
root.mkdir(parents=True, exist_ok=True)
target = root / job_id
target.mkdir(mode=0o700, exist_ok=False)
try:
    total, seen = 0, set()
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        for member in archive:
            name = member.name
            allowed = name in ("job.json", "job.schema.json", "remote.py", "wire.py", "audio.wav")
            if not allowed or not member.isfile() or name in seen or member.size < 0:
                raise ValueError("Invalid bundle member")
            seen.add(name)
            total += member.size
            if total > 130 * 1024 * 1024 or len(seen) > 5:
                raise ValueError("Bundle too large")
            source = archive.extractfile(member)
            with (target / name).open("xb") as output:
                shutil.copyfileobj(source, output)
    if not {"job.json", "job.schema.json", "remote.py", "wire.py", "audio.wav"} <= seen:
        raise ValueError("Incomplete bundle")
    print(json.dumps({"staged": job_id}))
except BaseException:
    shutil.rmtree(target)
    raise
"""

CLEAN_CODE = r"""
import pathlib, re, shutil, sys
root = pathlib.Path(sys.argv[1]).resolve()
name = sys.argv[2]
if not re.fullmatch(r"job-[0-9a-f]{32}", name):
    raise ValueError("Invalid job id")
target = root / name
if target.is_symlink() or target.resolve().parent != root:
    raise ValueError("Unsafe cleanup path")
if target.exists():
    shutil.rmtree(target)
"""


INVENTORY_CODE = r"""
import json, sys
import rclpy
from hal_interfaces.srv import HalUnifiedCall
rclpy.init()
node = rclpy.create_node("robot_poet_inventory")
try:
    client = node.create_client(HalUnifiedCall, "/hal/unified/call")
    if not client.wait_for_service(timeout_sec=float(sys.argv[1])):
        raise RuntimeError("Robot control service unavailable")
    result = {}
    for key, command in (("expressions", "get_expression_list"), ("actions", "get_action_list")):
        request = HalUnifiedCall.Request()
        request.cmd = command
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=float(sys.argv[2]))
        if not future.done():
            raise RuntimeError(command + " timed out")
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(command + " rejected")
        result[key] = list(response.str_results)
    print(json.dumps(result, ensure_ascii=False))
finally:
    node.destroy_node()
    rclpy.shutdown()
"""


def ssh_args(config: Config) -> list[str]:
    ssh = config.ssh
    args = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={ssh.connect_timeout}",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        "-p",
        str(ssh.port),
    ]
    for key, flag in (("identity_file", "-i"), ("known_hosts_file", "-o")):
        if getattr(ssh, key):
            value = (
                getattr(ssh, key)
                if key == "identity_file"
                else f"UserKnownHostsFile={getattr(ssh, key)}"
            )
            args.extend((flag, value))
    if ssh.user:
        args.extend(("-l", ssh.user))
    args.append(ssh.host)
    return args


def execution_command(config: Config, job_id: str, check_only: bool = False) -> str:
    remote = config.remote
    directory = str(PurePosixPath(remote.work_dir) / job_id)
    argv = [remote.python, "-u", directory + "/remote.py", "--bundle", directory, "--managed"]
    if check_only:
        argv.append("--check")
    return ros_command(config, argv)


def ros_command(config: Config, argv: list[str]) -> str:
    remote = config.remote
    lines = [
        "set -e",
        f"source {shlex.quote(remote.ros_setup)} >/dev/null",
        f"source {shlex.quote(remote.interfaces_setup)} >/dev/null",
        f"export ROS_DOMAIN_ID={remote.domain_id}",
    ]
    if remote.dds_profile:
        profile = shlex.quote(remote.dds_profile)
        lines.extend(
            (
                f"export FASTRTPS_DEFAULT_PROFILES_FILE={profile}",
                f"export FASTDDS_DEFAULT_PROFILES_FILE={profile}",
            )
        )
    lines.append("exec " + shlex.join(argv))
    return remote_command(config, ["bash", "-c", "\n".join(lines)])


def remote_command(config: Config, argv: list[str]) -> str:
    container = config.remote.container
    if container:
        argv = ["docker", "exec", "-i", container] + argv
    return shlex.join(argv)


def inventory(config: Config) -> RobotInventory:
    """Read capabilities before generating audio; never publish control messages."""
    options = config.playback
    command = ros_command(
        config,
        [
            config.remote.python,
            "-c",
            INVENTORY_CODE,
            str(options.discovery_timeout),
            str(options.service_timeout),
        ],
    )
    result = subprocess.run(
        ssh_args(config) + [command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=config.ssh.connect_timeout
        + options.discovery_timeout
        + 2 * options.service_timeout
        + 10,
    )
    if result.returncode:
        raise RuntimeError(f"读取机器人能力失败：{result.stderr[-2000:]}")
    return RobotInventory.model_validate_json(result.stdout)


def stage(config: Config, bundle: Path, job_id: str, archive_path: Path) -> None:
    if {path.name for path in bundle.iterdir()} != BUNDLE_FILES:
        raise ValidationError("Unexpected bundle members")
    with tarfile.open(archive_path, "w") as archive:
        for path in sorted(bundle.iterdir()):
            if not path.is_file() or path.is_symlink():
                raise ValidationError("Bundle contains unexpected non-file entry")
            archive.add(path, arcname=path.name, recursive=False)
    remote = config.remote
    command = remote_command(config, [remote.python, "-c", STAGE_CODE, remote.work_dir, job_id])
    with archive_path.open("rb") as stream:
        result = subprocess.run(
            ssh_args(config) + [command],
            stdin=stream,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    if result.returncode:
        raise RuntimeError(f"SSH upload failed ({result.returncode}): {result.stderr[-2000:]}")
    try:
        response = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Unexpected SSH upload output; check shell startup output") from exc
    if response != {"staged": job_id}:
        raise RuntimeError("Upload acknowledgement mismatch")


def cleanup(config: Config, job_id: str) -> None:
    remote = config.remote
    command = remote_command(config, [remote.python, "-c", CLEAN_CODE, remote.work_dir, job_id])
    result = subprocess.run(
        ssh_args(config) + [command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"Remote cleanup failed: {result.stderr[-2000:]}")


def run(
    config: Config,
    job_id: str,
    on_line: Callable[[str], None],
    check_only: bool = False,
    *,
    cancel: threading.Event | None = None,
) -> tuple[int, bool]:
    """No retries: SSH failure means the current playback outcome may be unknown."""
    if cancel is not None and cancel.is_set():
        return 130, True
    process = subprocess.Popen(
        ssh_args(config) + [execution_command(config, job_id, check_only)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    stop = threading.Event()
    write_lock = threading.Lock()

    def write(command: str) -> bool:
        with write_lock:
            try:
                process.stdin.write(command + "\n")
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def heartbeat() -> None:
        while not stop.is_set() and process.poll() is None:
            if not write("heartbeat"):
                return
            if stop.wait(0.5):
                return

    feeder = threading.Thread(target=heartbeat, daemon=True)
    feeder.start()
    output = queue.Queue()

    def read_output() -> None:
        try:
            for line in process.stdout:
                output.put(line.rstrip("\n"))
        finally:
            output.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    cancelled = False
    cancel_deadline = None
    try:
        while True:
            try:
                if cancel is not None and cancel.is_set() and not cancelled:
                    cancelled = True
                    stop.set()
                    write("cancel")
                    cancel_deadline = time.monotonic() + 5
                if cancel_deadline is not None and time.monotonic() > cancel_deadline:
                    returncode = 255
                    break
                try:
                    line = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    returncode = process.wait(timeout=5)
                    break
                on_line(line)
            except KeyboardInterrupt:
                if cancelled:
                    returncode = 255
                    break
                cancelled = True
                stop.set()
                write("cancel")
                # EOF/expired heartbeats also stop the robot executor after SSH loss.
                process.stdin.close()
                cancel_deadline = time.monotonic() + 5
    finally:
        stop.set()
        feeder.join(timeout=1)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()
    return returncode, cancelled
