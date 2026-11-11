"""Offline retry tests; no Gemini or Gmail network calls occur."""
from datetime import timedelta
from app import auto_pipeline
from app.db import session, Job, Contact, Outreach, Integration, get_kv, set_kv, now
from app.gemini import GeminiError


def test_transient_error_retries_on_next_trigger_and_stops_after_three(monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'false')
    monkeypatch.setattr(auto_pipeline, 'send_telegram', lambda *a, **k: None)
    monkeypatch.setattr(auto_pipeline, 'get_resume',
                        lambda db, name: ({'filename': 'resume.pdf', 'id': name}, b'%PDF-fake'))
    calls = []
    def transient(*args, **kwargs):
        calls.append(1)
        raise GeminiError('Gemini service temporarily unavailable (HTTP 503)', retryable=True)
    monkeypatch.setattr(auto_pipeline, 'build_email_preview', transient)
    with session() as db:
        job = Job(company='IndiGo', title='Manager - Analytics',
                  source='unit_test', post_url='https://example.org/job', jd='Valid job')
        db.add(job); db.flush()
        person = Contact(job_id=job.id, name='Jochen Hoesch', email='jochen@example.com')
        db.add(person); db.flush()
        outreach = Outreach(job_id=job.id, contact_id=person.id, status='pending_contact')
        db.add(outreach)
        db.add(Integration(provider='gmail', owner_email='owner@example.com'))
        db.flush()
        set_kv(db, auto_pipeline.KEY, 'true')
        oid = outreach.id
    for attempt in (1, 2, 3):
        with session() as db:
            if attempt > 1:
                set_kv(db, auto_pipeline.GEMINI_RETRY_AFTER_PREFIX + oid,
                       (now() - timedelta(minutes=1)).isoformat())
            result = auto_pipeline.process_pending(db, limit=1)
            row = db.get(Outreach, oid)
            assert result['processed'] == 1
            assert get_kv(db, auto_pipeline.GEMINI_RETRY_PREFIX + oid) == str(attempt)
            assert row.status == ('pending_contact' if attempt < 3 else 'needs_attention')
            assert row.gmail_draft_id == '' and row.sent_at is None
            if attempt < 3:
                assert 'next scheduler trigger' in row.error
            else:
                assert 'max 3 attempts' in row.error
        if attempt == 1:
            with session() as db:
                assert auto_pipeline.process_pending(db, limit=1)['processed'] == 0
    assert len(calls) == 3
    with session() as db:
        assert auto_pipeline.process_pending(db, limit=1)['processed'] == 0


def test_existing_503_error_is_recovered_but_gmail_draft_is_not(monkeypatch):
    monkeypatch.setenv('DEMO_MODE', 'false')
    monkeypatch.setattr(auto_pipeline, 'send_telegram', lambda *a, **k: None)
    with session() as db:
        job = Job(company='Tredence', title='Senior Data Science Manager',
                  source='unit_test', post_url='https://example.org/job', jd='Valid job')
        db.add(job); db.flush()
        c1 = Contact(job_id=job.id, name='Nishant Saxena', email='nishant@example.com')
        c2 = Contact(job_id=job.id, name='Manoj Jayaraman', email='manoj@example.com')
        db.add_all([c1,c2]);db.flush()
        error='Draft/schedule error: GeminiError: Gemini service temporarily unavailable (HTTP 503)'
        failed=Outreach(job_id=job.id, contact_id=c1.id, status='needs_attention', error=error)
        drafted=Outreach(job_id=job.id, contact_id=c2.id, status='needs_attention',
                         error=error, gmail_draft_id='existing-gmail-draft')
        db.add_all([failed,drafted]); db.flush()
        recovered=auto_pipeline._recover_transient_gemini_errors(db)
        assert recovered == 1
        assert failed.status == 'pending_contact'
        assert drafted.status == 'needs_attention'
        assert get_kv(db, auto_pipeline.GEMINI_RETRY_PREFIX + failed.id) == '1'
