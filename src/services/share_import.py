"""Import a completed recording from another Speakr instance's public share URL."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from flask import current_app
from werkzeug.utils import secure_filename

from src.database import db
from src.models import Recording, SystemSetting
from src.services.storage import get_storage_service
from src.utils.file_hash import compute_file_sha256

SHARE_PATH_RE = re.compile(r"^/share/(?P<public_id>[A-Za-z0-9_-]{8,128})/?$")
EXPORT_PATH_RE = re.compile(
    r"^/share/(?P<public_id>[A-Za-z0-9_-]{8,128})/export\.json/?$"
)
DATA_RECORDING_RE = re.compile(
    r'data-recording=(["\'])(?P<json>.*?)\1',
    re.DOTALL,
)

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 120.0


class ShareImportError(Exception):
    """User-facing import failure with an HTTP-ish status code."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class ParsedShareUrl:
    public_id: str
    origin: str  # scheme://netloc
    share_url: str
    export_url: str
    audio_url: str


@dataclass
class SharePayload:
    public_id: str
    title: Optional[str]
    participants: Optional[str]
    meeting_date: Optional[datetime]
    meeting_end_at: Optional[datetime]
    mime_type: Optional[str]
    original_filename: Optional[str]
    transcription: Optional[str]
    summary: Optional[str]
    notes: Optional[str]
    speaker_embeddings: Optional[dict]
    audio_available: bool
    audio_duration_seconds: Optional[float]


def _derive_meeting_end(
    meeting_date: Optional[datetime],
    meeting_end_at: Optional[datetime],
    audio_duration_seconds: Optional[float],
) -> Optional[datetime]:
    if meeting_end_at:
        return meeting_end_at
    if meeting_date and audio_duration_seconds:
        try:
            return meeting_date + timedelta(seconds=float(audio_duration_seconds))
        except (TypeError, ValueError):
            return None
    return None


def parse_share_url(raw_url: str) -> ParsedShareUrl:
    if not raw_url or not isinstance(raw_url, str):
        raise ShareImportError("A share URL is required.")

    raw_url = raw_url.strip()
    try:
        parsed = urlparse(raw_url)
    except Exception as exc:
        raise ShareImportError("Invalid share URL.") from exc

    if parsed.scheme not in ("http", "https"):
        raise ShareImportError("Share URL must use http or https.")
    if not parsed.netloc:
        raise ShareImportError("Share URL is missing a host.")

    path = parsed.path or ""
    match = SHARE_PATH_RE.match(path)
    if not match:
        # Allow paste of export.json URL as well
        match = EXPORT_PATH_RE.match(path)
    if not match:
        raise ShareImportError(
            "URL must be a Speakr public share link (/share/<id>)."
        )

    public_id = match.group("public_id")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return ParsedShareUrl(
        public_id=public_id,
        origin=origin,
        share_url=f"{origin}/share/{public_id}",
        export_url=f"{origin}/share/{public_id}/export.json",
        audio_url=f"{origin}/share/audio/{public_id}",
    )


