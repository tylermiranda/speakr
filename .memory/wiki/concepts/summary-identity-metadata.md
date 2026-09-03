# Summary identity metadata

Summarization always requires **Title**, **Date**, **Time**, **Duration**, and **Participants** near the top of the summary output.

- Runtime: `generate_summary_only_task` in `src/tasks/processing.py` puts recording title/date/time/duration/participants in Context and appends a standing instruction on every prompt path.
- Default prompt: `src/config/prompts.py` `DEFAULT_SUMMARY_PROMPT`.
- Personal meeting tag packs: `/Users/tyler/Documents/Speakr/*-tag.md` (re-paste into Speakr tags to refresh saved custom prompts).

See PKM: `wiki/projects/speakr/2026-09-03-summary-identity-metadata.md`.
