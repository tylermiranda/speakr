# Project memory log

Append-only changelog of memory operations and notable session captures.

## Entries

- (2026-10-05) Workflow tag **Filed** (Tower + Mac Speakr tag id 12): personal label only — color `#15803d`, no custom prompt / transcription hint / ASR defaults. Marks that a meeting summary was filed into a secondary system. Doc: `/Users/tyler/Documents/Speakr/filed-workflow-tag.md` (not a `*-tag.md` pack; not synced by `sync-speakr-tags.py`).
- (2026-10-02) Bidirectional peer sync after split use: `--direction both --limit 10000` pulled 17 Tower-only packages onto Mac; Mac→Tower push already empty. Post: Mac 110 / Tower 109 completed, 108 shared hashes. Leftover Tower-only: empty-transcription `sysaudio-…-Note` (id 2; sync skips). Runbook: `docs/ops/mac-primary-tower-standby.md`.
- (2026-09-29) New tag **IT Conference Session** (Tower + Mac Speakr tag id 11, transcription hint id 15): in-person conference session capture (keynote/breakout/panel/Q&A); speakers 1–8; color `#0369a1`. Pack: `/Users/tyler/Documents/Speakr/it-conference-session-tag.md`. Sync script now pushes to Tower LAN + Mac `127.0.0.1:8899`.
- (2026-09-11) New tag **HelpDesk VDI Web Tool 2.0 Planning** (Speakr tag id 10, transcription hint id 14): decision-heavy project-doc digest; speakers 2–4; color `#d97706`. Pack: `/Users/tyler/Documents/Speakr/helpdesk-vdi-web-tool-2.0-planning-tag.md`.
- (2026-09-10) Manager 1:1 work-only: Tag prompt hardened (exclude sports/personal; mandatory full-transcript work scan). Synced to Speakr tag id 3. Raised `transcript_length_limit` to `-1`. Recording 62 summary/title corrected to Windows 365 / on-prem work notes. See [manager-1on1-work-only-summaries](concepts/manager-1on1-work-only-summaries.md).
- (2026-09-03) Summary identity metadata: Context + standing output requirement for title/date/time/duration/participants; default prompt + admin preview updated. Tag packs under Documents/Speakr aligned and live tags updated via API.
- (install) Project memory scaffold created. See [agent-memory-setup](systems/agent-memory-setup.md).
