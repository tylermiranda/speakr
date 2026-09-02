#!/usr/bin/env python3
"""sessionStart hook: inject a short project-memory briefing."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from .memory/hooks/
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import emit, read_stdin_json, read_text, repo_root_from_cwd  # noqa: E402


def main() -> int:
    _ = read_stdin_json()
    root = repo_root_from_cwd()
    memory = root / ".memory"
    if not memory.is_dir():
        emit({})
        return 0

    index = read_text(memory / "wiki" / "index.md", limit=2500)
    log = read_text(memory / "wiki" / "log.md", limit=1500)
    agents = read_text(root / "AGENTS.md", limit=2000)

    parts = [
        "Project memory is enabled for this repository.",
        "Follow AGENTS.md. Prefer .memory/wiki/ for durable facts; .memory/raw/ for ephemeral dumps.",
        "Do not invent facts missing from memory — gather evidence from the repo.",
    ]
    if agents:
        parts.append("AGENTS.md (excerpt):\n" + agents)
    if index:
        parts.append(".memory/wiki/index.md:\n" + index)
    if log:
        parts.append(".memory/wiki/log.md (excerpt):\n" + log)

    emit({"additionalContext": "\n\n".join(parts)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
