# Agent memory setup

This repository uses a shared `.memory/` tree so AI agents can carry context across sessions without a personal PKM vault.

## Layout

```text
.memory/
  hooks/           # stdlib Python hooks (session, edits, close-out)
  raw/             # ephemeral dumps / session artifacts
  wiki/
    index.md       # catalog — read early
    log.md         # append-only activity log
    systems/       # how the project works
    decisions/     # durable decisions
    entities/      # named things
    concepts/      # patterns and ideas
AGENTS.md          # read/write + safety rules
Setup-ProjectMemoryHooks.*  # wire Copilot CLI hooks (py / ps1 / sh)
.github/hooks/project-memory.json  # repo-local Copilot hooks
```

## Agent protocol

1. **Start**: read `AGENTS.md`, then `wiki/index.md`, then recent `wiki/log.md`.
2. **During work**: update wiki pages when you learn durable facts; dump scratch to `raw/`.
3. **End**: run close-out capture — update pages + append `log.md`.

## Hooks

Hooks are Python 3 standard-library scripts. Copilot CLI config includes both `bash` and `powershell` command keys so the same project works on macOS and Windows.

Re-run setup after moving the repo:

```bash
python3 Setup-ProjectMemoryHooks.py
```

```powershell
python .\Setup-ProjectMemoryHooks.py
# or
.\Setup-ProjectMemoryHooks.ps1
```
