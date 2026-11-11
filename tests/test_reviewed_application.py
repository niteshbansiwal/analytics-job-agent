"""Offline review workflow tests. No real Gemini, Gmail, or RocketReach calls."""
import importlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

main = importlib.import_module('app.main')
from app import integrations
from app.db import Job, Contact, Integration, Outreach, session
from app.resume import resume_facts, resume_selection, tailored_pdf
from app import resume_master
from app.resume_library import save_resume, digest
from io import BytesIO
from reportlab.pdfgen import canvas


def test_master_resume_source_and_selectable_pdf():
    info = resume_facts()
    assert info['experience'][0]['title'] == 'Assistant Manager - Finance & Banking Analytics'
    assert '8+ years' in info['summary']
    assert '900+' in info['experience'][0]['bullets'][0]
    selected = resume_selection('SQL Tableau dashboard analytics', 'Decision Analytics Manager',
                                ['dashboards', 'amex_sme'])
    assert selected['all_claims_from_master_resume'] is True
    assert len(selected['positions']) == len(info['experience'])
    chosen = {str(p['index']): [b['index'] for b in p['bullets'] if b['selected']]
              for p in selected['positions']}
    for idx, p in enumerate(selected['positions']):
        assert chosen[str(idx)]
        for bullet in p['bullets']:
            assert bullet['text'] == info['experience'][idx]['bullets'][bullet['index']]
    pdf = tailored_pdf('SQL Tableau', 'Decision Analytics Manager', selected_bullets=chosen)
    assert pdf.startswith(b'%PDF') and len(pdf) > 1000
    with pytest.raises(ValueError):
        tailored_pdf('SQL', selected_bullets={'0': [999]})
    with pytest.raises(ValueError):
        tailored_pdf('SQL', selected_bullets={str(i): [] for i in range(4)})


def test_reviewed_draft_requires_confirmation_and_verifies_pdf_without_sending(monkeypatch):
    monkeypatch.setattr(main, 'demo_mode', lambda: False)
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792))
    document.drawString(40, 720, 'Offline original PDF fixture')
    document.save()
    original_pdf = output.getvalue()
    with session() as db:
        save_resume(db, 'analytics', original_pdf)
    main.app.dependency_overrides[main.owner] = lambda: 'offline-owner'
    job_id, contact_id = str(uuid4()), str(uuid4())
    with session() as db:
        db.add(Job(id=job_id, company='Test Employer', title='Decision Analytics Manager',
                   source='Offline test', post_url='https://example.com/jobs/test-role',
                   jd='SQL Tableau dashboards and analytics delivery', is_demo=0))
        db.add(Contact(id=contact_id, job_id=job_id, name='Test Recruiter',
                       email='recruiter@example.com', linkedin_url='',
                       email_status='user_supplied'))
        connection = db.get(Integration, 'gmail')
        if connection:
            connection.owner_email = 'owner@example.com'
        else:
            db.add(Integration(provider='gmail', owner_email='owner@example.com',
                               refresh_ciphertext='offline-only'))
    client = TestClient(main.app)
    calls = {'create': 0, 'update': 0, 'send': 0}
    draft_raw = {}
    def fake_create(db, to, subject, body, pdf, filename):
        calls['create'] += 1
        assert pdf == original_pdf
        draft_raw['raw'] = integrations.build_mime(to, 'owner@example.com', subject,
                                                    body, pdf, filename)
        return {'id': 'offline-draft-1'}
    def fake_update(db, draft_id, to, subject, body, pdf, filename):
        calls['update'] += 1
        assert draft_id == 'offline-draft-1'
        draft_raw['raw'] = integrations.build_mime(to, 'owner@example.com', subject,
                                                    body, pdf, filename)
        return {'id': draft_id}
    def fake_get(db, draft_id):
        return {'message': {'raw': draft_raw['raw']}}
    def fail_send(*args, **kwargs):
        calls['send'] += 1
        raise AssertionError('Gmail send must never be called by review workflow')
    monkeypatch.setattr(integrations, 'gmail_create_draft', fake_create)
    monkeypatch.setattr(integrations, 'gmail_update_draft', fake_update)
    monkeypatch.setattr(integrations, 'gmail_get_draft', fake_get)
    monkeypatch.setattr(integrations, 'gmail_send_existing_draft', fail_send)
    chosen = {str(i): list(range(count)) for i, count in enumerate([7, 7, 3, 1])}
    payload = {
        'contact_id': contact_id, 'recipient_email': 'recruiter@example.com',
        'subject': 'Decision Analytics Manager', 'body': 'Hi Test,\n\nI would like to discuss the analytics manager opening. https://example.com/jobs/test-role',
        'resume_id': 'analytics', 'resume_sha256': digest(original_pdf), 'reviewed': True,
    }
    try:
        preview = client.post('/api/jobs/'+job_id+'/reviewed-resume.pdf',
                              json={'resume_id': 'analytics', 'resume_sha256': digest(original_pdf)})
        assert preview.status_code == 200, preview.text
        assert preview.headers['content-type'].startswith('application/pdf')
        assert preview.content == original_pdf
        unreviewed = client.post('/api/jobs/'+job_id+'/reviewed-draft',
                                 json={**payload, 'reviewed': False})
        assert unreviewed.status_code == 422
        assert calls['create'] == 0
        invalid = client.post('/api/jobs/'+job_id+'/reviewed-draft',
                              json={**payload, 'resume_id': 'invented'})
        assert invalid.status_code == 422
        assert calls['create'] == 0
        stale = client.post('/api/jobs/'+job_id+'/reviewed-draft',
                            json={**payload, 'resume_sha256': '0'*64})
        assert stale.status_code == 409
        assert calls['create'] == 0
        response = client.post('/api/jobs/'+job_id+'/reviewed-draft', json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['verified'] is True
        assert data['sent'] is False and data['scheduled'] is False
        assert data['outreach']['status'] == 'draft_ready'
        assert data['outreach']['due_at'] is None
        assert calls == {'create': 1, 'update': 0, 'send': 0}
        repeated = client.post('/api/jobs/'+job_id+'/reviewed-draft', json=payload)
        assert repeated.status_code == 409
        assert calls['create'] == 1
        updated = client.post('/api/jobs/'+job_id+'/reviewed-draft',
                              json={**payload, 'replace_existing_draft': True})
        assert updated.status_code == 200, updated.text
        assert calls == {'create': 1, 'update': 1, 'send': 0}
        with session() as db:
            row = db.query(Outreach).filter_by(job_id=job_id).one()
            assert row.gmail_draft_id == 'offline-draft-1'
            assert row.status == 'draft_ready'
            assert row.resume_b64
    finally:
        main.app.dependency_overrides.clear()
