#!/usr/bin/env python3
"""Shared helpers for project-memory hooks (stdlib only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def repo_root_from_cwd() -> Path:
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / ".memory").is_dir() and (candidate / "AGENTS.md").exists():
            return candidate
        if (candidate / ".memory" / "wiki" / "index.md").exists():
            return candidate
    return cwd


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def read_text(path: Path, limit: int = 4000) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > limit:
        return text[:limit] + "\n…(truncated)…"
    return text
