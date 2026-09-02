# AGENTS.md — Project Memory

This repository uses a shared, version-controlled project memory under `.memory/`.
It is **project-only**: do not create or require a personal PKM vault.

## Read first

Before substantive work in a new session:

1. Read this file.
2. Read `.memory/wiki/index.md`.
3. Skim `.memory/wiki/log.md` for recent changes.
4. Open any linked pages that match the current task.

## Write rules

- Put durable knowledge in `.memory/wiki/` (systems, decisions, entities, concepts).
- Put ephemeral dumps, transcripts, and scratch captures in `.memory/raw/`.
- Prefer updating an existing wiki page over creating duplicates.
- Keep pages concise; link out instead of pasting large code or secrets.
- After meaningful work, append a short entry to `.memory/wiki/log.md` and refresh `index.md` if you added pages.

## Safety

- Never store secrets, tokens, passwords, or private personal data in `.memory/`.
- Do not delete wiki history casually; prefer correcting pages in place and noting the change in `log.md`.
- Do not invent project facts. If memory is missing, say so and gather evidence from the repo.

## Hooks

Python hooks live in `.memory/hooks/` (stdlib only):

| Hook | Role |
|------|------|
| `session_context.py` | Injects a short memory briefing at session start |
| `edit_tracker.py` | Appends tool/edit signals to `.memory/raw/session-edits.jsonl` |
| `closeout_prompt.py` | Writes a close-out capture checklist under `.memory/raw/` |

Wire Copilot CLI hooks with `Setup-ProjectMemoryHooks.py` (or `.ps1` / `.sh` on Windows/macOS).

## Close-out

Before ending a substantial session, capture:

- What changed and why
- Decisions made
- Open questions / next steps

Update the relevant wiki pages and `log.md`.
