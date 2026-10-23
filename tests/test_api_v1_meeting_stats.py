"""Meeting time on GET /api/v1/stats comes from the recording library."""

import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User

app.config["WTF_CSRF_ENABLED"] = False


def _user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user, title, filename, seconds, meeting_date):
    rec = Recording(
        user_id=user.id,
        title=title,
        original_filename=filename,
        status="COMPLETED",
        audio_duration_seconds=seconds,
        meeting_date=meeting_date,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_stats_meeting_window_and_all_time():
    with app.app_context():
        owner = _user("stats_owner")
        other = _user("stats_other")
        now = datetime.utcnow()
        recent_room = _recording(
            owner, "Standup", "2026-10-20 In-Person.m4a", 100, now - timedelta(days=1)
        )
        recent_call = _recording(
            owner, "Zoom", "2026-10-20 Zoom.m4a", 200, now - timedelta(days=2)
        )
        older = _recording(
            owner, "Old call", "2026-08-01 Zoom.m4a", 100, now - timedelta(days=40)
        )
        stranger = _recording(
            other, "Other library", "2020-01-01 Zoom.m4a", 10000, datetime(2020, 1, 1, 12, 0, 0)
        )
        client = app.test_client()
        _login(client, owner)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200, resp.data
        meetings = resp.get_json()["meetings"]
        assert meetings["window_days"] == 30
        assert meetings["recent"] == {
            "count": 2,
            "minutes": 5,
            "remote_count": 1,
            "remote_minutes": 3,
            "in_person_count": 1,
            "in_person_minutes": 2,
        }
        assert meetings["all_time"]["count"] == 3
        assert meetings["all_time"]["minutes"] == 7
        assert meetings["all_time"]["remote_count"] == 2
        assert meetings["all_time"]["remote_minutes"] == 5
        assert meetings["all_time"]["in_person_count"] == 1
        assert meetings["all_time"]["in_person_minutes"] == 2
        assert meetings["earliest_meeting_date"] == older.meeting_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        for rec in (recent_room, recent_call, older, stranger):
            db.session.delete(rec)
        db.session.delete(owner)
        db.session.delete(other)
        db.session.commit()