def _parse_meeting_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # Support trailing Z
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def _normalize_transcription(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _payload_from_export_json(data: dict, public_id: str) -> SharePayload:
    if not isinstance(data, dict):
        raise ShareImportError("Share export payload was not a JSON object.", 502)

    fmt = data.get("format")
    if fmt and fmt != "speakr-share-export":
        raise ShareImportError("Unrecognized share export format.", 502)

    transcription = _normalize_transcription(data.get("transcription"))
    if not transcription:
        raise ShareImportError("Shared recording has no transcription to import.")

    audio_available = bool(data.get("audio_available", True))
    embeddings = data.get("speaker_embeddings")
    if embeddings is not None and not isinstance(embeddings, dict):
        embeddings = None

    return SharePayload(
        public_id=data.get("public_id") or public_id,
        title=data.get("title"),
        participants=data.get("participants"),
        meeting_date=_parse_meeting_date(data.get("meeting_date")),
        meeting_end_at=_parse_meeting_date(data.get("meeting_end_at")),
        mime_type=data.get("mime_type"),
        original_filename=data.get("original_filename"),
        transcription=transcription,
        summary=data.get("summary"),
        notes=data.get("notes"),
        speaker_embeddings=embeddings,
        audio_available=audio_available,
        audio_duration_seconds=(
            float(data["audio_duration_seconds"])
            if data.get("audio_duration_seconds") is not None
            else None
        ),
    )


def _payload_from_share_html(html: str, public_id: str) -> SharePayload:
    match = DATA_RECORDING_RE.search(html)
    if not match:
        raise ShareImportError(
            "Could not read shared recording metadata from the share page.",
            502,
        )
    raw_json = match.group("json")
    # HTML attribute may contain HTML entities for quotes in some setups;
    # Flask's tojson typically emits a JSON string without extra escaping
    # beyond what's needed for the attribute delimiter.
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        # Common case: attribute used single quotes and JSON has &quot;
        unescaped = (
            raw_json.replace("&quot;", '"')
            .replace("&#34;", '"')
            .replace("&amp;", "&")
            .replace("&#39;", "'")
            .replace("&apos;", "'")
        )
        try:
            data = json.loads(unescaped)
        except json.JSONDecodeError as exc:
            raise ShareImportError(
                "Could not parse shared recording metadata.", 502
            ) from exc

    if not isinstance(data, dict):
        raise ShareImportError("Share page metadata was invalid.", 502)

    transcription = _normalize_transcription(data.get("transcription"))
    if not transcription:
        raise ShareImportError("Shared recording has no transcription to import.")

    summary = data.get("summary_raw")
    if summary is None and isinstance(data.get("summary"), str):
        # Prefer raw markdown; HTML-only is still better than nothing
        summary = None
    notes = data.get("notes_raw")

    audio_deleted = data.get("audio_deleted_at")
    audio_available = not bool(audio_deleted)

    return SharePayload(
        public_id=data.get("public_id") or public_id,
        title=data.get("title"),
        participants=data.get("participants"),
        meeting_date=_parse_meeting_date(data.get("meeting_date")),
        meeting_end_at=None,
        mime_type=data.get("mime_type"),
        original_filename=None,
        transcription=transcription,
        summary=summary,
        notes=notes,
        speaker_embeddings=None,
        audio_available=audio_available,
        audio_duration_seconds=(
            float(data["audio_duration"])
            if data.get("audio_duration") is not None
            else None
        ),
    )


def fetch_share_payload(parsed: ParsedShareUrl) -> SharePayload:
    timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as client:
            export_resp = client.get(parsed.export_url)
            if export_resp.status_code == 200:
                try:
                    data = export_resp.json()
                except ValueError as exc:
                    raise ShareImportError(
                        "Share export returned invalid JSON.", 502
                    ) from exc
                return _payload_from_export_json(data, parsed.public_id)

            if export_resp.status_code not in (404, 405):
                raise ShareImportError(
                    f"Could not fetch share export (HTTP {export_resp.status_code}).",
                    502,
                )

            # Fallback: stock Speakr HTML share page
            html_resp = client.get(parsed.share_url)
            if html_resp.status_code == 404:
                raise ShareImportError("Share link not found.", 404)
            if html_resp.status_code != 200:
                raise ShareImportError(
                    f"Could not fetch share page (HTTP {html_resp.status_code}).",
                    502,
                )
            return _payload_from_share_html(html_resp.text, parsed.public_id)
    except ShareImportError:
        raise
    except httpx.TimeoutException as exc:
        raise ShareImportError(
            "Timed out contacting the remote Speakr instance.", 504
        ) from exc
    except httpx.RequestError as exc:
        raise ShareImportError(
            "Could not reach the remote Speakr instance.", 502
        ) from exc


def _extension_for_mime(mime_type: Optional[str]) -> str:
    if not mime_type:
        return ".bin"
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mp4": ".m4a",
        "audio/m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/flac": ".flac",
        "audio/webm": ".webm",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }
    return mapping.get(mime_type.lower(), ".bin")


