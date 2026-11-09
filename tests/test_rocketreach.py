"""Offline RocketReach tests: no API credits, no Gmail messages."""
import os
os.environ['DEMO_MODE'] = 'true'

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base, Contact, Job
from app.integrations import _rocketreach_email_result
from app.main import _rocketreach_state, _save_rocketreach_result


LINKEDIN = 'https://www.linkedin.com/in/jane-analyst'


def test_complete_professional_email_is_extracted():
    result = _rocketreach_email_result({
        'id': 123, 'status': 'complete', 'linkedin_url': LINKEDIN,
        'emails': [
            {'email': 'jane@gmail.com', 'type': 'personal'},
            {'email': 'jane@company.com', 'type': 'professional', 'smtp_valid': 'valid'},
        ],
    }, LINKEDIN)
    assert result['status'] == 'returned'
    assert result['email'] == 'jane@company.com'
    assert result['email_status'] == 'rocketreach_smtp_valid'


def test_pending_does_not_expose_cached_email():
    result = _rocketreach_email_result({
        'id': 456, 'status': 'progress', 'linkedin_url': LINKEDIN,
        'current_work_email': 'jane@company.com',
    }, LINKEDIN)
    assert result['status'] == 'pending'
    assert result['provider_id'] == '456'
    assert 'email' not in result


def test_personal_email_never_selected():
    result = _rocketreach_email_result({
        'id': 1, 'status': 'complete',
        'emails': [{'email': 'jane@gmail.com', 'type': 'personal'}],
    }, LINKEDIN)
    assert result['status'] == 'awaiting_email'  # Work email may arrive on later status check
    assert 'email' not in result


def test_linkedin_identity_mismatch_blocks_email():
    result = _rocketreach_email_result({
        'id': 1, 'status': 'complete',
        'linkedin_url': 'https://www.linkedin.com/in/different-person',
        'current_work_email': 'other@company.com',
    }, LINKEDIN)
    assert result['status'] == 'identity_mismatch'
    assert 'email' not in result


def test_check_status_array_result():
    result = _rocketreach_email_result([{
        'id': 789, 'status': 'complete', 'linkedin_url': LINKEDIN,
        'current_work_email': 'jane@company.com',
    }], LINKEDIN)
    assert result['status'] == 'returned'
    assert result['provider_id'] == '789'



def test_complete_without_email_is_provisional_when_provider_id_exists():
    result = _rocketreach_email_result({
        'id': 456, 'status': 'complete', 'linkedin_url': LINKEDIN, 'emails': [],
    }, LINKEDIN)
    assert result['status'] == 'awaiting_email'
    assert result['provider_id'] == '456'


def test_completed_nested_profile_email_and_check_status_envelope():
    result = _rocketreach_email_result({
        'status': 'success',
        'people': [{'id': 456, 'status': 'complete', 'linkedin_url': LINKEDIN,
                    'email': {'address': 'jane@company.com', 'type': 'work'}}],
    }, LINKEDIN)
    assert result['status'] == 'returned'
    assert result['email'] == 'jane@company.com'


def test_second_click_reuses_provider_id_and_check_finds_email(monkeypatch):
    import json
    from contextlib import contextmanager
    from app import main
    from app.db import set_kv
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = Job(company='Example Payments', title='Manager Analytics',
                  source='test', post_url='https://example.com/jobs/1')
        db.add(job)
        db.flush()
        contact = Contact(job_id=job.id, name='Jane Analyst', linkedin_url=LINKEDIN)
        db.add(contact)
        db.flush()

        @contextmanager
        def db_session():
            yield db

        counts = {'lookup': 0, 'check': 0}

        def lookup(url):
            counts['lookup'] += 1
            return {'status': 'awaiting_email', 'provider_id': '456'}

        def check(pid, url):
            counts['check'] += 1
            assert pid == '456'
            return {'status': 'returned', 'provider_id': '456',
                    'email': 'jane@company.com',
                    'email_status': 'rocketreach_unverified'}

        monkeypatch.setattr(main, 'session', db_session)
        monkeypatch.setattr(main, 'demo_mode', lambda: False)
        monkeypatch.setattr(main, 'env', lambda key: 'test-key')
        monkeypatch.setattr(main, 'rocketreach_lookup', lookup)
        monkeypatch.setattr(main, 'rocketreach_check_status', check)
        first = main.lookup_contact_email(contact.id, main.RocketReachLookupInput(), user='owner')
        assert first['status'] == 'awaiting_email'
        again = main.lookup_contact_email(contact.id, main.RocketReachLookupInput(), user='owner')
        assert again['status'] == 'awaiting_email'
        assert counts['lookup'] == 1
        # Simulate the backoff time passing, without sleeping or real API calls.
        state = main._rocketreach_state(db, contact.id)
        state['next_check_at'] = '2020-01-01T00:00:00+00:00'
        set_kv(db, 'rocketreach:' + contact.id, json.dumps(state))
        completed = main.check_contact_lookup(contact.id, user='owner')
        assert completed['status'] == 'returned'
        assert completed['email'] == 'jane@company.com'
        assert counts == {'lookup': 1, 'check': 1}
        assert contact.email == 'jane@company.com'
    engine.dispose()


