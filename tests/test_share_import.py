"""Tests for cross-instance share export + import-from-share."""

import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "SQLALCHEMY_DATABASE_URI" not in os.environ:
    _STANDALONE_DIR = tempfile.mkdtemp(prefix="speakr_share_import_")
    os.environ["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{os.path.join(_STANDALONE_DIR, 'test.db')}"
    )
    os.environ.setdefault("UPLOAD_FOLDER", os.path.join(_STANDALONE_DIR, "uploads"))
    os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
    os.environ.setdefault("ENABLE_AUTO_PROCESSING", "false")
    os.environ.setdefault("TEXT_MODEL_API_KEY", "test-key")

from src.app import app, db
from src.models import User, Recording, Share
from src.services.share_import import (
    ShareImportError,
    parse_share_url,
    _payload_from_export_json,
    _payload_from_share_html,
)

app.config["WTF_CSRF_ENABLED"] = False


@contextmanager
def _db():
    with app.app_context():
        yield


def _setup_user(prefix="u"):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
        can_share_publicly=True,
    )
    db.session.add(user)
    db.session.commit()
    return user.id


def _make_recording(user_id, **kwargs):
    defaults = dict(
        user_id=user_id,
        title="Weekly Sync",
        participants="Ada, Bob",
        transcription=json.dumps(
            [{"speaker": "SPEAKER_00", "sentence": "Hello", "start_time": 0, "end_time": 1}]
        ),
        summary="## Summary\nTalked about stuff.",
        notes="Private notes",
        status="COMPLETED",
        audio_path="local://recordings/test/audio.mp3",
        mime_type="audio/mpeg",
        original_filename="weekly.mp3",
        speaker_embeddings={"SPEAKER_00": [0.1, 0.2, 0.3]},
    )
    defaults.update(kwargs)
    rec = Recording(**defaults)
    db.session.add(rec)
    db.session.commit()
    return rec.id


def _login(client, user_id):
    with _db():
        user = db.session.get(User, user_id)
        username = user.username
    # Follow the app's login form if present; otherwise use flask-login test helper
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return username


# --- URL parsing / SSRF guards ------------------------------------------------

def test_parse_share_url_accepts_valid():
    parsed = parse_share_url("https://speakr.example.com/share/AbCdEfGhIjKlMnOp")
    assert parsed.public_id == "AbCdEfGhIjKlMnOp"
    assert parsed.export_url.endswith("/export.json")
    assert parsed.audio_url.endswith("/share/audio/AbCdEfGhIjKlMnOp")


def test_parse_share_url_rejects_non_http():
    try:
        parse_share_url("file:///etc/passwd")
        assert False, "expected ShareImportError"
    except ShareImportError as exc:
        assert "http" in exc.message.lower()


def test_parse_share_url_rejects_non_share_path():
    try:
        parse_share_url("https://speakr.example.com/admin")
        assert False, "expected ShareImportError"
    except ShareImportError as exc:
        assert "share" in exc.message.lower()


def test_parse_share_url_rejects_short_id():
    try:
        parse_share_url("https://speakr.example.com/share/abc")
        assert False, "expected ShareImportError"
    except ShareImportError:
        pass


# --- Payload parsers ----------------------------------------------------------

def test_payload_from_export_json():
    payload = _payload_from_export_json(
        {
            "format": "speakr-share-export",
            "version": 1,
            "public_id": "AbCdEfGhIjKlMnOp",
            "title": "T",
            "transcription": "[{}]",
            "summary": "S",
            "notes": "N",
            "speaker_embeddings": {"SPEAKER_00": [1.0]},
            "audio_available": True,
        },
        "AbCdEfGhIjKlMnOp",
    )
    assert payload.title == "T"
    assert payload.speaker_embeddings == {"SPEAKER_00": [1.0]}


