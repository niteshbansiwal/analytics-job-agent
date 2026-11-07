"""Safe offline tests: must never send real emails or lookup real people."""
import os
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

os.environ['DEMO_MODE'] = 'true'
os.environ['DATABASE_URL'] = f"sqlite:///{Path(tempfile.gettempdir()) / 'atlas_job_agent_pytest.db'}"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.calendar_rules import next_fixed_send, followup_due, daily_search_due
from app.config import settings
from app.main import app
from app.db import session, Job, Outreach
from app.integrations import build_mime, inspect_gmail_draft
from app.resume import tailored_pdf

client = TestClient(app)
UTC = timezone.utc
IST = ZoneInfo('Asia/Kolkata')


def test_health_and_demo_overview():
    health = client.get('/api/health')
    assert health.status_code == 200 and health.json()['demo'] is True
    overview = client.get('/api/overview')
    assert overview.status_code == 200
    data = overview.json()
    assert all(j['is_demo'] for j in data['jobs'])
    assert all('DEMO' in j['company'] for j in data['jobs'])
    assert data['integrations']['gmail'] is False


def test_approval_creates_resume_and_demo_draft_without_real_send():
    job = client.get('/api/overview').json()['jobs'][0]
    approved = client.post(f"/api/jobs/{job['id']}/approve")
    assert approved.status_code == 200, approved.text
    row = approved.json()
    assert row['status'] == 'pending_contact'  # No fabricated email contact
    assert row['gmail_draft_id'] == ''
    assert row['resume_name'].endswith('.pdf')
    resume = client.get(f"/api/outreach/{row['id']}/resume.pdf")
    assert resume.status_code == 200 and resume.content.startswith(b'%PDF')
    assert client.post('/api/discover').status_code == 409
    assert client.get('/api/cron/tick').status_code == 401


def test_calendar_weekends_and_48hr_rule():
    # Fri 14:30 IST -> Mon 10:30 IST (earliest next fixed slot)
    fri = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    mon = next_fixed_send(fri).astimezone(IST)
    assert mon.strftime('%Y-%m-%d %H:%M') == '2026-11-09 10:30'
    # Thu 10:30 IST +48h => Saturday, reschedule to Monday 10:30 IST
    thurs = datetime(2026, 11, 5, 10, 30, tzinfo=IST)
    due = followup_due(thurs).astimezone(IST)
    assert due.strftime('%Y-%m-%d %H:%M') == '2026-11-09 10:30'
    assert daily_search_due(datetime(2026, 11, 9, 3, 31, tzinfo=UTC), '')
    assert not daily_search_due(datetime(2026, 11, 8, 4, 0, tzinfo=UTC), '')


def test_draft_mime_roundtrip_preserves_attachment_and_user_editable_fields():
    import base64
    raw = build_mime('recruiter@example.com', 'sender@example.com', 'Role title', 'My notes', b'%PDF-test', 'cv.pdf')
    draft = {'message': {'raw': raw}}
    info = inspect_gmail_draft(draft)
    assert info['to'] == 'recruiter@example.com'
    assert info['subject'] == 'Role title'
    assert info['body'].strip() == 'My notes'
    assert info['attachment_count'] == 1


def test_tailored_pdf_builds_from_verified_facts():
    data = tailored_pdf('Hiring analytics manager, Tableau SQL credit risk forecasting', 'Risk Analytics Manager')
    assert data.startswith(b'%PDF') and len(data) > 1000


def test_invalid_import_rejected():
    out = client.post('/api/jobs/import', json={'company':'Fake Corp','title':'Analytics Manager','post_url':'http://example.com'})
    assert out.status_code == 422
