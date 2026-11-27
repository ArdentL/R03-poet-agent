"""Local pause detection and target-count boundary selection; playback PCM is untouched."""

from __future__ import annotations

import bisect
import math
import sys
from array import array
from collections.abc import Callable
from typing import Protocol

from .model import Region
from .wire import ANALYSIS_RATE, ValidationError

ENGINE_VERSION = "webrtcvad-energy-v3"


class VoiceDetector(Protocol):
    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...


def automatic_regions(
    pcm: bytes, count: int, vad_factory: Callable[[int], VoiceDetector] | None = None
) -> list[Region]:
    """Pick count-1 observed pauses globally; never manufacture equal-duration cuts."""
    if vad_factory is None:
        import webrtcvad

        vad_factory = webrtcvad.Vad
    rate, window = ANALYSIS_RATE, 30
    min_speech_ms = 300
    min_segment_ms = min(1000, max(300, len(pcm) / (rate * 2) * 1000 / count * 0.4))
    size = rate * window // 1000 * 2
    detectors = [vad_factory(mode) for mode in (2, 3)]
    voiced, energies, edges = [], [], [0]
    for offset in range(0, len(pcm), size):
        raw = pcm[offset : offset + size]
        frame = raw.ljust(size, b"\0")
        votes = [vad.is_speech(frame, rate) for vad in detectors]
        voiced.append(any(votes))
        samples = array("h", raw)
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(value * value for value in samples) / len(samples))
        energies.append(20 * math.log10(max(rms, 1e-6) / 32768))
        edges.append(min((offset + size) // 2 * 1000 // rate, len(pcm) * 1000 // (2 * rate)))
    speech = [i for i, flag in enumerate(voiced) if flag]
    if not speech or edges[-1] == 0:
        raise ValidationError("No speech detected; supply a manual v1 timeline")
    first, last = speech[0], speech[-1] + 1
    # Relative to a robust speech level so quiet recordings need no fixed dB threshold.
    speech_energy = sorted(energies[i] for i in speech)
    reference = speech_energy[int((len(speech_energy) - 1) * 0.75)]
    threshold = reference - 14
    quiet = [not flag or energy <= threshold for flag, energy in zip(voiced, energies)]
    candidates, begin = [], None
    for index in range(first, last + 1):
        if index < last and quiet[index]:
            if begin is None:
                begin = index
        elif begin is not None:
            duration = edges[index] - edges[begin]
            if begin > first and index < last and duration >= 150:
                depth = max(0, reference - sum(energies[begin:index]) / (index - begin))
                candidates.append((begin, index, duration + min(depth, 40) * 3))
            begin = None
    prefix = [0]
    for i, flag in enumerate(voiced):
        # Low-energy windows inside a gap do not satisfy the minimum speech constraint.
        prefix.append(prefix[-1] + (edges[i + 1] - edges[i] if flag and not quiet[i] else 0))

    def valid(start: int, end: int) -> bool:
        return (
            edges[end] - edges[start] >= min_segment_ms
            and prefix[end] - prefix[start] >= min_speech_ms
        )

    if len(candidates) > 4096:
        raise ValidationError(
            "Too many pause candidates; split this long task or use a manual timeline"
        )
    cuts = count - 1
    # Each layer stores the best score ending at a candidate. Monotone prefix lookup
    # finds compatible previous cuts in O(log pauses), avoiding combinatorial search.
    parents = []
    scores = [score if valid(first, a) else -math.inf for a, _, score in candidates]
    parents.append([-1] * len(candidates))
    ends = [edges[b] for _, b, _ in candidates]
    speech_ends = [prefix[b] for _, b, _ in candidates]
    for _ in range(1, cuts):
        best_scores, best_indices, best, best_index = [], [], -math.inf, -1
        for i, score in enumerate(scores):
            if score > best:
                best, best_index = score, i
            best_scores.append(best)
            best_indices.append(best_index)
        next_scores, previous = [], []
        for i, (a, _, score) in enumerate(candidates):
            limit = min(
                i - 1,
                bisect.bisect_right(ends, edges[a] - min_segment_ms) - 1,
                bisect.bisect_right(speech_ends, prefix[a] - min_speech_ms) - 1,
            )
            next_scores.append(best_scores[limit] + score if limit >= 0 else -math.inf)
            previous.append(best_indices[limit] if limit >= 0 else -1)
        scores = next_scores
        parents.append(previous)
    chosen = []
    if cuts:
        eligible = [
            i
            for i, (_, b, _) in enumerate(candidates)
            if valid(b, last) and math.isfinite(scores[i])
        ]
        if not eligible:
            raise ValidationError(
                f"Cannot find {count} reliable regions from {len(candidates)} pauses; "
                "Use a manual version 1 timeline for this recording. "
                "Audio will not be divided evenly."
            )
        cursor = max(eligible, key=lambda i: scores[i])
        for layer in range(cuts - 1, -1, -1):
            chosen.append(candidates[cursor])
            cursor = parents[layer][cursor]
        chosen.reverse()
    elif prefix[last] - prefix[first] < min_speech_ms:
        raise ValidationError("Speech is shorter than 300 ms")
    regions, start = [], first
    for a, b, _ in chosen:
        regions.append(Region(start_ms=edges[start], end_ms=edges[a]))
        start = b
    regions.append(Region(start_ms=edges[start], end_ms=edges[last]))
    return regions