def test_payload_from_share_html_fallback():
    recording = {
        "public_id": "AbCdEfGhIjKlMnOp",
        "title": "From HTML",
        "participants": "A",
        "transcription": '[{"speaker":"SPEAKER_00","sentence":"Hi"}]',
        "summary_raw": "Sum",
        "notes_raw": "Note",
        "meeting_date": "2026-09-01T12:00:00",
        "mime_type": "audio/mpeg",
        "audio_deleted_at": None,
    }
    html = f'<div id="app" data-recording=\'{json.dumps(recording)}\'></div>'
    payload = _payload_from_share_html(html, "AbCdEfGhIjKlMnOp")
    assert payload.title == "From HTML"
    assert payload.summary == "Sum"
    assert payload.notes == "Note"
    assert payload.speaker_embeddings is None


# --- Export endpoint ----------------------------------------------------------

def test_export_json_endpoint_respects_share_flags():
    with _db():
        uid = _setup_user("owner")
        rid = _make_recording(uid)
        share = Share(
            recording_id=rid,
            user_id=uid,
            share_summary=True,
            share_notes=False,
        )
        db.session.add(share)
        db.session.commit()
        public_id = share.public_id

    client = app.test_client()
    resp = client.get(f"/share/{public_id}/export.json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["format"] == "speakr-share-export"
    assert data["summary"] is not None
    assert data["notes"] is None  # share_notes=False
    assert data["speaker_embeddings"] == {"SPEAKER_00": [0.1, 0.2, 0.3]}
    assert data["transcription"]


# --- Import API ---------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", content=b"", headers=None, url="https://remote.test/"):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.content = content
        self.headers = headers or {}
        self.url = url

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_bytes(self, chunk_size=65536):
        data = self.content
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


def test_import_from_share_happy_path():
    audio_bytes = b"ID3fake-audio-content-for-hash"

    with _db():
        uid = _setup_user("importer")

    export_payload = {
        "format": "speakr-share-export",
        "version": 1,
        "public_id": "AbCdEfGhIjKlMnOpQr",
        "title": "Imported Meeting",
        "participants": "X, Y",
        "meeting_date": "2026-09-01T15:30:00",
        "mime_type": "audio/mpeg",
        "original_filename": "meeting.mp3",
        "transcription": json.dumps([{"speaker": "SPEAKER_00", "sentence": "Hi"}]),
        "summary": "A summary",
        "notes": "Some notes",
        "speaker_embeddings": {"SPEAKER_00": [0.5]},
        "audio_available": True,
        "audio_url": "/share/audio/AbCdEfGhIjKlMnOpQr",
    }

    def fake_get(url, **kwargs):
        if url.endswith("export.json"):
            return _FakeResponse(200, json_data=export_payload, url=url)
        if "/share/audio/" in url:
            return _FakeResponse(
                200,
                content=audio_bytes,
                headers={"Content-Length": str(len(audio_bytes))},
                url=url,
            )
        return _FakeResponse(404, url=url)

    fake_client = MagicMock()
    fake_client.get.side_effect = fake_get
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    client = app.test_client()
    _login(client, uid)

    with patch("src.services.share_import.httpx.Client", return_value=fake_client):
        resp = client.post(
            "/api/recordings/import-from-share",
            json={"url": "https://remote.test/share/AbCdEfGhIjKlMnOpQr"},
        )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    assert body["already_imported"] is False
    rec = body["recording"]
    assert rec["title"] == "Imported Meeting"
    assert rec["status"] == "COMPLETED"
    assert rec["summary"] == "A summary"
    assert "SPEAKER_00" in (rec.get("transcription") or "")

    with _db():
        stored = db.session.get(Recording, rec["id"])
        assert stored is not None
        assert stored.processing_source == "share_import"
        assert stored.speaker_embeddings == {"SPEAKER_00": [0.5]}
        assert stored.audio_path


def test_import_rejects_bad_url():
    with _db():
        uid = _setup_user("badurl")
    client = app.test_client()
    _login(client, uid)
    resp = client.post(
        "/api/recordings/import-from-share",
        json={"url": "https://evil.test/not-a-share"},
    )
    assert resp.status_code == 400
    assert "share" in resp.get_json()["error"].lower()


def test_import_requires_login():
    client = app.test_client()
    resp = client.post(
        "/api/recordings/import-from-share",
        json={"url": "https://remote.test/share/AbCdEfGhIjKlMnOpQr"},
    )
    # Flask-Login typically redirects (302) or 401 depending on config
    assert resp.status_code in (302, 401, 403)
