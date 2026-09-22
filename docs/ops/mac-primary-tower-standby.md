# Mac Speakr primary + Tower hot standby

## Roles

| Role | Instance | URL | Behavior |
|------|----------|-----|----------|
| Primary | Mac Speakr | `http://127.0.0.1:8899` | Companion + day-to-day UI; local whispermlx ASR; pushes COMPLETED packages, taxonomy, settings, metadata to Tower |
| Hot standby | Tower | `https://speakr.workspace-api.com` | Receives Mac replicate continuously; **does not** push back during normal ops (`PEER_SYNC_ROLE=standby` or unset `PEER_SYNC_*`) |

Passwords stay instance-local. Optional one-time: set Mac admin password to match Tower (1Password) — not continuous sync.

## Healthy day-to-day

1. Companion host → `http://127.0.0.1:8899` (Mac Speakr), with local API token.
2. LaunchAgent `com.tyler.speakr-mac` keeps Docker lite + fork overlay up.
3. LaunchAgent `com.tyler.speakr-peer-sync` runs every ~5m with `--direction push` (Mac→Tower).
4. On COMPLETED, Mac also pushes immediately when `PEER_SYNC_BASE_URL` + `PEER_SYNC_TOKEN` + `PEER_SYNC_ROLE=primary` are set in `~/Library/Application Support/speakr/config/.env`.

## Failover (Mac down)

1. Point Companion / browser at Tower: `https://speakr.workspace-api.com` + Tower API token.
2. Continue recording/uploads on Tower while Mac is offline.
3. Do **not** enable Tower→Mac auto-push.

## Recovery (Mac back — mode 2A)

When Mac Speakr is healthy again:

```bash
# One-shot merge Tower → Mac (new hashes + taxonomy + settings + metadata)
python3 "$HOME/Library/Application Support/speakr/bin/speakr-peer-sync.py" --mode recovery -v

# Switch Companion back to Mac
# Host: http://127.0.0.1:8899

# Resume primary push (LaunchAgent already defaults to direction=push)
python3 "$HOME/Library/Application Support/speakr/bin/speakr-peer-sync.py" --direction push -v
```

Conflict rules on recovery:

- Same `file_hash` → skip create (no duplicate audio row).
- Metadata last-write-wins by `sync_updated_at` / `updated_at`; recovery pull uses `force` so Tower catalog wins for that pass.

## Date backfill (one-shot)

If older peer_sync rows still show “today” for upload date:

```bash
python3 "$HOME/Library/Application Support/speakr/bin/speakr-peer-sync.py" --backfill-dates -v
```

This sets Mac `created_at` / `completed_at` / `meeting_date` from Tower by `file_hash`.

## Sync surfaces

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/recordings/sync` | Package import (audio + transcript + dates + tags) |
| `GET /api/v1/recordings/sync/manifest` | Hash inventory |
| `POST /api/v1/recordings/sync/metadata` | Metadata-only by `file_hash` |
| `GET/PUT /api/v1/sync/taxonomy` | Tags + folders by name |
| `GET/PUT /api/v1/sync/settings-bundle` | Allowlisted settings + user templates (no secrets) |

## Out of scope

- Automatic Companion failover
- SSO on Mac
- Syncing in-progress ASR jobs
- Shared SQLite / rsync
