#!/usr/bin/env python3
"""Wire project-memory hooks into the user's Copilot CLI config (~/.copilot/hooks/)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def python_cmd() -> str:
    return "python" if sys.platform == "win32" else "python3"


def main() -> int:
    root = Path.cwd().resolve()
    hooks_src = root / ".memory" / "hooks"
    if not hooks_src.is_dir():
        print(
            f"error: missing {hooks_src} — run install-project-memory first",
            file=sys.stderr,
        )
        return 1

    # Prefer importing the skill installer helper if present beside templates,
    # otherwise duplicate the user-hook writer inline for portability.
    home = Path(os.environ.get("COPILOT_HOME") or (Path.home() / ".copilot"))
    hooks_dir = home / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    py = python_cmd()

    def bash(script: str) -> str:
        return f'if [ -f .memory/hooks/{script} ]; then {py} .memory/hooks/{script}; fi'

    def powershell(script: str) -> str:
        return f'if (Test-Path .memory/hooks/{script}) {{ {py} .memory/hooks/{script} }}'

    payload = {
        "version": 1,
        "hooks": {
            "sessionStart": [
                {
                    "type": "command",
                    "bash": bash("session_context.py"),
                    "powershell": powershell("session_context.py"),
                    "cwd": ".",
                    "timeoutSec": 20,
                }
            ],
            "postToolUse": [
                {
                    "type": "command",
                    "matcher": ".*",
                    "bash": bash("edit_tracker.py"),
                    "powershell": powershell("edit_tracker.py"),
                    "cwd": ".",
                    "timeoutSec": 10,
                }
            ],
            "sessionEnd": [
                {
                    "type": "command",
                    "bash": bash("closeout_prompt.py"),
                    "powershell": powershell("closeout_prompt.py"),
                    "cwd": ".",
                    "timeoutSec": 15,
                }
            ],
        },
    }

    # Also refresh repo-local hooks for teammates / cloud agent
    project_hooks = root / ".github" / "hooks" / "project-memory.json"
    project_hooks.parent.mkdir(parents=True, exist_ok=True)
    project_payload = {
        "version": 1,
        "hooks": {
            "sessionStart": [
                {
                    "type": "command",
                    "bash": f"{py} .memory/hooks/session_context.py",
                    "powershell": f"{py} .memory/hooks/session_context.py",
                    "cwd": ".",
                    "timeoutSec": 20,
                }
            ],
            "postToolUse": [
                {
                    "type": "command",
                    "matcher": ".*",
                    "bash": f"{py} .memory/hooks/edit_tracker.py",
                    "powershell": f"{py} .memory/hooks/edit_tracker.py",
                    "cwd": ".",
                    "timeoutSec": 10,
                }
            ],
            "sessionEnd": [
                {
                    "type": "command",
                    "bash": f"{py} .memory/hooks/closeout_prompt.py",
                    "powershell": f"{py} .memory/hooks/closeout_prompt.py",
                    "cwd": ".",
                    "timeoutSec": 15,
                }
            ],
        },
    }
    project_hooks.write_text(json.dumps(project_payload, indent=2) + "\n", encoding="utf-8")

    out = hooks_dir / "project-memory.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Wrote {project_hooks}")
    print("Restart Copilot CLI to reload hooks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
