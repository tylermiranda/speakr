#!/usr/bin/env python3
"""sessionEnd hook: write a close-out capture checklist for agents/humans."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import emit, read_stdin_json, repo_root_from_cwd  # noqa: E402


CHECKLIST = """# Pending close-out capture

Generated: {ts}

Before leaving this session, capture durable knowledge:

- [ ] Update relevant pages under `.memory/wiki/` (systems / decisions / entities / concepts)
- [ ] Append a short entry to `.memory/wiki/log.md`
- [ ] Refresh `.memory/wiki/index.md` if new pages were added
- [ ] Keep secrets out of `.memory/`
- [ ] Move any useful dumps from `.memory/raw/` into wiki pages, then prune noise

Session reason: {reason}
"""


def main() -> int:
    payload = read_stdin_json()
    root = repo_root_from_cwd()
    memory = root / ".memory"
    if not memory.is_dir():
        emit({})
        return 0

    raw_dir = memory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    reason = payload.get("reason") or payload.get("Reason") or "unknown"
    ts = datetime.now(timezone.utc).isoformat()
    path = raw_dir / "pending-closeout.md"
    try:
        path.write_text(
            CHECKLIST.format(ts=ts, reason=reason),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"closeout_prompt: failed to write {path}: {exc}", file=sys.stderr)

    print(
        "project-memory: wrote .memory/raw/pending-closeout.md — capture wiki updates before ending.",
        file=sys.stderr,
    )
    # sessionEnd cannot inject additionalContext; empty stdout is correct.
    emit({})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