def download_share_audio(
    parsed: ParsedShareUrl,
    *,
    mime_type: Optional[str],
    original_filename: Optional[str],
    max_bytes: int,
) -> tuple[str, int, str]:
    """Download audio to a staging tempfile.

    Returns (local_path, size_bytes, safe_filename).
    """
    timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)
    safe_name = secure_filename(original_filename or "") or (
        f"shared-{parsed.public_id}{_extension_for_mime(mime_type)}"
    )
    target_url = parsed.audio_url

    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as client:
            final_resp = None
            for _ in range(4):
                resp = client.get(target_url)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise ShareImportError("Audio redirect missing Location.", 502)
                    target_url = urljoin(str(resp.url), location)
                    continue
                final_resp = resp
                break

            if final_resp is None:
                raise ShareImportError("Too many redirects fetching shared audio.", 502)

            if final_resp.status_code == 404:
                raise ShareImportError("Shared audio was not found.", 404)
            if final_resp.status_code != 200:
                raise ShareImportError(
                    f"Could not download shared audio (HTTP {final_resp.status_code}).",
                    502,
                )

            content_length = final_resp.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise ShareImportError(
                            "Shared audio exceeds the maximum upload size.",
                            413,
                        )
                except ValueError:
                    pass

            staging_dir = get_storage_service().get_staging_dir()
            fd, local_path = tempfile.mkstemp(
                prefix="share_import_",
                suffix=os.path.splitext(safe_name)[1] or ".bin",
                dir=staging_dir,
            )
            os.close(fd)

            written = 0
            try:
                with open(local_path, "wb") as out:
                    for chunk in final_resp.iter_bytes(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            raise ShareImportError(
                                "Shared audio exceeds the maximum upload size.",
                                413,
                            )
                        out.write(chunk)
            except Exception:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                raise

            if written == 0:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                raise ShareImportError("Shared audio download was empty.", 502)

            return local_path, written, safe_name
    except ShareImportError:
        raise
    except httpx.TimeoutException as exc:
        raise ShareImportError(
            "Timed out downloading shared audio.", 504
        ) from exc
    except httpx.RequestError as exc:
        raise ShareImportError(
            "Could not download shared audio from the remote instance.", 502
        ) from exc


def import_recording_from_share_url(*, owner, share_url: str) -> dict:
    """Fetch a remote public share and create a local COMPLETED recording.

    Returns a dict suitable for JSON response (includes recording.to_dict()).
    """
    parsed = parse_share_url(share_url)
    payload = fetch_share_payload(parsed)

    if not payload.audio_available:
        raise ShareImportError(
            "This share no longer has audio available to import.", 409
        )

    max_mb = int(SystemSetting.get_setting("max_file_size_mb", 250) or 250)
    max_bytes = max_mb * 1024 * 1024

    local_path, file_size, safe_name = download_share_audio(
        parsed,
        mime_type=payload.mime_type,
        original_filename=payload.original_filename,
        max_bytes=max_bytes,
    )

    try:
        file_hash = compute_file_sha256(local_path)
        existing = Recording.query.filter_by(
            user_id=owner.id,
            file_hash=file_hash,
        ).order_by(Recording.created_at.desc()).first()
        if existing:
            try:
                os.remove(local_path)
            except OSError:
                pass
            return {
                "success": True,
                "already_imported": True,
                "recording": existing.to_dict(),
            }

        now = datetime.utcnow()
        title = (payload.title or "").strip() or f"Imported share {parsed.public_id}"
        meeting_date = payload.meeting_date or now
        meeting_end_at = _derive_meeting_end(
            meeting_date,
            payload.meeting_end_at,
            payload.audio_duration_seconds,
        )
        recording = Recording(
            user_id=owner.id,
            title=title[:200],
            participants=payload.participants,
            notes=payload.notes,
            transcription=payload.transcription,
            summary=payload.summary,
            status="COMPLETED",
            audio_path=None,
            meeting_date=meeting_date,
            meeting_end_at=meeting_end_at,
            file_size=file_size,
            original_filename=payload.original_filename or safe_name,
            mime_type=payload.mime_type,
            audio_duration_seconds=payload.audio_duration_seconds,
            completed_at=now,
            processing_source="share_import",
            file_hash=file_hash,
            speaker_embeddings=payload.speaker_embeddings,
            is_inbox=True,
        )
        db.session.add(recording)
        db.session.flush()

        storage = get_storage_service()
        storage_key = storage.build_recording_key(
            recording.original_filename, recording.id, now=now
        )
        stored = storage.upload_local_file(
            local_path,
            storage_key,
            content_type=payload.mime_type,
            delete_source=True,
        )
        recording.audio_path = stored.locator
        db.session.commit()

        current_app.logger.info(
            "Imported share %s as recording %s for user %s",
            parsed.public_id,
            recording.id,
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
                    "processing_source": "share_import",
                },
            )
        except Exception as exc:
            current_app.logger.warning(
                "Webhook emit (recording.created) failed for share import: %s", exc
            )

        return {
            "success": True,
            "already_imported": False,
            "recording": recording.to_dict(),
        }
    except Exception:
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except OSError:
            pass
        raise
