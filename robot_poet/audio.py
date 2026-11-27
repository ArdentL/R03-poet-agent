"""Full-audio conversion, automatic peak normalization and bundle preparation."""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
import warnings
import wave
from array import array
from pathlib import Path

from .analysis import ENGINE_VERSION, automatic_regions
from .model import (
    AudioInfo,
    AudioLevel,
    AutomaticTask,
    BundleAudio,
    Config,
    Job,
    ManualTask,
    Model,
    Region,
    Segment,
    Task,
    Timeline,
    save_model,
    validate_regions,
)
from .wire import (
    ANALYSIS_RATE,
    MAX_PCM_BYTES,
    PLAYBACK_RATE,
    ValidationError,
    file_sha256,
    read_json,
    write_json,
)

TARGET_PEAK_DBFS = -1.0
TARGET_PEAK_SAMPLE = int(32768 * 10 ** (TARGET_PEAK_DBFS / 20))


class AnalyzedAudio(Model):
    timeline: Timeline
    normalized: Path
    info: AudioInfo


def pcm_samples(raw: bytes) -> array:
    samples = array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def wav_info(path: Path) -> AudioInfo:
    with wave.open(str(path), "rb") as source:
        channels, width, rate, frames, codec, _ = source.getparams()
        if codec != "NONE" or width != 2 or channels not in (1, 2) or not 8000 <= rate <= 96000:
            raise ValidationError("Audio must be PCM S16LE WAV, 1 or 2 channels, 8000..96000 Hz")
        if not frames or frames * channels * width > MAX_PCM_BYTES:
            raise ValidationError("Empty audio or PCM exceeds 128 MiB")
        actual = sum(len(chunk) for chunk in iter(lambda: source.readframes(65536), b""))
        if actual != frames * channels * width:
            raise ValidationError("WAV is truncated or its frame count is invalid")
    return AudioInfo(sample_rate=rate, channels=channels, frames=frames)


