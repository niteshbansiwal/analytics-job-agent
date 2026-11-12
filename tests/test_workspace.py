import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import event
from app.db import Job, Contact, Outreach, Event, KV, session
from app.read_model import outreach_page, job_logs, timeline, revision


def seed(db, count=1):
    jobs = []
    for i in range(count):
        job = Job(company=f'Company {i}', title='Analytics Manager', source='Excel', post_url=f'https://example.com/jobs/{i}')
        db.add(job)
        db.flush()
        jobs.append(job)
    return jobs


def test_outreach_page_bounded_without_body_or_pdf():
    with session() as db:
        job = seed(db)[0]
        for i in range(31):
            db.add(Outreach(job_id=job.id, body='private email', resume_b64='large PDF', status='scheduled'))
        db.flush()
        queries = []
        def record(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        event.listen(db.bind, 'before_cursor_execute', record)
        try:
            result = outreach_page(db, page=2, size=25)
        finally:
            event.remove(db.bind, 'before_cursor_execute', record)
        assert result['total'] == 31
        assert len(result['items']) == 6
        assert 'body' not in result['items'][0] and 'resume_b64' not in result['items'][0]
        fetch = next(q for q in queries if ' LIMIT ' in q)
        assert 'outreach.body' not in fetch and 'outreach.resume_b64' not in fetch


def test_log_first_milestones_missing_history_and_batch_filter():
    with session() as db:
        job, missing = seed(db, 2)
        at = datetime(2026, 11, 11, tzinfo=timezone.utc)
        for i, action in enumerate(['job_uploaded', 'contact_email_available', 'reviewed_gmail_draft_verified', 'email_sent', 'email_sent', 'reply_received']):
            db.add(Event(job_id=job.id, action=action, created_at=at + timedelta(minutes=i)))
        # A profile-only update must not claim successful email enrichment.
        db.add(Event(job_id=missing.id, action='spreadsheet_contact_enriched', created_at=at + timedelta(minutes=7)))
        db.add(KV(key='import_batch:test', value=json.dumps({'job_ids':[job.id]})))
        db.flush()
        result = job_logs(db, batch='test')['items']
        assert len(result) == 1
        assert result[0]['first_sent_at'].startswith('2026-11-11T00:03')
        assert result[0]['drafted_at'].startswith('2026-11-11T00:02')
        unknown = next(x for x in job_logs(db)['items'] if x['id'] == missing.id)
        assert unknown['uploaded_at'] is None and unknown['enriched_at'] is None
        assert job_logs(db, batch='missing')['total'] == 0
        assert job_logs(db, event_type='first_sent_at')['total'] == 1
        assert job_logs(db, start=at + timedelta(days=1))['total'] == 0


def test_timeline_contact_receipt_detection_and_safe_details():
    with session() as db:
        job = seed(db)[0]
        contact = Contact(job_id=job.id, name='Hiring contact')
        db.add(contact); db.flush()
        row = Outreach(job_id=job.id, contact_id=contact.id)
        db.add(row); db.flush()
        before = revision(db)
        db.add(Event(job_id=job.id, outreach_id=row.id, action='reply_received', details=json.dumps({
            'received_at':'2026-11-11T01:00:00+00:00', 'detected_at':'2026-11-11T01:15:00+00:00',
            'body':'private', 'token':'secret'})))
        db.flush()
        assert before != revision(db)
        item = timeline(db, job.id)['items'][0]
        assert item['contact_name'] == 'Hiring contact'
        assert item['occurred_at'] == '2026-11-11T01:00:00+00:00'
        assert item['details']['detected_at'] == '2026-11-11T01:15:00+00:00'
        assert 'body' not in item['details'] and 'token' not in item['details']


def test_workspace_endpoints_require_owner_and_validate_dates(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, owner
    client = TestClient(app)
    monkeypatch.setenv('DEMO_MODE', 'false')
    assert client.get('/api/workspace/logs').status_code == 401
    app.dependency_overrides[owner] = lambda: 'owner@example.com'
    try:
        assert client.get('/api/workspace/logs?start=not-a-date').status_code == 422
        assert client.get('/api/workspace/jobs/missing/timeline').status_code == 404
        assert client.get('/api/workspace/outreach/missing').status_code == 404
        assert client.get('/api/workspace/changes').headers['cache-control'] == 'private, no-store'
    finally:
        app.dependency_overrides.clear()


def test_compact_overview_and_ist_date_filter(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, owner
    monkeypatch.setenv('DEMO_MODE', 'false')
    with session() as db:
        jobs = seed(db, 41)
        selected = jobs[0].id
        db.add(Event(job_id=selected, action='job_uploaded',
                     created_at=datetime(2026, 11, 10, 19, tzinfo=timezone.utc)))
    app.dependency_overrides[owner] = lambda: 'owner@example.com'
    try:
        client = TestClient(app)
        data = client.get('/api/overview?section=jobs&page=2').json()
        assert len(data['jobs']) == 20 and data['jobs_page']['total'] == 41
        compact = client.get('/api/overview?section=outbox').json()
        assert compact['jobs'] == [] and compact['outreach'] == []
        assert compact['totals']['jobs'] == 41
        assert client.get('/api/workspace/logs?start=2026-11-11&end=2026-11-11').json()['total'] == 1
        assert client.get('/api/workspace/logs?start=2026-11-10&end=2026-11-10').json()['total'] == 0
        assert client.get('/api/workspace/logs?job_id='+selected).json()['total'] == 1
    finally:
        app.dependency_overrides.clear()


def test_vercel_html_routes_use_static_cdn(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv('VERCEL', '1')
    client = TestClient(app)
    for path, target in [('/', '/welcome.html'), ('/app', '/index.html')]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == target
