"""Tests for editable meeting_end_at + /save validation."""

import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "SQLALCHEMY_DATABASE_URI" not in os.environ:
    _STANDALONE_DIR = tempfile.mkdtemp(prefix="speakr_meeting_end_")
    os.environ["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{os.path.join(_STANDALONE_DIR, 'test.db')}"
    )
    os.environ.setdefault("UPLOAD_FOLDER", os.path.join(_STANDALONE_DIR, "uploads"))
    os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
    os.environ.setdefault("ENABLE_AUTO_PROCESSING", "false")
    os.environ.setdefault("TEXT_MODEL_API_KEY", "test-key")

from src.app import app, db
from src.models import User, Recording
from src.services.share_import import _derive_meeting_end

app.config["WTF_CSRF_ENABLED"] = False


@contextmanager
def _db():
    with app.app_context():
        yield


def _setup_user():
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"u_{suffix}",
        email=f"u_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def test_derive_meeting_end_from_duration():
    start = datetime(2026, 9, 1, 20, 52, 0)
    end = _derive_meeting_end(start, None, 3600)
    assert end == datetime(2026, 9, 1, 21, 52, 0)


def test_derive_meeting_end_prefers_explicit():
    start = datetime(2026, 9, 1, 20, 52, 0)
    explicit = datetime(2026, 9, 1, 22, 0, 0)
    end = _derive_meeting_end(start, explicit, 3600)
    assert end == explicit


def test_save_meeting_end_at():
    with _db():
        uid = _setup_user()
        rec = Recording(
            user_id=uid,
            title="t",
            status="COMPLETED",
            meeting_date=datetime(2026, 9, 1, 15, 52, 0),
        )
        db.session.add(rec)
        db.session.commit()
        rid = rec.id

    client = app.test_client()
    _login(client, uid)
    resp = client.post(
        "/save",
        json={
            "id": rid,
            "title": "t",
            "meeting_date": "2026-09-01T15:52:00",
            "meeting_end_at": "2026-09-01T16:45:00",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with _db():
        stored = db.session.get(Recording, rid)
        assert stored.meeting_end_at is not None
        assert stored.meeting_end_at.hour == 16
        assert stored.meeting_end_at.minute == 45
        data = stored.to_dict()
        assert data["meeting_end_at"]


def test_save_rejects_end_before_start():
    with _db():
        uid = _setup_user()
        rec = Recording(
            user_id=uid,
            title="t",
            status="COMPLETED",
            meeting_date=datetime(2026, 9, 1, 15, 52, 0),
        )
        db.session.add(rec)
        db.session.commit()
        rid = rec.id

    client = app.test_client()
    _login(client, uid)
    resp = client.post(
        "/save",
        json={
            "id": rid,
            "title": "t",
            "meeting_date": "2026-09-01T15:52:00",
            "meeting_end_at": "2026-09-01T14:00:00",
        },
    )
    assert resp.status_code == 400
    assert "end" in resp.get_json()["error"].lower()