def find_ffmpeg(override: str | None = None) -> str:
    if override:
        executable = shutil.which(override)
        if not executable:
            raise ValidationError(f"Configured FFmpeg executable not found: {override}")
        return executable
    if executable := shutil.which("ffmpeg"):
        return executable
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def normalize(
    source: Path, destination: Path, ffmpeg: str | None = None, *, rate: int = PLAYBACK_RATE
) -> AudioInfo:
    try:
        info = wav_info(source)
    except (wave.Error, EOFError, ValidationError):
        info = None
    if info is not None and info.sample_rate == rate and info.channels == 1:
        shutil.copyfile(source, destination)
    else:
        cap_seconds = min(3601, MAX_PCM_BYTES / (rate * 2))
        result = subprocess.run(
            [
                find_ffmpeg(ffmpeg),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(rate),
                "-ac",
                "1",
                "-t",
                str(cap_seconds),
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            raise ValidationError(f"FFmpeg failed: {result.stderr[-2000:]}")
        if wav_info(destination).duration >= cap_seconds - 0.001:
            raise ValidationError("Decoded audio exceeds duration/size limit")
    return wav_info(destination)


def normalize_peak(source: Path, destination: Path) -> AudioLevel:
    """Constant gain to -1 dBFS; digital silence stays silent and frame count stays exact."""
    with wave.open(str(source), "rb") as stream:
        peak = max(
            (
                max(abs(v) for v in pcm_samples(raw))
                for raw in iter(lambda: stream.readframes(65536), b"")
            ),
            default=0,
        )
        input_dbfs = 20 * math.log10(peak / 32768) if peak else None
        factor = TARGET_PEAK_SAMPLE / peak if peak else 1.0
        stream.rewind()
        with wave.open(str(destination), "wb") as output:
            output.setparams(stream.getparams())
            for raw in iter(lambda: stream.readframes(65536), b""):
                scaled = array("h", (round(v * factor) for v in pcm_samples(raw)))
                if sys.byteorder != "little":
                    scaled.byteswap()
                output.writeframesraw(scaled.tobytes())
    return AudioLevel(input_peak_dbfs=input_dbfs, applied_db=20 * math.log10(factor))


def analyze(
    task: Task, task_dir: Path, config: Config, scratch: Path, timeline_path: Path
) -> AnalyzedAudio:
    source = (task_dir / Path(task.audio).expanduser()).resolve()
    if not source.is_file():
        raise ValidationError(f"Audio file not found: {source}")
    if timeline_path.resolve() == source:
        raise ValidationError("Timeline output must not overwrite the audio source")
    normalized = scratch / "normalized.wav"
    info = normalize(source, normalized, config.audio.ffmpeg)
    if info.duration > 3600:
        raise ValidationError("Audio exceeds one hour")
    count = len(task.sequence) if isinstance(task, AutomaticTask) else len(task.segments)
    binding = {
        "version": 2,
        "audio_sha256": file_sha256(source),
        "playback_frames": info.frames,
        "playback_rate": info.sample_rate,
        "engine": ENGINE_VERSION,
        "target_count": count,
    }
    if isinstance(task, ManualTask):
        warnings.warn(
            "Version 1 now plays the FULL file, including unselected ranges; segments are control markers only.",
            stacklevel=2,
        )
        regions = [Region(start_ms=s.start_ms, end_ms=s.end_ms) for s in task.segments]
        timeline = Timeline(**binding, regions=regions)
    else:
        cached = read_json(timeline_path) if timeline_path.exists() else None
        if cached is not None and not isinstance(cached, dict):
            raise ValidationError("Timeline cache must be a JSON object")
        if cached is not None and all(cached.get(key) == value for key, value in binding.items()):
            timeline = Timeline.model_validate(cached)
        else:
            analysis_wav = scratch / "analysis-16k.wav"
            normalize(normalized, analysis_wav, config.audio.ffmpeg, rate=ANALYSIS_RATE)
            with wave.open(str(analysis_wav), "rb") as stream:
                pcm = stream.readframes(stream.getnframes())
            regions = automatic_regions(pcm, count)
            for region in regions:
                region.end_ms = min(region.end_ms, info.frames * 1000 // info.sample_rate)
            timeline = Timeline(**binding, regions=regions)
    save_model(timeline_path, timeline)
    return AnalyzedAudio(timeline=timeline, normalized=normalized, info=info)


def prepare(
    task: Task,
    task_dir: Path,
    config: Config,
    destination: Path,
    scratch: Path,
    job_id: str,
    timeline_path: Path,
) -> Job:
    analyzed = analyze(task, task_dir, config, scratch, timeline_path)
    regions, info = analyzed.timeline.regions, analyzed.info
    validate_regions(regions, info.frames, info.sample_rate)
    if isinstance(task, AutomaticTask):
        if len(regions) != len(task.sequence):
            raise ValidationError(
                f"Timeline has {len(regions)} regions but sequence has {len(task.sequence)}; edit {timeline_path}"
            )
        segments = [
            Segment(id=f"sentence-{i + 1:04d}", **region.model_dump(), **entry.model_dump())
            for i, (region, entry) in enumerate(zip(regions, task.sequence, strict=True))
        ]
    else:
        segments = task.segments
    destination.mkdir(parents=True, exist_ok=False)
    try:
        target = destination / "audio.wav"
        level = normalize_peak(analyzed.normalized, target)
        job = Job(
            source_version=task.version,
            job_id=job_id,
            playback=config.playback,
            segments=segments,
            audio_level=level,
            audio=BundleAudio(frames=info.frames, sha256=file_sha256(target)),
        )
        for filename in ("remote.py", "wire.py"):
            shutil.copyfile(Path(__file__).with_name(filename), destination / filename)
        save_model(destination / "job.json", job)
        write_json(destination / "job.schema.json", Job.model_json_schema())
        return job
    except BaseException:
        shutil.rmtree(destination)
        raise
