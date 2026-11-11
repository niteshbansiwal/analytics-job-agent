import base64
from io import BytesIO
from uuid import uuid4
import importlib
import pytest
from reportlab.pdfgen import canvas
from fastapi.testclient import TestClient
from app.db import session, Job
from app.resume_library import save_resume, get_resume, list_resumes, digest
from app.integrations import build_mime, inspect_gmail_draft
main = importlib.import_module('app.main')

def pdf(text='Original resume'):
    buffer = BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(40, 700, text)
    c.save()
    return buffer.getvalue()

def test_both_versions_are_byte_identical_and_replacement_guarded():
    analytics, science = pdf('Analytics'), pdf('Data Science')
    with session() as db:
        save_resume(db, 'analytics', analytics)
        save_resume(db, 'data_science', science)
    with session() as db:
        assert get_resume(db, 'analytics')[1] == analytics
        assert get_resume(db, 'data_science')[1] == science
        assert len([r for r in list_resumes(db) if r['configured']]) == 2
        with pytest.raises(ValueError, match='replacement'):
            save_resume(db, 'analytics', science)
        save_resume(db, 'analytics', science, expected_sha256=digest(analytics))
    with session() as db:
        assert get_resume(db, 'analytics')[1] == science

@pytest.mark.parametrize('kind,raw', [('other',b'%PDF-1.7'),('analytics',b'not a pdf'),('analytics',b'%PDF-broken')])
def test_invalid_uploads_rejected(kind,raw):
    with session() as db:
        with pytest.raises(ValueError):
            save_resume(db,kind,raw)

def test_mime_verification_checks_actual_attachment_bytes():
    original=pdf()
    raw=build_mime('recipient@example.com','owner@example.com','Subject','Body',original,'resume.pdf')
    inspected=inspect_gmail_draft({'message':{'raw':raw}})
    assert inspected['attachment_sha256'] == [digest(original)]
    altered=build_mime('recipient@example.com','owner@example.com','Subject','Body',pdf('Wrong file'),'resume.pdf')
    assert inspect_gmail_draft({'message':{'raw':altered}})['attachment_sha256'] != [digest(original)]

def test_api_upload_preview_missing_stale_and_auth(monkeypatch):
    monkeypatch.setattr(main,'demo_mode',lambda:False)
    client=TestClient(main.app)
    assert client.get('/api/resumes').status_code==401
    main.app.dependency_overrides[main.owner]=lambda:'test-owner'
    job_id=str(uuid4())
    try:
        with session() as db:
            db.add(Job(id=job_id,company='Test',title='Analytics Manager',source='test',post_url='https://example.com/job'))
        original=pdf()
        payload={'resume_id':'analytics','resume_sha256':digest(original)}
        url='/api/jobs/'+job_id+'/reviewed-resume.pdf'
        assert client.post(url,json=payload).status_code==409
        uploaded=client.post('/api/resumes/analytics',files={'file':('resume.pdf',original,'application/pdf')})
        assert uploaded.status_code==200,uploaded.text
        preview=client.post(url,json=payload)
        assert preview.content==original
        assert preview.headers['cache-control']=='no-store'
        assert client.post(url,json={**payload,'resume_sha256':'0'*64}).status_code==409
        assert client.post(url,json={**payload,'selected_bullets':{}}).status_code==422
        assert client.post('/api/outreach/fake/schedule').status_code==409
    finally:
        main.app.dependency_overrides.clear()

def test_wrong_attachment_is_rejected_before_success(monkeypatch):
    from app import integrations
    from app.db import Contact, Integration, Outreach
    monkeypatch.setattr(main,'demo_mode',lambda:False)
    main.app.dependency_overrides[main.owner]=lambda:'test-owner'
    job_id, contact_id=str(uuid4()),str(uuid4())
    original, wrong=pdf('Correct'),pdf('Incorrect')
    with session() as db:
        save_resume(db,'data_science',original)
        db.add(Job(id=job_id,company='Test',title='Data Science Manager',source='test',post_url='https://example.com/job'))
        db.add(Contact(id=contact_id,job_id=job_id,name='Priya Recruiter',email='recruiter@example.com'))
        db.add(Integration(provider='gmail',owner_email='owner@example.com',refresh_ciphertext='test'))
    raw={}
    deleted=[]
    def create(db,to,subject,body,attachment,filename):
        assert attachment==original
        raw['value']=build_mime(to,'owner@example.com',subject,body,wrong,filename)
        return {'id':'wrong-draft'}
    monkeypatch.setattr(integrations,'gmail_create_draft',create)
    monkeypatch.setattr(integrations,'gmail_get_draft',lambda db,id:{'message':{'raw':raw['value']}})
    monkeypatch.setattr(integrations,'gmail_delete_draft',lambda db,id:deleted.append(id))
    try:
        response=TestClient(main.app).post('/api/jobs/'+job_id+'/reviewed-draft',json={
            'contact_id':contact_id,'recipient_email':'recruiter@example.com',
            'subject':'Application','body':'Hi Priya,\n\nI am interested in this analytics opportunity. https://example.com/job',
            'resume_id':'data_science','resume_sha256':digest(original),'reviewed':True})
        assert response.status_code==502,response.text
        assert deleted==['wrong-draft']
        with session() as db:
            assert db.query(Outreach).filter_by(job_id=job_id).count()==0
    finally:
        main.app.dependency_overrides.clear()
