#!/usr/bin/env python3
"""Mac-primary Speakr peer sync agent (LaunchAgent).

Normal ops (Mac primary): push missing COMPLETED packages + taxonomy +
settings + metadata to Tower. Do not pull unless recovering.

Recovery (``--mode recovery`` / ``--direction pull``): merge Tower → Mac
(packages, taxonomy, settings, metadata), then resume primary push.

Also supports ``--backfill-dates`` to refresh local created_at/completed_at/
meeting_date from Tower by file_hash (fixes peer_sync rows stamped at import).

Config: ~/Library/Application Support/speakr/config/peer-sync.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

try:
    import httpx
except ImportError:
    print("httpx is required: python3 -m pip install --user httpx", file=sys.stderr)
    sys.exit(1)

LOG = logging.getLogger("speakr-peer-sync")
DEFAULT_CONFIG = Path.home() / "Library/Application Support/speakr/config/peer-sync.json"
STATE_PATH = Path.home() / "Library/Application Support/speakr/config/peer-sync-state.json"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as fh:
        return json.load(fh)


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class SpeakrClient:
    def __init__(self, base_url: str, token: str, name: str = "peer"):
        self.base = base_url.rstrip("/")
        self.token = token
        self.name = name
        self._client = httpx.Client(
            timeout=httpx.Timeout(300.0, connect=20.0),
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _url(self, path: str) -> str:
        return urljoin(self.base + "/", path.lstrip("/"))

    def get_json(self, path: str, **params) -> Any:
        r = self._client.get(self._url(path), params=params)
        r.raise_for_status()
        return r.json()

    def put_json(self, path: str, payload: dict) -> Any:
        r = self._client.put(self._url(path), json=payload)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, payload: dict) -> Any:
        r = self._client.post(self._url(path), json=payload)
        r.raise_for_status()
        return r.json()

    def manifest(self, since: Optional[str] = None) -> list[dict]:
        params = {}
        if since:
            params["since"] = since
        r = self._client.get(self._url("/api/v1/recordings/sync/manifest"), params=params)
        if r.status_code == 404:
            return self._manifest_via_list()
        r.raise_for_status()
        return list(r.json().get("recordings") or [])

    def _manifest_via_list(self) -> list[dict]:
        page = 1
        rows: list[dict] = []
        while page <= 50:
            data = self.get_json(
                "/api/v1/recordings",
                status="completed",
                per_page=100,
                page=page,
            )
            batch = data.get("recordings") or []
            for r in batch:
                rows.append(
                    {
                        "id": r["id"],
                        "file_hash": r.get("file_hash"),
                        "title": r.get("title"),
                        "status": r.get("status"),
                        "completed_at": r.get("completed_at"),
                        "created_at": r.get("created_at"),
                        "meeting_date": r.get("meeting_date"),
                        "file_size": r.get("file_size"),
                        "original_filename": r.get("original_filename"),
                        "mime_type": r.get("mime_type"),
                        "audio_available": r.get("audio_available", True),
                        "processing_source": r.get("processing_source"),
                    }
                )
            pag = data.get("pagination") or {}
            if not pag.get("has_next"):
                break
            page += 1
        return rows

    def recording_detail(self, recording_id: int) -> dict:
        return self.get_json(
            f"/api/v1/recordings/{recording_id}",
            include="transcription,summary,notes",
            raw="1",
        )

    def download_audio(self, recording_id: int, dest: str) -> None:
        with self._client.stream(
            "GET", self._url(f"/api/v1/recordings/{recording_id}/audio")
        ) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)

    def post_sync(self, audio_path: str, meta: dict) -> dict:
        filename = meta.get("original_filename") or "audio.bin"
        mime = meta.get("mime_type") or "application/octet-stream"
        tag_names = meta.get("tag_names")
        if tag_names is None and meta.get("tags"):
            tag_names = [
                t.get("name") if isinstance(t, dict) else str(t)
                for t in (meta.get("tags") or [])
            ]
        folder_path = meta.get("folder_path")
        if not folder_path and isinstance(meta.get("folder"), dict):
            folder_path = meta["folder"].get("name")
        data = {
            "title": meta.get("title") or "",
            "participants": meta.get("participants") or "",
            "notes": meta.get("notes") or "",
            "transcription": meta.get("transcription") or "",
            "summary": meta.get("summary") or "",
            "meeting_date": meta.get("meeting_date") or "",
            "meeting_end_at": meta.get("meeting_end_at") or "",
            "created_at": meta.get("created_at") or "",
            "completed_at": meta.get("completed_at") or "",
            "mime_type": mime,
            "original_filename": filename,
            "audio_duration_seconds": (
                ""
                if meta.get("audio_duration") is None
                and meta.get("audio_duration_seconds") is None
                else str(
                    meta.get("audio_duration_seconds", meta.get("audio_duration"))
                )
            ),
            "file_hash": meta.get("file_hash") or "",
            "speaker_embeddings": json.dumps(meta.get("speaker_embeddings") or {}),
            "tag_names": json.dumps(tag_names or []),
            "folder_path": folder_path or "",
            "is_inbox": "1" if meta.get("is_inbox", True) else "0",
            "is_highlighted": "1" if meta.get("is_highlighted") else "0",
            "sync_updated_at": meta.get("sync_updated_at")
            or meta.get("completed_at")
            or "",
        }
        with open(audio_path, "rb") as fh:
            files = {"file": (filename, fh, mime)}
            r = self._client.post(
                self._url("/api/v1/recordings/sync"), data=data, files=files
            )
        if r.status_code == 404:
            raise RuntimeError("peer_missing_sync_endpoint")
        if r.status_code >= 400:
            raise RuntimeError(f"sync_failed {r.status_code}: {r.text[:300]}")
        return r.json()

    def get_taxonomy(self) -> dict:
        return self.get_json("/api/v1/sync/taxonomy")

    def put_taxonomy(self, payload: dict) -> dict:
        return self.put_json("/api/v1/sync/taxonomy", payload)

    def get_settings_bundle(self) -> dict:
        return self.get_json("/api/v1/sync/settings-bundle")

    def put_settings_bundle(self, payload: dict) -> dict:
        return self.put_json("/api/v1/sync/settings-bundle", payload)

    def post_metadata(self, payload: dict) -> dict:
        return self.post_json("/api/v1/recordings/sync/metadata", payload)


def _hashes(manifest: list[dict]) -> dict[str, dict]:
    out = {}
    for row in manifest:
        h = row.get("file_hash")
        if h:
            out[h] = row
    return out


def _meta_from_detail(detail: dict, digest: str) -> dict:
    tag_names = [
        t.get("name") if isinstance(t, dict) else str(t)
        for t in (detail.get("tags") or [])
    ]
    folder = detail.get("folder") or {}
    return {
        **detail,
        "file_hash": digest,
        "audio_duration_seconds": detail.get("audio_duration"),
        "tag_names": tag_names,
        "folder_path": folder.get("name") if isinstance(folder, dict) else None,
        "sync_updated_at": detail.get("sync_updated_at") or detail.get("completed_at"),
    }


def pull_missing(
    local: SpeakrClient, peer: SpeakrClient, dry_run: bool, limit: Optional[int] = None
) -> int:
    peer_rows = peer.manifest()
    peer_rows = sorted(
        peer_rows,
        key=lambda r: r.get("completed_at") or r.get("created_at") or "",
        reverse=True,
    )
    local_hashes = set(_hashes(local.manifest()))
    imported = 0
    for row in peer_rows:
        if limit is not None and imported >= limit:
            break
        digest = row.get("file_hash")
        rid = row.get("id")
        if not rid or not row.get("audio_available", True):
            continue
        if digest and digest in local_hashes:
            continue
        LOG.info("Pull %s id=%s hash=%s…", peer.name, rid, (digest or "?")[:12])
        if dry_run:
            imported += 1
            continue
        detail = peer.recording_detail(rid)
        if not detail.get("transcription"):
            LOG.warning("Skip id=%s: no transcription", rid)
            continue
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = os.path.join(tmp, "audio.bin")
            peer.download_audio(rid, audio_path)
            if not digest:
                digest = _sha256_file(audio_path)
                if digest in local_hashes:
                    LOG.info("Skip id=%s: hash already local after download", rid)
                    continue
            result = local.post_sync(audio_path, _meta_from_detail(detail, digest))
            LOG.info(
                "Imported → local already=%s id=%s",
                result.get("already_imported"),
                (result.get("recording") or {}).get("id"),
            )
            local_hashes.add(digest)
            imported += 1
    return imported


def push_missing(
    local: SpeakrClient, peer: SpeakrClient, dry_run: bool, limit: Optional[int] = None
) -> int:
    local_rows = local.manifest()
    peer_hashes = set(_hashes(peer.manifest()))
    pushed = 0
    for row in local_rows:
        if limit is not None and pushed >= limit:
            break
        digest = row.get("file_hash")
        rid = row.get("id")
        if not digest or not rid:
            continue
        if digest in peer_hashes:
            continue
        if row.get("processing_source") == "peer_sync":
            continue
        LOG.info("Push %s id=%s hash=%s…", local.name, rid, digest[:12])
        if dry_run:
            pushed += 1
            continue
        detail = local.recording_detail(rid)
        if not detail.get("transcription"):
            LOG.warning("Skip push id=%s: no transcription", rid)
            continue
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = os.path.join(tmp, "audio.bin")
            local.download_audio(rid, audio_path)
            try:
                result = peer.post_sync(audio_path, _meta_from_detail(detail, digest))
            except RuntimeError as exc:
                if "peer_missing_sync_endpoint" in str(exc):
                    LOG.error(
                        "Peer %s missing /api/v1/recordings/sync — "
                        "deploy the Speakr fork image, then re-run.",
                        peer.name,
                    )
                    return pushed
                raise
            LOG.info(
                "Pushed → peer already=%s id=%s",
                result.get("already_imported"),
                (result.get("recording") or {}).get("id"),
            )
            peer_hashes.add(digest)
            pushed += 1
    return pushed


def sync_taxonomy(src: SpeakrClient, dst: SpeakrClient, dry_run: bool) -> None:
    LOG.info("Taxonomy %s → %s", src.name, dst.name)
    if dry_run:
        return
    try:
        payload = src.get_taxonomy()
        result = dst.put_taxonomy(payload)
        LOG.info("Taxonomy applied: %s", result)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        if code == 404:
            LOG.warning(
                "Peer %s missing /api/v1/sync/taxonomy — deploy fork image, then re-run",
                dst.name,
            )
            return
        raise


def sync_settings(src: SpeakrClient, dst: SpeakrClient, dry_run: bool) -> None:
    LOG.info("Settings-bundle %s → %s", src.name, dst.name)
    if dry_run:
        return
    try:
        payload = src.get_settings_bundle()
        result = dst.put_settings_bundle(payload)
        LOG.info("Settings applied: %s", result)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        if code == 404:
            LOG.warning(
                "Peer %s missing /api/v1/sync/settings-bundle — deploy fork image, then re-run",
                dst.name,
            )
            return
        raise


def push_metadata_for_shared(
    local: SpeakrClient, peer: SpeakrClient, dry_run: bool, limit: Optional[int] = None
) -> int:
    """Push metadata for hashes present on both sides (edits / recovery LWW)."""
    local_map = _hashes(local.manifest())
    peer_map = _hashes(peer.manifest())
    shared = [h for h in local_map if h in peer_map]
    count = 0
    for digest in shared:
        if limit is not None and count >= limit:
            break
        rid = local_map[digest].get("id")
        if not rid:
            continue
        LOG.info("Metadata push hash=%s…", digest[:12])
        if dry_run:
            count += 1
            continue
        detail = local.recording_detail(rid)
        payload = {
            "file_hash": digest,
            "title": detail.get("title"),
            "participants": detail.get("participants"),
            "notes": detail.get("notes"),
            "summary": detail.get("summary"),
            "meeting_date": detail.get("meeting_date"),
            "meeting_end_at": detail.get("meeting_end_at"),
            "created_at": detail.get("created_at"),
            "completed_at": detail.get("completed_at"),
            "tag_names": [
                t.get("name") if isinstance(t, dict) else str(t)
                for t in (detail.get("tags") or [])
            ],
            "folder_path": (detail.get("folder") or {}).get("name")
            if isinstance(detail.get("folder"), dict)
            else None,
            "is_inbox": detail.get("is_inbox"),
            "is_highlighted": detail.get("is_highlighted"),
            "sync_updated_at": detail.get("sync_updated_at")
            or detail.get("completed_at"),
            "audio_duration_seconds": detail.get("audio_duration"),
        }
        try:
            peer.post_metadata(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                LOG.warning("Peer missing metadata endpoint; skip")
                return count
            raise
        count += 1
    return count


def pull_metadata_for_shared(
    local: SpeakrClient, peer: SpeakrClient, dry_run: bool, limit: Optional[int] = None
) -> int:
    """Recovery: apply Tower metadata onto Mac for shared hashes (Tower wins when newer)."""
    local_map = _hashes(local.manifest())
    peer_map = _hashes(peer.manifest())
    shared = [h for h in peer_map if h in local_map]
    count = 0
    for digest in shared:
        if limit is not None and count >= limit:
            break
        rid = peer_map[digest].get("id")
        if not rid:
            continue
        LOG.info("Metadata pull hash=%s…", digest[:12])
        if dry_run:
            count += 1
            continue
        detail = peer.recording_detail(rid)
        payload = {
            "file_hash": digest,
            "title": detail.get("title"),
            "participants": detail.get("participants"),
            "notes": detail.get("notes"),
            "summary": detail.get("summary"),
            "meeting_date": detail.get("meeting_date"),
            "meeting_end_at": detail.get("meeting_end_at"),
            "created_at": detail.get("created_at"),
            "completed_at": detail.get("completed_at"),
            "tag_names": [
                t.get("name") if isinstance(t, dict) else str(t)
                for t in (detail.get("tags") or [])
            ],
            "folder_path": (detail.get("folder") or {}).get("name")
            if isinstance(detail.get("folder"), dict)
            else None,
            "is_inbox": detail.get("is_inbox"),
            "is_highlighted": detail.get("is_highlighted"),
            "sync_updated_at": detail.get("sync_updated_at")
            or detail.get("completed_at"),
            "audio_duration_seconds": detail.get("audio_duration"),
            "force": True,
        }
        local.post_metadata(payload)
        count += 1
    return count


def backfill_dates(
    local: SpeakrClient, peer: SpeakrClient, dry_run: bool, limit: Optional[int] = None
) -> int:
    """Set Mac dates from Tower for hash-matched rows (esp. peer_sync imports)."""
    local_map = _hashes(local.manifest())
    peer_map = _hashes(peer.manifest())
    updated = 0
    for digest, local_row in local_map.items():
        if digest not in peer_map:
            continue
        if limit is not None and updated >= limit:
            break
        peer_id = peer_map[digest].get("id")
        if not peer_id:
            continue
        LOG.info(
            "Backfill dates hash=%s… (local source=%s)",
            digest[:12],
            local_row.get("processing_source"),
        )
        if dry_run:
            updated += 1
            continue
        detail = peer.recording_detail(peer_id)
        payload = {
            "file_hash": digest,
            "meeting_date": detail.get("meeting_date"),
            "meeting_end_at": detail.get("meeting_end_at"),
            "created_at": detail.get("created_at"),
            "completed_at": detail.get("completed_at"),
            "sync_updated_at": detail.get("completed_at"),
            "force": True,
        }
        result = local.post_metadata(payload)
        LOG.info("Backfill result changed=%s", result.get("changed"))
        updated += 1
    return updated


def run(
    config: dict,
    *,
    dry_run: bool = False,
    direction: str = "push",
    mode: str = "normal",
    limit: Optional[int] = None,
    backfill_dates_only: bool = False,
    include_catalog: bool = True,
) -> int:
    local = SpeakrClient(config["local_base_url"], config["local_token"], name="mac")
    peer = SpeakrClient(config["peer_base_url"], config["peer_token"], name="tower")
    try:
        if backfill_dates_only:
            n = backfill_dates(local, peer, dry_run=dry_run, limit=limit)
            LOG.info("Backfill-dates done count=%s", n)
            _save_json(
                STATE_PATH,
                {
                    "last_run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "mode": "backfill-dates",
                    "backfilled": n,
                    "dry_run": dry_run,
                },
            )
            return 0

        if mode == "recovery":
            direction = "pull"

        pulled = pushed = meta = 0
        if direction in ("both", "pull"):
            pulled = pull_missing(local, peer, dry_run=dry_run, limit=limit)
            if include_catalog:
                sync_taxonomy(peer, local, dry_run=dry_run)
                sync_settings(peer, local, dry_run=dry_run)
            meta = pull_metadata_for_shared(
                local, peer, dry_run=dry_run, limit=limit
            )
        if direction in ("both", "push"):
            pushed = push_missing(local, peer, dry_run=dry_run, limit=limit)
            if include_catalog:
                sync_taxonomy(local, peer, dry_run=dry_run)
                sync_settings(local, peer, dry_run=dry_run)
            meta = push_metadata_for_shared(
                local, peer, dry_run=dry_run, limit=limit
            )

        LOG.info(
            "Done mode=%s pull=%s push=%s meta=%s dry_run=%s",
            mode,
            pulled,
            pushed,
            meta,
            dry_run,
        )
        _save_json(
            STATE_PATH,
            {
                "last_run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "mode": mode,
                "direction": direction,
                "pulled": pulled,
                "pushed": pushed,
                "metadata": meta,
                "dry_run": dry_run,
            },
        )
        return 0
    finally:
        local.close()
        peer.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--direction",
        choices=("both", "push", "pull"),
        default=None,
        help="Default: push (primary). Use pull for recovery merge.",
    )
    parser.add_argument(
        "--mode",
        choices=("normal", "recovery"),
        default="normal",
        help="recovery forces Tower→Mac pull of packages+catalog+metadata",
    )
    parser.add_argument(
        "--backfill-dates",
        action="store_true",
        help="One-shot: refresh Mac dates from Tower by file_hash",
    )
    parser.add_argument(
        "--no-catalog",
        action="store_true",
        help="Skip taxonomy/settings bundle sync",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max recordings to pull/push this run",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.config.exists():
        LOG.error("Missing config %s", args.config)
        return 2
    config = _load_json(args.config, {})
    required = ("local_base_url", "local_token", "peer_base_url", "peer_token")
    missing = [k for k in required if not config.get(k)]
    if missing:
        LOG.error("Config missing keys: %s", ", ".join(missing))
        return 2

    direction = args.direction
    if direction is None:
        direction = config.get("direction") or "push"

    limit = args.limit
    if limit is None and not args.backfill_dates and config.get("limit_per_run") is not None:
        limit = int(config["limit_per_run"])

    return run(
        config,
        dry_run=args.dry_run,
        direction=direction,
        mode=args.mode,
        limit=limit,
        backfill_dates_only=args.backfill_dates,
        include_catalog=not args.no_catalog,
    )


if __name__ == "__main__":
    raise SystemExit(main())
