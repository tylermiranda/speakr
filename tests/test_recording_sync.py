"""Tests for file_hash-deduped peer recording sync."""

import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "SQLALCHEMY_DATABASE_URI" not in os.environ:
    _STANDALONE_DIR = tempfile.mkdtemp(prefix="speakr_recording_sync_")
    os.environ["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{os.path.join(_STANDALONE_DIR, 'test.db')}"
    )
    os.environ.setdefault("UPLOAD_FOLDER", os.path.join(_STANDALONE_DIR, "uploads"))
    os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
    os.environ.setdefault("ENABLE_AUTO_PROCESSING", "false")
    os.environ.setdefault("TEXT_MODEL_API_KEY", "test-key")

from src.app import app, db
from src.models import User, Recording
from src.utils.file_hash import compute_file_sha256

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


def _login(client, user_id):
    with _db():
        user = db.session.get(User, user_id)
        username = user.username
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return username


def _write_audio(tmpdir, content=b"fake-audio-bytes-for-sync"):
    path = os.path.join(tmpdir, "clip.wav")
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def test_sync_import_dedups_on_file_hash():
    from src.services.recording_sync import import_completed_recording

    with _db():
        uid = _setup_user("sync")
        user = db.session.get(User, uid)
        with tempfile.TemporaryDirectory() as tmp:
            audio = _write_audio(tmp)
            digest = compute_file_sha256(audio)
            first = import_completed_recording(
                owner=user,
                local_audio_path=audio,
                title="Meeting A",
                transcription=json.dumps(
                    [{"speaker": "SPEAKER_00", "sentence": "hi", "start_time": 0, "end_time": 1}]
                ),
                summary="notes",
                mime_type="audio/wav",
                original_filename="clip.wav",
                file_hash=digest,
                delete_source=False,
            )
            assert first["already_imported"] is False
            audio2 = _write_audio(tmp, b"fake-audio-bytes-for-sync")
            second = import_completed_recording(
                owner=user,
                local_audio_path=audio2,
                title="Meeting A copy",
                transcription=json.dumps(
                    [{"speaker": "SPEAKER_00", "sentence": "hi", "start_time": 0, "end_time": 1}]
                ),
                summary="notes",
                mime_type="audio/wav",
                original_filename="clip.wav",
                file_hash=digest,
                delete_source=False,
            )
            assert second["already_imported"] is True
            assert second["recording"]["id"] == first["recording"]["id"]
            assert Recording.query.filter_by(user_id=uid).count() == 1


def test_sync_api_endpoint_creates_completed():
    client = app.test_client()
    with _db():
        uid = _setup_user("api")
        _login(client, uid)
        with tempfile.TemporaryDirectory() as tmp:
            audio = _write_audio(tmp, b"endpoint-audio-payload")
            with open(audio, "rb") as fh:
                resp = client.post(
                    "/api/v1/recordings/sync",
                    data={
                        "file": (fh, "clip.wav"),
                        "title": "API Sync",
                        "transcription": json.dumps(
                            [
                                {
                                    "speaker": "SPEAKER_00",
                                    "sentence": "hello",
                                    "start_time": 0,
                                    "end_time": 1,
                                }
                            ]
                        ),
                        "summary": "## Sum",
                        "mime_type": "audio/wav",
                    },
                    content_type="multipart/form-data",
                )
            assert resp.status_code == 201, resp.get_data(as_text=True)
            body = resp.get_json()
            assert body["success"] is True
            assert body["already_imported"] is False
            rec = db.session.get(Recording, body["recording"]["id"])
            assert rec.status == "COMPLETED"
            assert rec.processing_source == "peer_sync"
            assert rec.file_hash


def test_manifest_lists_hashed_completed():
    from src.services.recording_sync import import_completed_recording, manifest_rows

    with _db():
        uid = _setup_user("man")
        user = db.session.get(User, uid)
        with tempfile.TemporaryDirectory() as tmp:
            audio = _write_audio(tmp, b"manifest-audio")
            import_completed_recording(
                owner=user,
                local_audio_path=audio,
                title="Manifested",
                transcription='[{"sentence":"x"}]',
                delete_source=False,
            )
        rows = manifest_rows(owner_id=uid)
        assert any(r["title"] == "Manifested" and r["file_hash"] for r in rows)


def test_push_skips_peer_sourced():
    from src.services.recording_sync import import_completed_recording, push_recording_to_peer

    with _db():
        uid = _setup_user("push")
        user = db.session.get(User, uid)
        with tempfile.TemporaryDirectory() as tmp:
            audio = _write_audio(tmp, b"push-skip")
            result = import_completed_recording(
                owner=user,
                local_audio_path=audio,
                title="From peer",
                transcription='[{"sentence":"x"}]',
                delete_source=False,
            )
            rid = result["recording"]["id"]
        with patch.dict(
            os.environ,
            {
                "PEER_SYNC_BASE_URL": "https://example.test",
                "PEER_SYNC_TOKEN": "tok",
            },
        ):
            out = push_recording_to_peer(rid)
        assert out.get("skipped") is True
        assert out.get("reason") == "originated_from_peer"
