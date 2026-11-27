"""Offline, guarded JSON Patch subset. Writes a NEW candidate, never an active config."""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


def apply(document: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(document)

    def locate(path: str) -> tuple[Any, str | int]:
        parts = path.lstrip("/").split("/")
        current = result
        for key in parts[:-1]:
            current = current[int(key)] if isinstance(current, list) else current[key]
        key = int(parts[-1]) if isinstance(current, list) else parts[-1]
        return current, key

    for operation in operations:
        parent, key = locate(operation["path"])
        kind = operation["op"]
        if kind == "test":
            if parent[key] != operation["value"]:
                raise ValueError(f"Patch prerequisite changed: {operation['path']}")
        elif kind == "copy":
            if key in parent:
                raise ValueError("Dedicated template already exists; do not overwrite")
            source, source_key = locate(operation["from"])
            parent[key] = copy.deepcopy(source[source_key])
        elif kind == "remove":
            del parent[key]
        elif kind == "replace":
            if key not in parent:
                raise ValueError("Missing replacement target")
            parent[key] = operation["value"]
        else:
            raise ValueError(f"Unsupported patch operation: {kind}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specs", type=Path)
    parser.add_argument("settings", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    settings = args.settings.read_text(encoding="utf-8")
    active = re.search(r"^active_head_id:\s*[\"\x27]?([^\"\x27\s#]+)", settings, re.M)
    if not active or active.group(1) != "xishiG3_S02_ID06":
        raise ValueError("Active head changed; review and regenerate patch")
    specs = json.loads(args.specs.read_text(encoding="utf-8"))
    if args.rollback:
        if any(
            h.get("template") == "robot_poet_mouth_only_v1"
            for name, h in specs["head_instances"].items()
            if name != active.group(1)
        ):
            raise ValueError("Another head now uses this template; cannot remove it")
    filename = "head-mouth-only.rollback.json" if args.rollback else "head-mouth-only.patch.json"
    result = apply(
        specs, json.loads(Path(__file__).with_name(filename).read_text(encoding="utf-8"))
    )
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        f"Review candidate: {args.output}; no active config changed; head node reload required after manual install."
    )


if __name__ == "__main__":
    main()
