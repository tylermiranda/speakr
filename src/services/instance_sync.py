"""Taxonomy, settings-bundle, and metadata sync for Mac↔Tower Speakr.

Used by primary continuous replicate (Mac→Tower) and recovery merge (Tower→Mac).
Match tags/folders by name under the owner. Metadata is keyed by ``file_hash``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

import httpx
from flask import current_app

from src.database import db
from src.models import (
    ExportTemplate,
    InitialPromptTemplate,
    NamingTemplate,
    Recording,
    RecordingTag,
    SystemSetting,
    Tag,
)
from src.models.organization import Folder
from src.services.recording_sync import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    apply_dates_to_recording,
    find_by_file_hash,
    peer_sync_configured,
    peer_sync_role,
)
from src.services.share_import import _parse_meeting_date

# SystemSetting keys safe to replicate (no secrets / API keys / OIDC).
SETTINGS_ALLOWLIST = frozenset(
    {
        "max_file_size_mb",
        "default_summary_prompt",
        "admin_transcription_models",
        "enable_auto_processing",
        "video_retention",
        "audio_retention_days",
        "ui_theme",
        "default_language",
        "asr_diarize_default",
    }
)

SETTINGS_DENY_SUBSTRINGS = (
    "secret",
    "api_key",
    "apikey",
    "password",
    "token",
    "oidc",
    "oauth",
    "client_secret",
    "webhook_signing",
)


class InstanceSyncError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _is_denied_setting_key(key: str) -> bool:
    low = (key or "").lower()
    if low in ("secret_key",):
        return True
    return any(s in low for s in SETTINGS_DENY_SUBSTRINGS)


def apply_recording_taxonomy(
    recording: Recording,
    *,
    owner,
    tag_names: Optional[list[str]] = None,
    folder_path: Optional[str] = None,
) -> bool:
    """Resolve tag names / folder path onto a recording. Returns True if changed."""
    changed = False
    if tag_names is not None:
        desired = []
        for raw in tag_names:
            name = (raw or "").strip()
            if not name:
                continue
            tag = Tag.query.filter_by(user_id=owner.id, name=name, group_id=None).first()
            if not tag:
                tag = Tag(user_id=owner.id, name=name[:50])
                db.session.add(tag)
                db.session.flush()
                changed = True
            desired.append(tag)
        current_ids = {a.tag_id for a in recording.tag_associations}
        desired_ids = {t.id for t in desired}
        if current_ids != desired_ids:
            RecordingTag.query.filter_by(recording_id=recording.id).delete()
            for order, tag in enumerate(desired):
                db.session.add(
                    RecordingTag(recording_id=recording.id, tag_id=tag.id, order=order)
                )
            changed = True

    if folder_path is not None:
        path = (folder_path or "").strip()
        if not path:
            if recording.folder_id is not None:
                recording.folder_id = None
                changed = True
        else:
            # Speakr folders are flat (unique name per user); path == name.
            name = path.split("/")[-1][:50]
            folder = Folder.query.filter_by(
                user_id=owner.id, name=name, group_id=None
            ).first()
            if not folder:
                folder = Folder(user_id=owner.id, name=name)
                db.session.add(folder)
                db.session.flush()
                changed = True
            if recording.folder_id != folder.id:
                recording.folder_id = folder.id
                changed = True
    return changed


def export_taxonomy(*, owner_id: int) -> dict:
    tags = (
        Tag.query.filter_by(user_id=owner_id, group_id=None)
        .order_by(Tag.name)
        .all()
    )
    folders = (
        Folder.query.filter_by(user_id=owner_id, group_id=None)
        .order_by(Folder.name)
        .all()
    )
    return {
        "tags": [
            {
                "name": t.name,
                "color": t.color,
                "custom_prompt": t.custom_prompt,
                "default_language": t.default_language,
                "default_hotwords": t.default_hotwords,
                "default_initial_prompt": t.default_initial_prompt,
                "default_transcription_model": t.default_transcription_model,
                "protect_from_deletion": bool(t.protect_from_deletion),
                "retention_days": t.retention_days,
            }
            for t in tags
        ],
        "folders": [
            {
                "name": f.name,
                "parent_path": None,
                "color": f.color,
                "custom_prompt": f.custom_prompt,
                "default_language": f.default_language,
                "default_hotwords": f.default_hotwords,
                "default_initial_prompt": f.default_initial_prompt,
                "default_transcription_model": f.default_transcription_model,
                "protect_from_deletion": bool(f.protect_from_deletion),
                "retention_days": f.retention_days,
            }
            for f in folders
        ],
    }


def import_taxonomy(*, owner, payload: dict) -> dict:
    tags_in = payload.get("tags") or []
    folders_in = payload.get("folders") or []
    tags_upserted = 0
    folders_upserted = 0

    for item in tags_in:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        tag = Tag.query.filter_by(user_id=owner.id, name=name[:50], group_id=None).first()
        if not tag:
            tag = Tag(user_id=owner.id, name=name[:50])
            db.session.add(tag)
            tags_upserted += 1
        if item.get("color"):
            tag.color = item["color"]
        for field in (
            "custom_prompt",
            "default_language",
            "default_hotwords",
            "default_initial_prompt",
            "default_transcription_model",
        ):
            if field in item:
                setattr(tag, field, item.get(field))
        if "protect_from_deletion" in item:
            tag.protect_from_deletion = bool(item["protect_from_deletion"])
        if "retention_days" in item:
            tag.retention_days = item.get("retention_days")
        tag.updated_at = datetime.utcnow()

    for item in folders_in:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        # parent_path ignored for flat Speakr folders; last segment wins.
        if item.get("parent_path"):
            name = str(item["parent_path"]).rstrip("/").split("/")[-1] or name
        name = name[:50]
        folder = Folder.query.filter_by(
            user_id=owner.id, name=name, group_id=None
        ).first()
        if not folder:
            folder = Folder(user_id=owner.id, name=name)
            db.session.add(folder)
            folders_upserted += 1
        if item.get("color"):
            folder.color = item["color"]
        for field in (
            "custom_prompt",
            "default_language",
            "default_hotwords",
            "default_initial_prompt",
            "default_transcription_model",
        ):
            if field in item:
                setattr(folder, field, item.get(field))
        if "protect_from_deletion" in item:
            folder.protect_from_deletion = bool(item["protect_from_deletion"])
        if "retention_days" in item:
            folder.retention_days = item.get("retention_days")
        folder.updated_at = datetime.utcnow()

    db.session.commit()
    return {
        "success": True,
        "tags_upserted": tags_upserted,
        "folders_upserted": folders_upserted,
        "tags_total": len(tags_in),
        "folders_total": len(folders_in),
    }


def export_settings_bundle(*, owner) -> dict:
    settings_out = []
    for row in SystemSetting.query.order_by(SystemSetting.key).all():
        if _is_denied_setting_key(row.key):
            continue
        if SETTINGS_ALLOWLIST and row.key not in SETTINGS_ALLOWLIST:
            # Still export allowlisted-looking non-secret keys that are in allowlist only.
            continue
        settings_out.append(
            {
                "key": row.key,
                "value": row.value,
                "setting_type": row.setting_type,
                "description": row.description,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    def _templates(model):
        rows = model.query.filter_by(user_id=owner.id).order_by(model.name).all()
        return [r.to_dict() for r in rows]

    user_prefs = {
        "transcription_language": getattr(owner, "transcription_language", None),
        "output_language": getattr(owner, "output_language", None),
        "ui_language": getattr(owner, "ui_language", None),
        "summary_prompt": getattr(owner, "summary_prompt", None),
        "transcription_hotwords": getattr(owner, "transcription_hotwords", None),
        "transcription_initial_prompt": getattr(
            owner, "transcription_initial_prompt", None
        ),
        "auto_summarization": getattr(owner, "auto_summarization", None),
        "diarize": getattr(owner, "diarize", None),
        "export_filename_template": getattr(owner, "export_filename_template", None),
    }

    return {
        "system_settings": settings_out,
        "user_prefs": user_prefs,
        "naming_templates": _templates(NamingTemplate),
        "export_templates": _templates(ExportTemplate),
        "initial_prompt_templates": _templates(InitialPromptTemplate),
    }


def import_settings_bundle(*, owner, payload: dict) -> dict:
    applied_settings = 0
    for item in payload.get("system_settings") or []:
        key = (item.get("key") or "").strip()
        if not key or _is_denied_setting_key(key):
            continue
        if key not in SETTINGS_ALLOWLIST:
            continue
        SystemSetting.set_setting(
            key,
            item.get("value"),
            description=item.get("description"),
            setting_type=item.get("setting_type") or "string",
        )
        applied_settings += 1

    prefs = payload.get("user_prefs") or {}
    for field in (
        "transcription_language",
        "output_language",
        "ui_language",
        "summary_prompt",
        "transcription_hotwords",
        "transcription_initial_prompt",
        "export_filename_template",
    ):
        if field in prefs:
            setattr(owner, field, prefs[field])
    if "auto_summarization" in prefs and prefs["auto_summarization"] is not None:
        owner.auto_summarization = bool(prefs["auto_summarization"])
    if "diarize" in prefs and prefs["diarize"] is not None:
        owner.diarize = bool(prefs["diarize"])

    def _upsert_templates(model, items, extra_fields=()):
        count = 0
        for item in items or []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            row = model.query.filter_by(user_id=owner.id, name=name).first()
            if not row:
                row = model(user_id=owner.id, name=name, template=item.get("template") or "")
                db.session.add(row)
                count += 1
            row.template = item.get("template") or row.template or ""
            if "description" in item:
                row.description = item.get("description")
            if "is_default" in item:
                row.is_default = bool(item["is_default"])
            for f in extra_fields:
                if f in item:
                    val = item.get(f)
                    if f == "regex_patterns" and isinstance(val, dict):
                        val = json.dumps(val)
                    setattr(row, f, val)
            row.updated_at = datetime.utcnow()
        return count

    naming = _upsert_templates(
        NamingTemplate,
        payload.get("naming_templates"),
        extra_fields=("regex_patterns",),
    )
    export = _upsert_templates(ExportTemplate, payload.get("export_templates"))
    prompts = _upsert_templates(
        InitialPromptTemplate,
        payload.get("initial_prompt_templates"),
        extra_fields=("hotwords",),
    )
    db.session.commit()
    return {
        "success": True,
        "system_settings_applied": applied_settings,
        "naming_templates_upserted": naming,
        "export_templates_upserted": export,
        "initial_prompt_templates_upserted": prompts,
    }


def apply_metadata_by_hash(*, owner, payload: dict) -> dict:
    """Last-write-wins metadata merge keyed by file_hash."""
    file_hash = (payload.get("file_hash") or "").strip()
    if not file_hash:
        raise InstanceSyncError("file_hash is required", 400)
    recording = find_by_file_hash(owner_id=owner.id, file_hash=file_hash)
    if not recording:
        raise InstanceSyncError("No local recording with that file_hash", 404)

    incoming_sync = _parse_meeting_date(payload.get("sync_updated_at"))
    local_sync = getattr(recording, "sync_updated_at", None)
    if (
        incoming_sync
        and local_sync
        and local_sync > incoming_sync
        and not payload.get("force")
    ):
        return {
            "success": True,
            "skipped": True,
            "reason": "local_newer",
            "recording": recording.to_dict(include_html=False),
        }

    changed = False
    for field in ("title", "participants", "notes", "summary"):
        if field in payload and payload[field] is not None:
            if getattr(recording, field) != payload[field]:
                setattr(recording, field, payload[field])
                changed = True

    if apply_dates_to_recording(
        recording,
        meeting_date=payload.get("meeting_date"),
        meeting_end_at=payload.get("meeting_end_at"),
        created_at=payload.get("created_at"),
        completed_at=payload.get("completed_at"),
        audio_duration_seconds=payload.get("audio_duration_seconds")
        or recording.audio_duration_seconds,
    ):
        changed = True

    if "is_inbox" in payload and payload["is_inbox"] is not None:
        val = bool(payload["is_inbox"])
        if recording.is_inbox != val:
            recording.is_inbox = val
            changed = True
    if "is_highlighted" in payload and payload["is_highlighted"] is not None:
        val = bool(payload["is_highlighted"])
        if recording.is_highlighted != val:
            recording.is_highlighted = val
            changed = True

    if "tag_names" in payload or "folder_path" in payload:
        if apply_recording_taxonomy(
            recording,
            owner=owner,
            tag_names=payload.get("tag_names"),
            folder_path=payload.get("folder_path"),
        ):
            changed = True

    if incoming_sync:
        recording.sync_updated_at = incoming_sync
        changed = True
    elif changed and hasattr(recording, "sync_updated_at"):
        recording.sync_updated_at = datetime.utcnow()

    if changed:
        db.session.commit()

    return {
        "success": True,
        "skipped": False,
        "changed": changed,
        "recording": recording.to_dict(include_html=False),
    }


def metadata_payload_for_recording(recording: Recording) -> dict:
    sync_at = getattr(recording, "sync_updated_at", None) or recording.completed_at
    return {
        "file_hash": recording.file_hash,
        "title": recording.title,
        "participants": recording.participants,
        "notes": recording.notes,
        "summary": recording.summary,
        "meeting_date": recording.meeting_date.isoformat() if recording.meeting_date else None,
        "meeting_end_at": (
            recording.meeting_end_at.isoformat() if recording.meeting_end_at else None
        ),
        "created_at": recording.created_at.isoformat() if recording.created_at else None,
        "completed_at": (
            recording.completed_at.isoformat() if recording.completed_at else None
        ),
        "tag_names": [t.name for t in recording.tags] if recording.tags else [],
        "folder_path": recording.folder.name if recording.folder else None,
        "is_inbox": bool(recording.is_inbox),
        "is_highlighted": bool(recording.is_highlighted),
        "sync_updated_at": sync_at.isoformat() if sync_at else None,
        "audio_duration_seconds": recording.audio_duration_seconds,
    }


def push_metadata_to_peer(recording_id: int) -> dict:
    if not peer_sync_configured() or peer_sync_role() == "standby":
        return {"skipped": True, "reason": "not_primary_or_unconfigured"}
    recording = db.session.get(Recording, recording_id)
    if not recording or not recording.file_hash:
        return {"skipped": True, "reason": "missing"}
    if recording.processing_source == "peer_sync":
        # Still allow metadata refresh to peer for edits after import.
        pass
    url = (
        os.environ["PEER_SYNC_BASE_URL"].strip().rstrip("/")
        + "/api/v1/recordings/sync/metadata"
    )
    headers = {
        "Authorization": f"Bearer {os.environ['PEER_SYNC_TOKEN'].strip()}",
        "Content-Type": "application/json",
    }
    payload = metadata_payload_for_recording(recording)
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0, connect=CONNECT_TIMEOUT),
            follow_redirects=True,
        ) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise InstanceSyncError(f"Peer metadata push failed: {exc}", 502) from exc
    if resp.status_code >= 400:
        raise InstanceSyncError(
            f"Peer metadata rejected ({resp.status_code}): {(resp.text or '')[:300]}",
            resp.status_code if resp.status_code < 600 else 502,
        )
    try:
        return resp.json()
    except ValueError:
        return {"success": True}


def queue_metadata_push_if_configured(recording_id: int) -> None:
    if not peer_sync_configured() or peer_sync_role() == "standby":
        return
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                result = push_metadata_to_peer(recording_id)
                app.logger.info(
                    "Peer metadata push for recording %s: %s", recording_id, result
                )
            except Exception as exc:
                app.logger.warning(
                    "Peer metadata push failed for recording %s: %s",
                    recording_id,
                    exc,
                )

    import threading

    threading.Thread(
        target=_run, name=f"peer-meta-push-{recording_id}", daemon=True
    ).start()


def push_taxonomy_to_peer(*, owner) -> dict:
    if not peer_sync_configured() or peer_sync_role() == "standby":
        return {"skipped": True, "reason": "not_primary_or_unconfigured"}
    url = (
        os.environ["PEER_SYNC_BASE_URL"].strip().rstrip("/")
        + "/api/v1/sync/taxonomy"
    )
    headers = {
        "Authorization": f"Bearer {os.environ['PEER_SYNC_TOKEN'].strip()}",
        "Content-Type": "application/json",
    }
    payload = export_taxonomy(owner_id=owner.id)
    with httpx.Client(
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        follow_redirects=True,
    ) as client:
        resp = client.put(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise InstanceSyncError(
            f"Taxonomy push failed ({resp.status_code}): {(resp.text or '')[:300]}",
            resp.status_code,
        )
    return resp.json()


def push_settings_bundle_to_peer(*, owner) -> dict:
    if not peer_sync_configured() or peer_sync_role() == "standby":
        return {"skipped": True, "reason": "not_primary_or_unconfigured"}
    url = (
        os.environ["PEER_SYNC_BASE_URL"].strip().rstrip("/")
        + "/api/v1/sync/settings-bundle"
    )
    headers = {
        "Authorization": f"Bearer {os.environ['PEER_SYNC_TOKEN'].strip()}",
        "Content-Type": "application/json",
    }
    payload = export_settings_bundle(owner=owner)
    with httpx.Client(
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        follow_redirects=True,
    ) as client:
        resp = client.put(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise InstanceSyncError(
            f"Settings push failed ({resp.status_code}): {(resp.text or '')[:300]}",
            resp.status_code,
        )
    return resp.json()
