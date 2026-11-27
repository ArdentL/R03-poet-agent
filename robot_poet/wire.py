"""Dependency-free JSON I/O and bundle contract shared with the robot executor.

The schema is exported by the local Pydantic Job model. This module validates
that schema on the robot without installing Pydantic or duplicating field rules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

PLAYBACK_RATE = 24000
ANALYSIS_RATE = 16000
CHUNK_MS = 100
SEND_INTERVAL_SECONDS = 0.002
MAX_PCM_BYTES = 128 * 1024 * 1024
BUNDLE_FILES = frozenset({"job.json", "job.schema.json", "remote.py", "wire.py", "audio.wav"})
ACTION_NAMES = frozenset(
    {
        "手垂下",
        "指向右前",
        "指向右边",
        "指向左前",
        "指向左边",
        "欢迎",
        "自我介绍",
        "自检",
        "默认_body",
    }
)
JsonObject = dict[str, Any]


class ValidationError(ValueError):
    """Invalid task, audio or bundle data."""


def unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    if path.stat().st_size > 1024 * 1024:
        raise ValidationError("JSON exceeds 1 MiB")
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=unique_object)


def write_json(path: Path, value: Any) -> None:
    """Atomic replacement, including when two local analysis commands overlap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_schema(
    value: Any, schema: JsonObject, root: JsonObject | None = None, location: str = "job"
) -> None:
    """Validate the JSON Schema subset emitted by Job.model_json_schema()."""
    if root is None:
        root = schema
    if "$ref" in schema:
        reference = root
        for key in schema["$ref"].removeprefix("#/").split("/"):
            reference = reference[key]
        validate_schema(value, reference, root, location)
        return
    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            try:
                validate_schema(value, alternative, root, location)
                return
            except ValidationError:
                pass
        raise ValidationError(f"{location}: no matching schema alternative")
    kind = schema.get("type")
    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if kind is not None and not valid_type.get(kind, False):
        raise ValidationError(f"{location}: expected {kind}")
    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{location}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{location}: invalid choice")
    if kind == "object":
        properties = schema.get("properties", {})
        missing = set(schema.get("required", ())) - value.keys()
        extra = (
            value.keys() - properties.keys()
            if schema.get("additionalProperties") is False
            else set()
        )
        if missing or extra:
            raise ValidationError(f"{location}: missing={sorted(missing)}, unknown={sorted(extra)}")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], root, f"{location}.{key}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise ValidationError(f"{location}: invalid item count")
        for index, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), root, f"{location}[{index}]")
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            raise ValidationError(f"{location}: invalid string length")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValidationError(f"{location}: invalid string")
    elif kind in ("number", "integer"):
        if not math.isfinite(value) or not schema.get("minimum", -math.inf) <= value <= schema.get(
            "maximum", math.inf
        ):
            raise ValidationError(f"{location}: number outside bounds")
        if value <= schema.get("exclusiveMinimum", -math.inf) or value >= schema.get(
            "exclusiveMaximum", math.inf
        ):
            raise ValidationError(f"{location}: number outside exclusive bounds")
