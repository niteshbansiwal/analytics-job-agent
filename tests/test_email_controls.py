import base64
import json
from email import policy
from email.parser import BytesParser
import pytest
from app import gemini
from app.email_policy import (assert_no_variables, validate_settings, validate_final,
                              EmailValidationError, compose, DEFAULTS)
from app.integrations import build_mime, inspect_gmail_draft

URL='https://careers.example.com/opportunities/12345?team=analytics&region=in'
JD='Own SQL and Python analytics, dashboards, payments and stakeholder decision support. '*3
VALUE="I'm interested because this role needs payments analytics, useful dashboards, and clear stakeholder decisions. I can support these priorities with SQL, business analysis, reporting automation, and a practical approach to delivering trustworthy insights."


def output(**changes):
    return json.dumps({'value_proposition':VALUE,'fact_ids':['dashboards','plivo_margin'],**changes})


def generate(**kwargs):
    return gemini.build_email_preview('Visa','Analytics Manager',JD,'Shikha Sharad','Write crisp business-focused copy.',URL,**kwargs)


@pytest.mark.parametrize('token',['[FIRST_NAME]','{{first_name}}','{Recipient First Name}','${company}','<%= job.title %>','JOB_URL','first_name','[INSERT NAME]','&lbrace;&lbrace;first_name&rbrace;&rbrace;'])
def test_variables_blocked_in_subject_and_body(token):
    with pytest.raises(EmailValidationError):assert_no_variables(token,'Some valid body')
    with pytest.raises(EmailValidationError):assert_no_variables('Normal subject',token)


def test_controlled_generation_names_links_facts_and_template(monkeypatch):
    prompts=[]
    def fake(prompt,**kwargs):prompts.append(prompt);return output()
    monkeypatch.setattr(gemini,'gemini_text',fake)
    result=generate()
    assert result['body'].startswith('Hi Shikha,\n')
    assert URL in result['body']
    assert gemini.CAREER_FACTS['dashboards'] in result['body']
    assert result['body'].index('A bit about me:') < result['body'].index('1. ') < result['body'].index('2. ') < result['body'].index("I'm interested because")
    assert 'Shikha' not in prompts[0] and 'Sharad' not in prompts[0]
    assert 'Write crisp business-focused copy.' in prompts[0]
    assert result['repair_attempts']==0 and result['validation_status']=='passed'
    assert result['gmail_draft_created'] is False


def test_invalid_output_repaired_with_specific_feedback(monkeypatch):
    replies=iter([output(value_proposition='Hi [FIRST_NAME], I can help with analytics and business decisions.'),output()])
    prompts=[]
    def fake(prompt,**kwargs):prompts.append(prompt);return next(replies)
    monkeypatch.setattr(gemini,'gemini_text',fake)
    result=generate()
    assert result['repair_attempts']==1
    assert 'REPAIR REQUIRED' in prompts[1] and 'Unresolved email variable' in prompts[1]
    assert '[FIRST_NAME]' not in result['body']+result['subject']


@pytest.mark.parametrize('bad',['not JSON',output(fact_ids=['made_up','dashboards']),output(value_proposition='Saved 123456789 dollars in analytics projects for your team.'),output(value_proposition=None),output(subject='Unrequested key')])
def test_repeated_invalid_output_stops_after_three_calls(monkeypatch,bad):
    calls=[]
    monkeypatch.setattr(gemini,'gemini_text',lambda *a,**k:calls.append(1) or bad)
    with pytest.raises(gemini.GeminiError,match='after 3 attempts'):generate()
    assert len(calls)==3


def test_transport_errors_not_blindly_retried(monkeypatch):
    calls=[]
    def failed(*a,**k):calls.append(1);raise gemini.GeminiError('quota exceeded')
    monkeypatch.setattr(gemini,'gemini_text',failed)
    with pytest.raises(gemini.GeminiError,match='quota'):generate()
    assert len(calls)==1


def test_mime_has_exact_career_url_hyperlink_and_escaped_text():
    subject,body,_=compose({},'Example','Analytics Manager','Priya Recruiter',URL,VALUE,['dashboards','plivo_margin'],gemini.CAREER_FACTS)
    raw=build_mime('person@example.com','owner@example.com',subject,body+'\n<script>alert(1)</script>')
    result=inspect_gmail_draft({'message':{'raw':raw}})
    assert URL in result['html_links'] and URL in result['body']
    msg=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
    rendered=msg.get_body(preferencelist=('html',)).get_content()
    assert '&lt;script&gt;' in rendered and '<script>' not in rendered
    assert '>link here</a>' in rendered


