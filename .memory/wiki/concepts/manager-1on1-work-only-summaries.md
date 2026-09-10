# Manager 1:1 — work-only summaries

The **Manager 1:1** tag (`/Users/tyler/Documents/Speakr/manager-1on1-tag.md`, Speakr tag id 3) must produce a **work-only** personal working record.

## Rules (product intent)

- Never include personal life, sports, hobbies, weekend/holiday small talk, or other non-work chat — not even as “topics covered.”
- Always scan the **full** transcript; work often sits in the middle/final third after a long casual opener.
- Title from work content, not social themes.
- Prefer capturing real work over writing “No work topics discussed.”

## Ops notes

- Tag sync: `python3 ~/.cursor/skills/speakr-create-prompt-tag/scripts/sync-speakr-tags.py` (LAN + API key from PKM `secrets.md`; key heading is `### API Key`).
- System setting `transcript_length_limit` was raised from `30000` to `-1` (no limit) on 2026-09-10 so long 1:1s are not truncated mid-work. Default in code remains 30000 if re-initialized.
- Example incident: recording 62 (2026-09-10) — first summaries kept sports / claimed no work despite Windows 365 / Jira / Zscaler discussion near the end; fixed via prompt + unlimited transcript + corrected summary/title.

## Related

- [Summary identity metadata](summary-identity-metadata.md)