def test_three_empty_status_checks_finalize_without_paid_retry():
    from app.main import _save_rocketreach_result, _rocketreach_state
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = Job(company='Example Payments', title='Manager Analytics',
                  source='test', post_url='https://example.com/jobs/1')
        db.add(job)
        db.flush()
        contact = Contact(job_id=job.id, name='Jane Analyst', linkedin_url=LINKEDIN)
        db.add(contact)
        db.flush()
        first = _save_rocketreach_result(db, contact, {'status': 'awaiting_email', 'provider_id': '456'})
        assert first['status'] == 'awaiting_email'
        for n in range(1, 4):
            result = _save_rocketreach_result(db, contact,
                {'status': 'awaiting_email', 'provider_id': '456'}, is_check=True)
            assert result['status'] == ('not_found' if n == 3 else 'awaiting_email')
        assert _rocketreach_state(db, contact.id)['lookup_count'] == 1
        assert _rocketreach_state(db, contact.id)['check_count'] == 3
    engine.dispose()

def test_lookup_state_persists_and_contact_email_only_on_completion():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = Job(company='Example Payments', title='Manager Analytics',
                  source='test', post_url='https://example.com/jobs/1')
        db.add(job)
        db.flush()
        contact = Contact(job_id=job.id, name='Jane Analyst', linkedin_url=LINKEDIN)
        db.add(contact)
        db.flush()
        pending = _save_rocketreach_result(db, contact, {
            'status': 'pending', 'provider_id': '789'})
        assert pending['status'] == 'pending'
        assert contact.email == ''
        assert _rocketreach_state(db, contact.id)['provider_id'] == '789'
        complete = _save_rocketreach_result(db, contact, {
            'status': 'returned', 'provider_id': '789',
            'email': 'jane@company.com', 'email_status': 'rocketreach_unverified'})
        assert complete['email'] == 'jane@company.com'
        assert contact.email_status == 'rocketreach_unverified'
        assert _rocketreach_state(db, contact.id)['status'] == 'returned'
    engine.dispose()


def test_auto_enrichment_reuses_manual_provider_id(monkeypatch):
    """Automatic flow must recheck, not charge for a duplicate person lookup."""
    import json
    from app import auto_pipeline
    from app.db import Outreach, set_kv
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = Job(company='Example Payments', title='Manager Analytics',
                  source='test', post_url='https://example.com/jobs/1')
        db.add(job)
        db.flush()
        contact = Contact(job_id=job.id, name='Jane Analyst', linkedin_url=LINKEDIN)
        db.add(contact)
        db.flush()
        row = Outreach(job_id=job.id, contact_id=contact.id, status='pending_contact')
        db.add(row)
        db.flush()
        set_kv(db, 'atlas_auto_import_enabled', 'true')
        set_kv(db, 'rocketreach:' + contact.id, json.dumps({
            'status': 'awaiting_email', 'provider_id': '456',
            'lookup_count': 1, 'check_count': 0,
            'next_check_at': '2020-01-01T00:00:00+00:00',
        }))
        calls = {'lookup': 0, 'check': 0}
        def lookup(url):
            calls['lookup'] += 1
            raise AssertionError('Must not start another paid lookup')
        def check(pid, url):
            calls['check'] += 1
            assert pid == '456'
            return {'status': 'returned', 'provider_id': '456',
                    'email': 'jane@company.com',
                    'email_status': 'rocketreach_work_email'}
        monkeypatch.setattr(auto_pipeline, 'demo_mode', lambda: False)
        monkeypatch.setattr(auto_pipeline, 'env', lambda key: 'test-key')
        monkeypatch.setattr(auto_pipeline, 'rocketreach_lookup', lookup)
        monkeypatch.setattr(auto_pipeline, 'rocketreach_check_status', check)
        result = auto_pipeline.process_pending(db, limit=1)
        assert result['processed'] == 1
        assert calls == {'lookup': 0, 'check': 1}
        assert contact.email == 'jane@company.com'
        assert _rocketreach_state(db, contact.id)['status'] == 'returned'
    engine.dispose()


def test_auto_enrichment_waits_for_existing_lookup_without_recharging(monkeypatch):
    import json
    from app import auto_pipeline
    from app.db import Outreach, set_kv
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = Job(company='Example Payments', title='Manager Analytics',
                  source='test', post_url='https://example.com/jobs/1')
        db.add(job)
        db.flush()
        contact = Contact(job_id=job.id, name='Jane Analyst', linkedin_url=LINKEDIN)
        db.add(contact)
        db.flush()
        db.add(Outreach(job_id=job.id, contact_id=contact.id, status='pending_contact'))
        db.flush()
        set_kv(db, 'atlas_auto_import_enabled', 'true')
        set_kv(db, 'rocketreach:' + contact.id, json.dumps({
            'status': 'pending', 'provider_id': '456',
            'lookup_count': 1, 'check_count': 0,
            'next_check_at': '2099-01-01T00:00:00+00:00',
        }))
        monkeypatch.setattr(auto_pipeline, 'demo_mode', lambda: False)
        monkeypatch.setattr(auto_pipeline, 'env', lambda key: 'test-key')
        monkeypatch.setattr(auto_pipeline, 'rocketreach_lookup',
                            lambda *args: (_ for _ in ()).throw(AssertionError('lookup')))
        monkeypatch.setattr(auto_pipeline, 'rocketreach_check_status',
                            lambda *args: (_ for _ in ()).throw(AssertionError('check')))
        result = auto_pipeline.process_pending(db, limit=1)
        assert result['processed'] == 1
        assert result['scheduled'] == 0
        assert _rocketreach_state(db, contact.id)['provider_id'] == '456'
    engine.dispose()


def test_rocketreach_completed_alias_and_missing_status():
    assert _rocketreach_email_result({'status': 'completed', 'id': 123, 'work_email': 'jane@company.com'}, LINKEDIN)['status'] == 'returned'
    assert _rocketreach_email_result({'id': 123, 'work_email': 'jane@company.com'}, LINKEDIN)['status'] == 'returned'