def test_wrong_contact_and_missing_link_rejected():
    with pytest.raises(EmailValidationError,match='different contact'):
        validate_final('Neha, a role at Visa','Hi Shikha,\n\nAnalytics opportunity '+URL,'Shikha Sharad',URL)
    with pytest.raises(EmailValidationError,match='Greeting'):
        validate_final('Interest in Visa','Hi Neha,\n\nAnalytics opportunity '+URL,'Shikha Sharad',URL)
    with pytest.raises(EmailValidationError,match='URL is missing'):
        validate_final('Interest in Visa','Hi Shikha,\n\nAnalytics opportunity with your company.','Shikha Sharad',URL)


def test_unknown_variables_and_missing_required_tokens_rejected():
    with pytest.raises(EmailValidationError):validate_settings({'subjects':['Hi {{wrong_name}}, interested']})
    with pytest.raises(EmailValidationError):validate_settings({'body_template':DEFAULTS['body_template'].replace('{{job_link}}','missing')})
    with pytest.raises(EmailValidationError):validate_settings({'allowed_fact_ids':['dashboards']})


def test_final_provider_send_guard_reads_manually_edited_draft(monkeypatch):
    from app import integrations
    calls=[]
    monkeypatch.setattr(integrations,'gmail_get_draft',lambda *a:{})
    monkeypatch.setattr(integrations,'inspect_gmail_draft',lambda *a:{'subject':'Hi [FIRST_NAME], a role','body':VALUE})
    monkeypatch.setattr(integrations,'gmail',lambda *a,**k:calls.append(1))
    with pytest.raises(EmailValidationError):integrations.gmail_send_existing_draft(None,'draft')
    assert calls==[]


def test_final_provider_allows_valid_draft_but_blocks_late_edits(monkeypatch):
    from app import integrations
    checked={'to':'shikha@example.com','subject':'Analytics role','body':VALUE,
             'html_links':[URL],'html_body':'<p>'+VALUE+'</p>','attachment_sha256':['digest']}
    monkeypatch.setattr(integrations,'gmail_get_draft',lambda *a:{})
    monkeypatch.setattr(integrations,'inspect_gmail_draft',lambda *a:checked)
    sent=[]
    monkeypatch.setattr(integrations,'gmail',lambda *a,**k:sent.append(1) or {'id':'sent'})
    assert integrations.gmail_send_existing_draft(None,'draft',validated=dict(checked))['id']=='sent'
    with pytest.raises(RuntimeError,match='changed after validation'):
        integrations.gmail_send_existing_draft(None,'draft',validated={**checked,'subject':'Another subject'})
    assert sent==[1]


