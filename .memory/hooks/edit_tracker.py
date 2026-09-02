#!/usr/bin/env python3
"""postToolUse hook: append lightweight edit/tool signals to raw session log."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import emit, read_stdin_json, repo_root_from_cwd  # noqa: E402


def main() -> int:
    payload = read_stdin_json()
    root = repo_root_from_cwd()
    raw_dir = root / ".memory" / "raw"
    if not (root / ".memory").is_dir():
        emit({})
        return 0

    raw_dir.mkdir(parents=True, exist_ok=True)
    out = raw_dir / "session-edits.jsonl"

    tool = (
        payload.get("toolName")
        or payload.get("tool_name")
        or payload.get("tool")
        or "unknown"
    )
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "toolName": tool,
        "cwd": str(root),
    }
    # Keep a small subset of useful fields without dumping huge payloads
    for key in ("filePath", "file_path", "path", "paths", "command"):
        if key in payload:
            entry[key] = payload[key]

    try:
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

    emit({})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
