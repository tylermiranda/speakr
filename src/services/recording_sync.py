"""Mac-primary / Tower-standby recording package sync.

Identity is ``file_hash`` (SHA-256 of audio). Integer recording ids stay
instance-local. Transport: authenticated HTTPS multipart to
``POST /api/v1/recordings/sync``.

Roles via ``PEER_SYNC_ROLE``:
  - ``primary`` (default when configured): push COMPLETED to peer
  - ``standby``: never auto-push (Mac pulls on recovery)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Optional

import httpx
from flask import current_app
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from src.database import db
from src.models import Recording, SystemSetting
from src.services.share_import import (
    ShareImportError,
    _derive_meeting_end,
    _normalize_transcription,
    _parse_meeting_date,
)
from src.services.storage import get_storage_service
from src.utils.file_hash import compute_file_sha256

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 300.0
PEER_SOURCE = "peer_sync"


class RecordingSyncError(ShareImportError):
    """User-facing sync failure (same shape as ShareImportError)."""


def peer_sync_role() -> str:
    """Return ``primary`` or ``standby``."""
    raw = (os.environ.get("PEER_SYNC_ROLE") or "primary").strip().lower()
    return "standby" if raw in ("standby", "secondary", "fallback") else "primary"


def _parse_embeddings(value: Any) -> Optional[dict]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def find_by_file_hash(*, owner_id: int, file_hash: str) -> Optional[Recording]:
    if not file_hash:
        return None
    return (
        Recording.query.filter_by(user_id=owner_id, file_hash=file_hash)
        .order_by(Recording.created_at.desc())
        .first()
    )


def apply_dates_to_recording(
    recording: Recording,
    *,
    meeting_date: Any = None,
    meeting_end_at: Any = None,
    created_at: Any = None,
    completed_at: Any = None,
    audio_duration_seconds: Any = None,
) -> bool:
    """Overwrite date fields from peer payload. Returns True if anything changed."""
    changed = False
    meeting_dt = _parse_meeting_date(meeting_date)
    if meeting_dt and recording.meeting_date != meeting_dt:
        recording.meeting_date = meeting_dt
        changed = True
    end_dt = _parse_meeting_date(meeting_end_at)
    if end_dt is None and meeting_dt and audio_duration_seconds is not None:
        end_dt = _derive_meeting_end(meeting_dt, None, float(audio_duration_seconds or 0) or None)
    if end_dt and recording.meeting_end_at != end_dt:
        recording.meeting_end_at = end_dt
        changed = True
    created_dt = _parse_meeting_date(created_at)
    if created_dt and recording.created_at != created_dt:
        recording.created_at = created_dt
        changed = True
    completed_dt = _parse_meeting_date(completed_at)
    if completed_dt and recording.completed_at != completed_dt:
        recording.completed_at = completed_dt
        changed = True
    return changed


def import_completed_recording(
    *,
    owner,
    local_audio_path: str,
    title: Optional[str] = None,
    participants: Optional[str] = None,
    notes: Optional[str] = None,
    transcription: Any = None,
    summary: Optional[str] = None,
    meeting_date: Any = None,
    meeting_end_at: Any = None,
    created_at: Any = None,
    completed_at: Any = None,
    mime_type: Optional[str] = None,
    original_filename: Optional[str] = None,
    audio_duration_seconds: Any = None,
    speaker_embeddings: Any = None,
    file_hash: Optional[str] = None,
    tag_names: Optional[list[str]] = None,
    folder_path: Optional[str] = None,
    is_inbox: Optional[bool] = None,
    is_highlighted: Optional[bool] = None,
    sync_updated_at: Any = None,
    delete_source: bool = True,
) -> dict:
    """Create a local COMPLETED recording from audio + metadata (no ASR)."""
    transcription_text = _normalize_transcription(transcription)
    if not transcription_text:
        raise RecordingSyncError("Synced recording requires a transcription.", 400)
    if not local_audio_path or not os.path.isfile(local_audio_path):
        raise RecordingSyncError("Synced audio file is missing.", 400)

    computed_hash = file_hash or compute_file_sha256(local_audio_path)
    existing = find_by_file_hash(owner_id=owner.id, file_hash=computed_hash)
    if existing:
        # Refresh dates / tags on already-imported rows (recovery / backfill).
        changed = apply_dates_to_recording(
            existing,
            meeting_date=meeting_date,
            meeting_end_at=meeting_end_at,
            created_at=created_at,
            completed_at=completed_at,
            audio_duration_seconds=audio_duration_seconds,
        )
        if tag_names is not None or folder_path is not None:
            from src.services.instance_sync import apply_recording_taxonomy

            if apply_recording_taxonomy(
                existing,
                owner=owner,
                tag_names=tag_names,
                folder_path=folder_path,
            ):
                changed = True
        if sync_updated_at:
            sync_dt = _parse_meeting_date(sync_updated_at)
            if sync_dt and (
                getattr(existing, "sync_updated_at", None) is None
                or existing.sync_updated_at < sync_dt
            ):
                existing.sync_updated_at = sync_dt
                changed = True
        if changed:
            db.session.commit()
        if delete_source:
            try:
                os.remove(local_audio_path)
            except OSError:
                pass
        return {
            "success": True,
            "already_imported": True,
            "dates_refreshed": changed,
            "recording": existing.to_dict(include_html=False),
        }

    file_size = os.path.getsize(local_audio_path)
    max_mb = int(SystemSetting.get_setting("max_file_size_mb", 250) or 250)
    if file_size > max_mb * 1024 * 1024:
        raise RecordingSyncError(
            f"Synced audio exceeds max file size ({max_mb} MB).", 413
        )

    now = datetime.utcnow()
    meeting_dt = _parse_meeting_date(meeting_date) or now
    created_dt = _parse_meeting_date(created_at) or now
    completed_dt = _parse_meeting_date(completed_at) or created_dt
    duration = None
    if audio_duration_seconds is not None:
        try:
            duration = float(audio_duration_seconds)
        except (TypeError, ValueError):
            duration = None
    end_dt = _derive_meeting_end(
        meeting_dt, _parse_meeting_date(meeting_end_at), duration
    )
    sync_dt = _parse_meeting_date(sync_updated_at) or completed_dt or now

    safe_name = secure_filename(original_filename or "") or "synced-audio.bin"
    title_text = (title or "").strip() or safe_name

    recording = Recording(
        user_id=owner.id,
        title=title_text[:200],
        participants=participants,
        notes=notes,
        transcription=transcription_text,
        summary=summary,
        status="COMPLETED",
        audio_path=None,
        meeting_date=meeting_dt,
        meeting_end_at=end_dt,
        created_at=created_dt,
        completed_at=completed_dt,
        file_size=file_size,
        original_filename=original_filename or safe_name,
        mime_type=mime_type or "application/octet-stream",
        audio_duration_seconds=duration,
        processing_source=PEER_SOURCE,
        file_hash=computed_hash,
        speaker_embeddings=_parse_embeddings(speaker_embeddings),
        is_inbox=True if is_inbox is None else bool(is_inbox),
        is_highlighted=bool(is_highlighted) if is_highlighted is not None else False,
    )
    if hasattr(Recording, "sync_updated_at"):
        recording.sync_updated_at = sync_dt
    db.session.add(recording)
    db.session.flush()

    storage = get_storage_service()
    storage_key = storage.build_recording_key(
        recording.original_filename, recording.id, now=created_dt
    )
    stored = storage.upload_local_file(
        local_audio_path,
        storage_key,
        content_type=recording.mime_type,
        delete_source=delete_source,
    )
    recording.audio_path = stored.locator

    if tag_names is not None or folder_path is not None:
        from src.services.instance_sync import apply_recording_taxonomy

        apply_recording_taxonomy(
            recording,
            owner=owner,
            tag_names=tag_names,
            folder_path=folder_path,
        )

    db.session.commit()

    current_app.logger.info(
        "Peer-synced recording %s (hash=%s…) for user %s",
        recording.id,
        computed_hash[:12],
        owner.id,
    )

    try:
        from src.services.webhook_dispatch import emit_webhook_event

        emit_webhook_event(
            user_id=owner.id,
            event_type="recording.created",
            data={
                "recording_id": recording.id,
                "title": recording.title,
                "file_size": recording.file_size,
                "original_filename": recording.original_filename,
                "processing_source": PEER_SOURCE,
                "file_hash": computed_hash,
            },
        )
    except Exception as exc:
        current_app.logger.warning(
            "Webhook emit (recording.created) failed for peer sync: %s", exc
        )

    return {
        "success": True,
        "already_imported": False,
        "recording": recording.to_dict(include_html=False),
    }


def manifest_rows(*, owner_id: int, since: Optional[datetime] = None) -> list[dict]:
    """COMPLETED recordings with hashes for peer pull."""
    query = Recording.query.filter(
        Recording.user_id == owner_id,
        Recording.status == "COMPLETED",
        Recording.audio_deleted_at.is_(None),
        Recording.file_hash.isnot(None),
    )
    if since is not None:
        query = query.filter(
            or_(
                Recording.completed_at >= since,
                Recording.created_at >= since,
            )
        )
    rows = (
        query.order_by(Recording.completed_at.desc(), Recording.id.desc())
        .limit(500)
        .all()
    )
    out = []
    for r in rows:
        tag_names = [t.name for t in r.tags] if r.tags else []
        folder_path = r.folder.name if r.folder else None
        out.append(
            {
                "id": r.id,
                "file_hash": r.file_hash,
                "title": r.title,
                "status": r.status,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "meeting_date": r.meeting_date.isoformat() if r.meeting_date else None,
                "meeting_end_at": (
                    r.meeting_end_at.isoformat() if r.meeting_end_at else None
                ),
                "file_size": r.file_size,
                "original_filename": r.original_filename,
                "mime_type": r.mime_type,
                "audio_available": r.audio_deleted_at is None,
                "has_transcription": bool(r.transcription),
                "has_summary": bool(r.summary),
                "processing_source": r.processing_source,
                "tag_names": tag_names,
                "folder_path": folder_path,
                "is_inbox": bool(r.is_inbox),
                "is_highlighted": bool(r.is_highlighted),
                "sync_updated_at": (
                    r.sync_updated_at.isoformat()
                    if getattr(r, "sync_updated_at", None)
                    else (r.completed_at.isoformat() if r.completed_at else None)
                ),
            }
        )
    return out


def peer_sync_configured() -> bool:
    base = (os.environ.get("PEER_SYNC_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("PEER_SYNC_TOKEN") or "").strip()
    return bool(base and token)


def push_recording_to_peer(recording_id: int) -> dict:
    """Push a local COMPLETED recording to PEER_SYNC_BASE_URL."""
    if not peer_sync_configured():
        return {"skipped": True, "reason": "peer_sync_not_configured"}
    if peer_sync_role() == "standby":
        return {"skipped": True, "reason": "standby_role"}

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return {"skipped": True, "reason": "not_found"}
    if recording.status != "COMPLETED":
        return {"skipped": True, "reason": "not_completed"}
    if recording.processing_source == PEER_SOURCE:
        return {"skipped": True, "reason": "originated_from_peer"}
    if not recording.file_hash or not recording.transcription:
        return {"skipped": True, "reason": "missing_hash_or_transcript"}
    if recording.audio_deleted_at is not None or not recording.audio_path:
        return {"skipped": True, "reason": "audio_unavailable"}

    storage = get_storage_service()
    with storage.materialize(recording.audio_path) as materialized:
        return _post_sync_multipart(
            base_url=os.environ["PEER_SYNC_BASE_URL"].strip().rstrip("/"),
            token=os.environ["PEER_SYNC_TOKEN"].strip(),
            audio_path=materialized.local_path,
            recording=recording,
        )


def _recording_tag_names(recording: Recording) -> list[str]:
    return [t.name for t in recording.tags] if recording.tags else []


def _post_sync_multipart(
    *, base_url: str, token: str, audio_path: str, recording: Recording
) -> dict:
    url = f"{base_url}/api/v1/recordings/sync"
    filename = recording.original_filename or "audio.bin"
    mime = recording.mime_type or "application/octet-stream"
    sync_at = getattr(recording, "sync_updated_at", None) or recording.completed_at
    data = {
        "title": recording.title or "",
        "participants": recording.participants or "",
        "notes": recording.notes or "",
        "transcription": recording.transcription or "",
        "summary": recording.summary or "",
        "meeting_date": recording.meeting_date.isoformat() if recording.meeting_date else "",
        "meeting_end_at": (
            recording.meeting_end_at.isoformat() if recording.meeting_end_at else ""
        ),
        "created_at": recording.created_at.isoformat() if recording.created_at else "",
        "completed_at": (
            recording.completed_at.isoformat() if recording.completed_at else ""
        ),
        "mime_type": mime,
        "original_filename": filename,
        "audio_duration_seconds": (
            ""
            if recording.audio_duration_seconds is None
            else str(recording.audio_duration_seconds)
        ),
        "file_hash": recording.file_hash or "",
        "speaker_embeddings": json.dumps(recording.speaker_embeddings or {}),
        "tag_names": json.dumps(_recording_tag_names(recording)),
        "folder_path": recording.folder.name if recording.folder else "",
        "is_inbox": "1" if recording.is_inbox else "0",
        "is_highlighted": "1" if recording.is_highlighted else "0",
        "sync_updated_at": sync_at.isoformat() if sync_at else "",
    }
    headers = {"Authorization": f"Bearer {token}"}
    with open(audio_path, "rb") as fh:
        files = {"file": (filename, fh, mime)}
        try:
            with httpx.Client(
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                follow_redirects=True,
            ) as client:
                resp = client.post(url, headers=headers, data=data, files=files)
        except httpx.HTTPError as exc:
            raise RecordingSyncError(f"Peer sync request failed: {exc}", 502) from exc

    if resp.status_code == 404:
        raise RecordingSyncError(
            "Peer does not expose /api/v1/recordings/sync yet.", 404
        )
    if resp.status_code >= 400:
        detail = (resp.text or "")[:300]
        raise RecordingSyncError(
            f"Peer sync rejected ({resp.status_code}): {detail}",
            resp.status_code if resp.status_code < 600 else 502,
        )
    try:
        return resp.json()
    except ValueError:
        return {"success": True, "raw": resp.text[:200]}


def queue_peer_push_if_configured(recording_id: int) -> None:
    """Best-effort background push after local completion (primary only)."""
    if not peer_sync_configured() or peer_sync_role() == "standby":
        return
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                result = push_recording_to_peer(recording_id)
                app.logger.info(
                    "Peer sync push for recording %s: %s", recording_id, result
                )
            except Exception as exc:
                app.logger.warning(
                    "Peer sync push failed for recording %s: %s", recording_id, exc
                )

    threading.Thread(
        target=_run, name=f"peer-sync-push-{recording_id}", daemon=True
    ).start()