def test_preview_binds_selected_contact_and_resume_and_saved_controls(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app,owner
    from app.db import session,Job,Contact
    from app.resume_library import save_resume
    from io import BytesIO
    from reportlab.pdfgen import canvas
    monkeypatch.setenv('DEMO_MODE','false')
    calls=[]
    monkeypatch.setattr(gemini,'gemini_text',lambda *a,**k:calls.append(1) or output())
    buffer=BytesIO();pdf=canvas.Canvas(buffer);pdf.drawString(20,700,'Offline PDF');pdf.save()
    with session() as db:
        job=__import__('app.db',fromlist=['Job']).Job(company='Visa',title='Analytics Manager',source='test',post_url=URL,jd=JD)
        db.add(job);db.flush();jid=job.id
        for name,cid in [('Neha Ghai','c1'),('Shikha Sharad','c2')]:
            db.add(Contact(id=cid,job_id=jid,name=name,email=cid+'@example.com'))
        save_resume(db,'analytics',buffer.getvalue())
    app.dependency_overrides[owner]=lambda:'owner@example.com'
    try:
        client=TestClient(app)
        controls=client.get('/api/email/templates').json()['controls']
        controls.update(subject_mode='fixed',subject_index=0)
        assert client.put('/api/email/templates',json=controls).status_code==200
        invalid=client.put('/api/email/templates',json={**controls,'subjects':['Hi {{wrong}}, test']})
        assert invalid.status_code==422
        response=client.post('/api/jobs/'+jid+'/gemini-preview?contact_id=c2&resume_id=analytics')
        assert response.status_code==200,response.text
        result=response.json()
        assert result['subject'].startswith('Shikha,') and result['body'].startswith('Hi Shikha,')
        assert result['contact_id']=='c2' and len(result['resume_sha256'])==64
        assert len(calls)==1
    finally:app.dependency_overrides.clear()


@pytest.mark.parametrize('failure',['placeholder','wrong_contact','missing_hyperlink','wrong_resume'])
def test_scheduler_holds_invalid_live_draft_without_sending(monkeypatch,failure):
    from datetime import datetime,timezone,timedelta
    from app import workflow
    from app.db import session,Job,Contact,Outreach,Integration
    monkeypatch.setattr(workflow,'demo_mode',lambda:False)
    monkeypatch.setattr(workflow,'send_telegram',lambda *a,**k:None)
    now=datetime(2026,11,11,5,tzinfo=timezone.utc)
    sent=[]
    monkeypatch.setattr(workflow,'gmail_get_draft',lambda *a:{})
    latest={'subject':'Shikha, analytics role','body':'Hi Shikha,\n\nAnalytics opportunity '+URL,
            'to':'shikha@example.com','html_links':[URL],'attachment_count':1,'attachment_sha256':['bad']}
    if failure=='placeholder':latest['subject']='Hi [FIRST_NAME], analytics role'
    if failure=='wrong_contact':latest['subject']='Neha, analytics role'
    if failure=='missing_hyperlink':latest['html_links']=[]
    monkeypatch.setattr(workflow,'inspect_gmail_draft',lambda *a:latest)
    monkeypatch.setattr(workflow,'gmail_send_existing_draft',lambda *a:sent.append(1))
    with session() as db:
        job=Job(company='Visa',title='Analytics Manager',source='test',post_url=URL)
        db.add(job);db.flush()
        c=Contact(job_id=job.id,name='Shikha Sharad',email='shikha@example.com')
        db.add(c);db.flush()
        db.add(Integration(provider='gmail',owner_email='owner@example.com'))
        row=Outreach(job_id=job.id,contact_id=c.id,to_email=c.email,status='scheduled',
            gmail_draft_id='draft',due_at=now-timedelta(minutes=1),resume_b64=base64.b64encode(b'pdf').decode())
        db.add(row);db.commit()
        result=workflow.send_due(db,clock=now)
        assert result['sent']==0 and sent==[]
        assert row.status=='needs_attention'


def test_valid_controlled_draft_still_sends_through_scheduler(monkeypatch):
    from datetime import datetime,timezone,timedelta
    from app import workflow,integrations
    from app.db import session,Job,Contact,Outreach,Integration
    monkeypatch.setattr(workflow,'demo_mode',lambda:False)
    monkeypatch.setattr(workflow,'send_telegram',lambda *a,**k:None)
    at=datetime(2026,11,11,5,tzinfo=timezone.utc)
    attachment=b'original PDF bytes'
    body='Hi Shikha,\n\nAnalytics opportunity '+URL
    raw=build_mime('shikha@example.com','owner@example.com','Shikha, analytics role',body,attachment,'resume.pdf')
    monkeypatch.setattr(workflow,'gmail_get_draft',lambda *a:{'message':{'raw':raw}})
    monkeypatch.setattr(integrations,'gmail_get_draft',lambda *a:{'message':{'raw':raw}})
    sent=[]
    monkeypatch.setattr(integrations,'gmail',lambda *a,**k:sent.append(1) or {'id':'sent-id','threadId':'thread'})
    with session() as db:
        job=Job(company='Visa',title='Analytics Manager',source='test',post_url=URL)
        db.add(job);db.flush()
        c=Contact(job_id=job.id,name='Shikha Sharad',email='shikha@example.com')
        db.add(c);db.flush()
        db.add(Integration(provider='gmail',owner_email='owner@example.com'))
        row=Outreach(job_id=job.id,contact_id=c.id,to_email=c.email,status='scheduled',
            gmail_draft_id='draft',due_at=at-timedelta(minutes=1),resume_b64=base64.b64encode(attachment).decode())
        db.add(row);db.commit()
        result=workflow.send_due(db,clock=at)
        assert result['sent']==1 and sent==[1]
        assert row.sent_message_id=='sent-id'


def test_owner_v2_and_followup():
    from app.email_policy import require_v2_format, compose_followup
    body = ('Hi Shikha,\n\nA bit about me:\n1. American Express analytics.\n'
            '2. Plivo margin improvements.\n\n'
            "I'm interested because I can help with this role.\n\n"
            "If you're still hiring, would you be open to a quick conversation?\n\n"
            'Nitesh Bansiwal')
    require_v2_format(body)
    with pytest.raises(EmailValidationError, match='numbered bullets'):
        require_v2_format(body.replace('1. ', '- '))
    followup = compose_followup(DEFAULTS, 'Visa', 'Analytics Manager', 'Shikha Sharad')
    assert followup.startswith('Hi Shikha,') and 'Visa' in followup
    with pytest.raises(EmailValidationError, match='Follow-up'):
        validate_settings({'followup_template': 'Hello person'})
