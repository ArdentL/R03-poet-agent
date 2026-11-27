"""Validated local task, configuration, timeline and bundle models."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from typing_extensions import Self

from .wire import ACTION_NAMES, PLAYBACK_RATE, ValidationError, read_json, write_json

Name = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")]
Milliseconds = Annotated[int, Field(ge=0, le=3_600_000)]
Seconds = Annotated[float, Field(ge=1, le=3600)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def integer_literals(cls, value: object) -> object:
        # Literal[1] otherwise accepts True because Python considers them equal.
        if isinstance(value, dict):
            for key in ("version", "source_version", "sample_rate", "channels"):
                if key in cls.model_fields and key in value and type(value[key]) is not int:
                    raise ValueError(f"{key}: expected an integer")
        return value


class SequenceEntry(Model):
    expression: Name
    action: str | None = None

    @field_validator("expression")
    @classmethod
    def nonblank_expression(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Expression must not be blank")
        return value

    @field_validator("action")
    @classmethod
    def known_action(cls, value: str | None) -> str | None:
        if value is not None and value not in ACTION_NAMES:
            raise ValueError(f"Unsupported action: {value}")
        return value


class RobotInventory(Model):
    expressions: Annotated[list[Name], Field(min_length=1)]
    actions: list[Name]


class Region(Model):
    start_ms: Milliseconds
    end_ms: Annotated[int, Field(ge=1, le=3_600_000)]

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("Region must be nonempty")
        return self


class Segment(SequenceEntry, Region):
    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def validate_regions(
    regions: list[Region], frames: int | None = None, rate: int = PLAYBACK_RATE
) -> None:
    previous = 0
    for region in regions:
        if region.start_ms < previous:
            raise ValidationError("Regions must be ordered and nonoverlapping")
        if frames is not None and region.end_ms * rate > frames * 1000:
            raise ValidationError("Timeline exceeds full audio")
        previous = region.end_ms


class ManualTask(Model):
    version: Literal[1]
    audio: Annotated[str, Field(min_length=1, max_length=4096)]
    segments: Annotated[list[Segment], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        validate_regions(list(self.segments))
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("Duplicate segment id")
        return self


class AutomaticTask(Model):
    version: Literal[2]
    audio: Annotated[str, Field(min_length=1, max_length=4096)]
    sequence: Annotated[list[SequenceEntry], Field(min_length=1, max_length=256)]


Task = ManualTask | AutomaticTask
TASK_ADAPTER = TypeAdapter(Annotated[Task, Field(discriminator="version")])


class PlaybackOptions(Model):
    expression_offset_ms: Annotated[int, Field(ge=-5000, le=5000)] = 0
    action_offset_ms: Annotated[int, Field(ge=-5000, le=5000)] = 0
    start_timeout: Seconds = 30
    end_timeout: Seconds = 120
    action_timeout: Seconds = 30
    discovery_timeout: Seconds = 10
    service_timeout: Seconds = 5
    lease_timeout: Seconds = 10


class SshConfig(Model):
    host: Annotated[
        str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:%-]*$")
    ]
    user: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+$")] | None = None
    port: Annotated[int, Field(ge=1, le=65535)] = 22
    identity_file: str | None = None
    known_hosts_file: str | None = None
    connect_timeout: Annotated[int, Field(ge=1, le=120)] = 10


class RemoteConfig(Model):
    ros_setup: str
    interfaces_setup: str
    work_dir: str
    domain_id: Annotated[int, Field(ge=0, le=232)]
    python: Annotated[str, Field(pattern=r"^[A-Za-z0-9_./+][A-Za-z0-9_./+-]*$")] = "python3"
    container: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")] | None = None
    dds_profile: str | None = None

    @field_validator("ros_setup", "interfaces_setup", "work_dir", "dds_profile")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        if value is not None:
            path = PurePosixPath(value)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or value.startswith("//")
                or re.search(r"[\x00-\x1f\x7f]", value)
            ):
                raise ValueError("Expected absolute POSIX path without '..' or control characters")
        return value

    @field_validator("work_dir")
    @classmethod
    def dedicated_directory(cls, value: str) -> str:
        if PurePosixPath(value) == PurePosixPath("/"):
            raise ValueError("work_dir must be a dedicated directory")
        return value


class AudioConfig(Model):
    ffmpeg: str | None = None


class Config(Model):
    ssh: SshConfig
    remote: RemoteConfig
    audio: AudioConfig = Field(default_factory=AudioConfig)
    playback: PlaybackOptions = Field(default_factory=PlaybackOptions)


class AudioInfo(Model):
    sample_rate: Annotated[int, Field(ge=8000, le=96000)]
    channels: Literal[1, 2]
    frames: Annotated[int, Field(gt=0)]

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


class Timeline(Model):
    version: Literal[2] = 2
    audio_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    playback_frames: Annotated[int, Field(gt=0)]
    playback_rate: Literal[24000] = PLAYBACK_RATE
    engine: str
    target_count: Annotated[int, Field(ge=1, le=256)]
    regions: Annotated[list[Region], Field(max_length=256)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        validate_regions(self.regions, self.playback_frames, self.playback_rate)
        return self


class AudioLevel(Model):
    target_peak_dbfs: Literal[-1.0] = -1.0
    input_peak_dbfs: float | None
    applied_db: float


class BundleAudio(Model):
    file: Literal["audio.wav"] = "audio.wav"
    frames: Annotated[int, Field(gt=0, le=PLAYBACK_RATE * 3600)]
    sample_rate: Literal[24000] = PLAYBACK_RATE
    channels: Literal[1] = 1
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Job(Model):
    version: Literal[2] = 2
    source_version: Literal[1, 2]
    job_id: Annotated[str, Field(pattern=r"^job-[0-9a-f]{32}$")]
    playback: PlaybackOptions
    segments: Annotated[list[Segment], Field(min_length=1, max_length=256)]
    audio_level: AudioLevel
    audio: BundleAudio

    @model_validator(mode="after")
    def ordered(self) -> Self:
        validate_regions(list(self.segments), self.audio.frames, self.audio.sample_rate)
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("Duplicate segment id")
        return self


def load_task(path: Path) -> Task:
    return TASK_ADAPTER.validate_python(read_json(path))


def load_config(path: Path) -> Config:
    config = Config.model_validate(read_json(path))
    for field in ("identity_file", "known_hosts_file"):
        value = getattr(config.ssh, field)
        if value:
            setattr(
                config.ssh, field, str((path.resolve().parent / Path(value).expanduser()).resolve())
            )
    return config


def save_model(path: Path, model: BaseModel) -> None:
    write_json(path, model.model_dump(mode="json"))
