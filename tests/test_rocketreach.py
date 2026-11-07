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
    assert result['status'] == 'not_found'


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
