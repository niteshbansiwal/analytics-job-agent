from __future__ import annotations
import httpx
import base64
import hmac
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Depends, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .config import ROOT, env, settings, demo_mode
from .db import session, engine, Job, Contact, Outreach, Integration, Event, get_kv, set_kv, seed_demo, log, now
from .calendar_rules import next_fixed_send
from .integrations import (google_oauth_url, google_code_exchange, google_userinfo, save_google_refresh,
    rocketreach_lookup, rocketreach_check_status)
from .workflow import approve_job, prepare_outreach, cancel_outreach, monitor_replies, send_due

app = FastAPI(title='Analytics Job Agent', version='0.1.0', docs_url=None, redoc_url=None)
if (ROOT / 'public').is_dir():
    app.mount('/assets', StaticFiles(directory=str(ROOT / 'public')), name='assets')

def is_https():
    return env('APP_BASE_URL').startswith('https://')

def create_session(email: str):
    secret = env('SESSION_SECRET')
    if len(secret) < 32:
        raise RuntimeError('SESSION_SECRET must be 32 or more characters')
    payload = base64.urlsafe_b64encode(f'{email}|{int(time.time())+7*86400}'.encode()).decode()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f'{payload}.{sig}'

def check_session(token: str):
    try:
        payload, signature = token.rsplit('.', 1)
        expected = hmac.new(env('SESSION_SECRET').encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return ''
        decoded = base64.urlsafe_b64decode(payload.encode()).decode()
        email, expires = decoded.rsplit('|', 1)
        if int(expires) < time.time() or email.lower() != env('ALLOWED_GOOGLE_EMAIL').lower():
            return ''
        return email
    except Exception:
        return ''

def owner(request: Request):
    if demo_mode() and not os.getenv('VERCEL'):
        return 'demo-local-owner'
    user = check_session(request.cookies.get('job_agent_session', ''))
    if not user:
        raise HTTPException(401, 'Sign in with your configured Google account')
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        # Defence in depth against cookie-based cross-site requests.
        origin = request.headers.get('origin', '')
        if origin and origin.rstrip('/') != env('APP_BASE_URL').rstrip('/'):
            raise HTTPException(403, 'Cross-origin request rejected')
    return user

def json_job(job: Job, db: Session):
    contacts = db.execute(select(Contact).where(Contact.job_id == job.id)).scalars().all()
    outreach = db.execute(select(Outreach).where(Outreach.job_id == job.id)).scalar_one_or_none()
    return {
      'id': job.id, 'company': job.company, 'title': job.title, 'location': job.location,
      'arrangement': job.arrangement, 'experience': job.experience, 'salary': job.salary,
      'source': job.source, 'post_url': job.post_url, 'apply_url': job.apply_url,
      'post_age': job.post_age, 'posting_date_kind': job.posting_date_kind,
      'verified_at': job.verified_at.isoformat() if job.verified_at else '',
      'author_name': job.author_name, 'author_url': job.author_url, 'jd': job.jd,
      'fit_score': job.fit_score, 'fit_why': job.fit_why, 'gaps': job.gaps,
      'leadership': job.leadership, 'verification': job.verification, 'status': job.status,
      'is_demo': bool(job.is_demo), 'created_at': job.created_at.isoformat(),
      'contacts': [{
        'id': c.id, 'name': c.name, 'title': c.title, 'category': c.category,
        'linkedin_url': c.linkedin_url, 'evidence_url': c.evidence_url, 'reason': c.reason,
        'relationship': c.relationship, 'email': c.email, 'email_status': c.email_status,
         'rocketreach_status': _rocketreach_state(db, c.id).get('status', 'not_started'),
         'rocketreach_next_check_at': _rocketreach_state(db, c.id).get('next_check_at', ''),
         'rocketreach_lookup_count': _rocketreach_state(db, c.id).get('lookup_count', 0),
      } for c in contacts],
      'outreach': json_outreach(outreach) if outreach else None,
    }

def json_outreach(row: Outreach | None):
    if row is None:
        return None
    return {k: getattr(row, k).isoformat() if isinstance(getattr(row, k), datetime) else getattr(row, k)
            for k in ['id', 'job_id', 'contact_id', 'to_email', 'subject', 'body', 'resume_name',
                      'gmail_draft_id', 'gmail_thread_id', 'sent_message_id', 'status',
                      'due_at', 'sent_at', 'last_reply_at', 'last_reply_class',
                      'followup_count', 'error', 'created_at']}

@app.get('/preview')
def preview():
    if os.getenv('VERCEL'):
        return RedirectResponse('/preview.html', status_code=307)
    return FileResponse(ROOT / 'public' / 'preview.html', media_type='text/html')

@app.get('/')
def index():
    if os.getenv('VERCEL'):
        return RedirectResponse('/welcome.html', status_code=307)
    return FileResponse(ROOT / 'public' / 'welcome.html', media_type='text/html')

@app.get('/app')
def workspace():
    if os.getenv('VERCEL'):
        return RedirectResponse('/index.html', status_code=307)
    return FileResponse(ROOT / 'public' / 'index.html', media_type='text/html')

@app.get('/api/health')
def health():
    return {'ok': True, 'demo': demo_mode(), 'version': '0.1.0'}

@app.get('/api/auth/status')
def auth_status(request: Request):
    """Configuration readiness only; does not validate third-party credentials."""
    app_origin_configured = env('APP_BASE_URL').startswith('https://')
    owner_email_configured = bool(env('ALLOWED_GOOGLE_EMAIL'))
    secrets_configured = len(env('SESSION_SECRET')) >= 32 and bool(env('TOKEN_ENCRYPTION_KEY'))
    google_oauth_configured = bool(env('GOOGLE_CLIENT_ID') and env('GOOGLE_CLIENT_SECRET'))
    google_ready = bool(app_origin_configured and owner_email_configured
                        and secrets_configured and google_oauth_configured)
    database_configured = bool(env('DATABASE_URL').startswith(
        ('postgres://', 'postgresql://', 'postgresql+psycopg://')))
    return {
        'signed_in': bool(check_session(request.cookies.get('job_agent_session', ''))),
        'app_origin_configured': app_origin_configured,
        'owner_email_configured': owner_email_configured,
        'secrets_configured': secrets_configured,
        'google_oauth_configured': google_oauth_configured,
        'google_ready': google_ready,
        'database_configured': database_configured,
        'search_configured': bool(env('SERPER_API_KEY')),
        'chatgpt_search_configured': bool(env('OPENAI_API_KEY')),
        'gmail_oauth_configured': bool(google_ready and env('TOKEN_ENCRYPTION_KEY') and database_configured),
        'send_enabled': False,
        'demo_available': True,
    }

def start_google_oauth(*, gmail: bool = False):
    if not all([env('GOOGLE_CLIENT_ID'), env('GOOGLE_CLIENT_SECRET'), env('APP_BASE_URL'),
                env('ALLOWED_GOOGLE_EMAIL')]) or len(env('SESSION_SECRET')) < 32:
        raise HTTPException(503, 'Google sign-in is not configured. See the homepage setup checklist.')
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(google_oauth_url(state, gmail=gmail), status_code=302)
    response.set_cookie('oauth_state', state, httponly=True, secure=is_https(), samesite='lax', max_age=600)
    response.set_cookie('oauth_flow', 'gmail' if gmail else 'login',
                        httponly=True, secure=is_https(), samesite='lax', max_age=600)
    return response

@app.get('/api/auth/google/start')
def google_auth_start():
    return start_google_oauth(gmail=False)

@app.get('/api/auth/gmail/start')
def gmail_auth_start(user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Gmail integration is unavailable in demo mode.')
    if not env('DATABASE_URL') or not env('TOKEN_ENCRYPTION_KEY'):
        raise HTTPException(503, 'Configure PostgreSQL and TOKEN_ENCRYPTION_KEY before connecting Gmail.')
    return start_google_oauth(gmail=True)

@app.get('/api/auth/google/callback')
def google_auth_callback(request: Request, code: str = '', state: str = ''):
    expected = request.cookies.get('oauth_state', '')
    mode = request.cookies.get('oauth_flow', 'login')
    if not expected or not hmac.compare_digest(expected, state) or not code or mode not in ('login', 'gmail'):
        raise HTTPException(400, 'OAuth state validation failed. Please try signing in again.')
    try:
        result = google_code_exchange(code)
        account = google_userinfo(result['access_token'])
        email = account.get('email', '').lower()
        if not account.get('email_verified') or email != env('ALLOWED_GOOGLE_EMAIL').lower():
            raise HTTPException(403, 'This Google account is not the configured workspace owner.')
        if mode == 'gmail':
            if check_session(request.cookies.get('job_agent_session', '')) != email:
                raise HTTPException(403, 'Sign in before connecting Gmail.')
            if demo_mode():
                raise HTTPException(409, 'Disable demo mode before connecting Gmail.')
            with session() as db:
                refresh = result.get('refresh_token')
                if not refresh and not db.get(Integration, 'gmail'):
                    raise HTTPException(400, 'Google did not issue a refresh token. Reconnect with consent.')
                if refresh:
                    save_google_refresh(db, email, refresh)
                log(db, 'gmail_connected', owner=email)
            response = RedirectResponse('/app?connected=gmail', status_code=302)
        else:
            response = RedirectResponse('/app', status_code=302)
        response.set_cookie('job_agent_session', create_session(email), httponly=True,
                            secure=is_https(), samesite='lax', max_age=7*86400)
        response.delete_cookie('oauth_state')
        response.delete_cookie('oauth_flow')
        return response
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f'Google OAuth failed: {type(exc).__name__}')

@app.post('/api/auth/logout')
def logout():
    response = Response(status_code=204)
    response.delete_cookie('job_agent_session')
    return response

@app.get('/api/overview')
def overview(user: str = Depends(owner)):
    if os.getenv('VERCEL') and not env('DATABASE_URL'):
        raise HTTPException(503, 'PostgreSQL DATABASE_URL is not configured yet. See the homepage setup checklist.')
    with session() as db:
        seed_demo(db)
        db.flush()
        jobs = db.execute(select(Job).order_by(Job.fit_score.desc(), Job.created_at.desc())).scalars().all()
        letters = db.execute(select(Outreach).order_by(Outreach.created_at.desc())).scalars().all()
        events = db.execute(select(Event).order_by(Event.created_at.desc()).limit(35)).scalars().all()
        integration = db.get(Integration, 'gmail')
        cfg = settings()
        return {
            'demo': demo_mode(), 'jobs': [json_job(j, db) for j in jobs],
            'outreach': [json_outreach(o) for o in letters],
            'events': [{'action': e.action, 'details': json.loads(e.details or '{}'),
                        'at': e.created_at.isoformat(), 'job_id': e.job_id} for e in events],
            'integrations': {
               'gmail': bool(integration) and not demo_mode(),
               'telegram': bool(env('TELEGRAM_BOT_TOKEN') and env('TELEGRAM_CHAT_ID')),
               'rocketreach': bool(env('ROCKETREACH_API_KEY')),
               'serper': bool(env('SERPER_API_KEY')),
               'openai': bool(env('OPENAI_API_KEY')),
               'database': 'SQLite / local' if db.bind.dialect.name == 'sqlite' else 'PostgreSQL',
            },
            'settings': {
                'timezone': cfg['timezone'], 'search_time': cfg['search']['at'],
                'search_weekdays': cfg['search']['weekdays'], 'lookback_days': cfg['search']['lookback_days'],
                'max_jobs': cfg['search']['shortlist_size'], 'send_start': cfg['email']['start'],
                'send_end': cfg['email']['end'], 'send_time': cfg['email']['preferred_send_time'],
                'send_enabled': cfg['email']['scheduled_sending_enabled'],
                'followup_enabled': cfg['email']['followup']['enabled'],
                'followup_hours': cfg['email']['followup']['hours'],
                'max_followups': cfg['email']['followup']['max_attempts'],
            },
        }

@app.get('/api/jobs/{job_id}')
def job_details(job_id: str, user: str = Depends(owner)):
    with session() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Job not found')
        return json_job(job, db)

@app.post('/api/jobs/{job_id}/approve')
def approve(job_id: str, user: str = Depends(owner)):
    with session() as db:
        try:
            row = approve_job(db, job_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return json_outreach(row)

@app.post('/api/jobs/{job_id}/test-gmail-draft')
def test_gmail_resume_attachment(job_id: str, user: str = Depends(owner)):
    """Explicit owner-initiated smoke test. Draft to SELF; never send or approve job.

    Creates a real Gmail draft containing a generated PDF, reads the draft
    back from Gmail and checks its recipient and attachment filename.
    """
    if demo_mode():
        raise HTTPException(409, 'Gmail draft testing is unavailable in demo mode.')
    from .resume import tailored_pdf
    from .integrations import (gmail_create_draft, gmail_get_draft,
                               inspect_gmail_draft, gmail_delete_draft)
    import re
    with session() as db:
        connection = db.get(Integration, 'gmail')
        if not connection:
            raise HTTPException(409, 'Connect Gmail under Configuration first.')
        job = db.get(Job, job_id)
        if not job or job.is_demo:
            raise HTTPException(404, 'Imported job not found.')
        # This is only a delivery/attachment test, not the final AI resume.
        pdf = tailored_pdf(job.jd, job.title)
        if not pdf.startswith(b'%PDF'):
            raise HTTPException(500, 'Generated resume is not a valid PDF.')
        safe_company = re.sub(r'[^A-Za-z0-9_-]+', '_', job.company).strip('_')[:36] or 'Job'
        filename = f'ATLAS_TEST_{safe_company}_Resume.pdf'
        subject = f'[ATLAS TEST - DO NOT SEND] Resume PDF attachment: {job.company}'
        body = ('This is a private Atlas Gmail attachment test addressed to myself.\\n'
                'The attached PDF is generated from the current resume template, '
                'not the final AI-personalized version.\\n'
                'Please review the attachment and discard this draft after checking.\\n'
                'No recruiter has been contacted.')
        draft_id = ''
        try:
            created = gmail_create_draft(db, connection.owner_email, subject, body, pdf, filename)
            draft_id = created['id']
            verified = inspect_gmail_draft(gmail_get_draft(db, draft_id))
            if (verified['to'].lower() != connection.owner_email.lower()
                    or verified['attachment_count'] != 1
                    or verified['attachment_filenames'] != [filename]
                    or verified['subject'] != subject):
                raise RuntimeError('Draft verification mismatch (recipient, subject or PDF attachment).')
            log(db, 'gmail_attachment_test_verified', job.id,
                gmail_draft_id=draft_id, attachment_count=1)
            return {'verified': True, 'job_id': job.id, 'company': job.company,
                    'to': connection.owner_email, 'subject': subject,
                    'attachment_count': 1, 'filename': filename,
                    'draft_id': draft_id,
                    'gmail_url': 'https://mail.google.com/mail/u/0/#drafts',
                    'sent': False}
        except Exception as exc:
            if draft_id:
                try:
                    gmail_delete_draft(db, draft_id)
                except Exception:
                    pass
            detail = (f'Gmail API returned HTTP {exc.response.status_code}'
                      if isinstance(exc, httpx.HTTPStatusError)
                      else type(exc).__name__)
            raise HTTPException(502, f'Gmail draft verification failed: {detail}. '
                                'Check Gmail OAuth and API permissions. No email was sent.')


@app.post('/api/jobs/{job_id}/reject')
def reject(job_id: str, user: str = Depends(owner)):
    with session() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Job not found')
        if db.execute(select(Outreach).where(Outreach.job_id == job.id)).scalar_one_or_none():
            raise HTTPException(409, 'Cannot reject an approved job. Cancel its outreach first.')
        job.status = 'rejected'
        log(db, 'job_rejected', job.id)
        return {'status': 'rejected'}

class ContactInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    title: str = Field(default='', max_length=200)
    category: str = Field(default='User-supplied professional contact', max_length=80)
    linkedin_url: str = Field(default='', max_length=500)
    evidence_url: str = Field(default='', max_length=500)
    email: str = Field(default='', max_length=180)
    reason: str = Field(default='Contact identified by owner', max_length=300)

@app.post('/api/jobs/{job_id}/contacts')
def add_contact(job_id: str, data: ContactInput, user: str = Depends(owner)):
    if data.email and ('@' not in data.email or '\n' in data.email):
        raise HTTPException(422, 'Enter a valid professional email address')
    if data.linkedin_url and not data.linkedin_url.startswith('https://www.linkedin.com/in/'):
        raise HTTPException(422, 'LinkedIn profile must use https://www.linkedin.com/in/...')
    with session() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Job not found')
        if len(db.execute(select(Contact).where(Contact.job_id == job_id)).scalars().all()) >= 5:
            raise HTTPException(409, 'Five contacts per job is the maximum')
        row = Contact(job_id=job_id, name=data.name, title=data.title, category=data.category,
                      linkedin_url=data.linkedin_url, evidence_url=data.evidence_url,
                      email=data.email, email_status='user_supplied' if data.email else 'unavailable',
                      relationship='potential', reason=data.reason)
        db.add(row)
        log(db, 'contact_added', job_id, email_present=bool(data.email))
        return {'saved': True}

class RocketReachLookupInput(BaseModel):
    retry: bool = False


def _rocketreach_state(db: Session, contact_id: str) -> dict:
    try:
        state = json.loads(get_kv(db, 'rocketreach:' + contact_id, '{}'))
        if not isinstance(state, dict):
            return {}
        # Older Atlas versions incorrectly marked a completed, empty result
        # as final. Existing provider IDs are still eligible for rechecks.
        if state.get('status') == 'not_found' and state.get('provider_id') and not state.get('check_count'):
            state['status'] = 'awaiting_email'
        return state
    except (TypeError, ValueError):
        return {}


def _rocketreach_public(state: dict, contact: Contact) -> dict:
    status = state.get('status', 'not_started')
    return {'status': status,
            'email': contact.email if status in ('returned', 'already_found') else '',
            'email_status': contact.email_status if status in ('returned', 'already_found') else '',
            'detail': state.get('detail', ''),
            'next_check_at': state.get('next_check_at', ''),
            'check_count': state.get('check_count', 0),
            'lookup_count': state.get('lookup_count', 0)}


def _save_rocketreach_result(db: Session, contact: Contact, result: dict,
                             *, is_check: bool = False) -> dict:
    from datetime import timedelta
    previous = _rocketreach_state(db, contact.id)
    timestamp = now()
    status = result.get('status', 'api_error')
    provider_id = str(result.get('provider_id') or previous.get('provider_id') or '')
    check_count = int(previous.get('check_count') or 0) + (1 if is_check else 0)
    lookup_count = int(previous.get('lookup_count') or 0) + (0 if is_check else 1)

    if status in ('pending', 'awaiting_email') and not provider_id:
        status = 'not_found' if status == 'awaiting_email' else 'api_error'
    if status == 'awaiting_email' and check_count >= 3:
        status = 'not_found'
    if status == 'returned':
        contact.email = result['email']
        contact.email_status = result.get('email_status', 'rocketreach_unverified')
    # A temporary provider failure should not erase a live lookup.
    if status in ('api_error', 'rate_limited') and is_check and previous.get('status') in ('pending', 'awaiting_email'):
        status = previous['status']
        check_count = int(previous.get('check_count') or 0)

    delays = (12, 40, 120, 180)
    next_check_at = ((timestamp + timedelta(seconds=delays[min(check_count, len(delays) - 1)])).isoformat()
                     if status in ('pending', 'awaiting_email') and provider_id else '')
    detail = str(result.get('detail') or '')[:180]
    if status == 'not_found' and provider_id:
        detail = 'No professional email after checking the existing RocketReach lookup; optional paid retry available later.'
    state = {
        'status': status, 'provider_id': provider_id,
        'checked_at': timestamp.isoformat(), 'next_check_at': next_check_at,
        'first_lookup_at': previous.get('first_lookup_at') or timestamp.isoformat(),
        'last_lookup_at': previous.get('last_lookup_at', '') if is_check else timestamp.isoformat(),
        'lookup_count': lookup_count, 'check_count': check_count,
        'detail': detail,
    }
    set_kv(db, 'rocketreach:' + contact.id, json.dumps(state))
    log(db, 'rocketreach_status_checked' if is_check else 'rocketreach_lookup_result',
        contact.job_id, contact_id=contact.id, status=status,
        lookup_count=lookup_count, check_count=check_count,
        email_found=bool(contact.email))
    return _rocketreach_public(state, contact)


@app.post('/api/contacts/{contact_id}/rocketreach/lookup')
def lookup_contact_email(contact_id: str, data: RocketReachLookupInput,
                         user: str = Depends(owner)):
    """Start at most two explicit person lookups; rechecks never start another."""
    if demo_mode():
        raise HTTPException(409, 'RocketReach lookup is disabled in demo mode.')
    if not env('ROCKETREACH_API_KEY'):
        raise HTTPException(409, 'Configure ROCKETREACH_API_KEY first.')
    with session() as db:
        # PostgreSQL row lock prevents two clicks from creating concurrent paid lookups.
        contact = db.get(Contact, contact_id, with_for_update=True)
        if not contact or not db.get(Job, contact.job_id) or db.get(Job, contact.job_id).is_demo:
            raise HTTPException(404, 'Imported hiring contact not found.')
        if contact.email:
            return {'status': 'already_found', 'email': contact.email,
                    'email_status': contact.email_status, 'detail': 'Email already saved; no lookup started.'}
        from urllib.parse import urlsplit
        url = urlsplit(contact.linkedin_url)
        if url.scheme != 'https' or (url.hostname or '').lower() not in (
                'www.linkedin.com', 'linkedin.com', 'in.linkedin.com') or not url.path.startswith('/in/'):
            raise HTTPException(422, 'A valid LinkedIn person profile URL is required.')
        previous = _rocketreach_state(db, contact.id)
        if previous:
            if not data.retry:
                result = _rocketreach_public(previous, contact)
                result['detail'] = 'Existing lookup retained; use status recheck instead of another paid lookup.'
                return result
            if previous.get('status') in ('pending', 'awaiting_email') and previous.get('provider_id'):
                raise HTTPException(409, 'Recheck the existing RocketReach lookup first; do not pay again.')
            if int(previous.get('lookup_count') or 1) >= 2:
                raise HTTPException(409, 'Two lookups already attempted for this contact. Review manually.')
            from datetime import timedelta
            last = previous.get('last_lookup_at') or previous.get('checked_at')
            if last:
                try:
                    if now() < datetime.fromisoformat(last) + timedelta(minutes=10):
                        raise HTTPException(409, 'Wait 10 minutes before another potentially chargeable lookup.')
                except ValueError:
                    pass
        try:
            result = rocketreach_lookup(contact.linkedin_url)
        except (httpx.HTTPError, ValueError) as exc:
            result = {'status': 'api_error', 'detail': 'RocketReach request failed: ' + type(exc).__name__}
        return _save_rocketreach_result(db, contact, result, is_check=False)


@app.post('/api/contacts/{contact_id}/rocketreach/check')
def check_contact_lookup(contact_id: str, user: str = Depends(owner)):
    """Recheck existing RocketReach profile ID without initiating a fresh lookup."""
    if demo_mode():
        raise HTTPException(409, 'RocketReach lookup is disabled in demo mode.')
    with session() as db:
        contact = db.get(Contact, contact_id, with_for_update=True)
        if not contact:
            raise HTTPException(404, 'Contact not found.')
        if contact.email:
            return {'status': 'already_found', 'email': contact.email,
                    'email_status': contact.email_status, 'detail': 'Email already saved.'}
        previous = _rocketreach_state(db, contact.id)
        if previous.get('status') not in ('pending', 'awaiting_email') or not previous.get('provider_id'):
            result = _rocketreach_public(previous, contact)
            result['detail'] = 'No outstanding provider lookup to check.'
            return result
        next_check = previous.get('next_check_at')
        if next_check:
            try:
                if now() < datetime.fromisoformat(next_check):
                    result = _rocketreach_public(previous, contact)
                    result['detail'] = 'Waiting for provider enrichment; no new lookup started.'
                    return result
            except ValueError:
                pass
        try:
            result = rocketreach_check_status(previous['provider_id'], contact.linkedin_url)
        except (httpx.HTTPError, ValueError) as exc:
            result = {'status': 'api_error', 'detail': 'Provider status check failed: ' + type(exc).__name__}
        return _save_rocketreach_result(db, contact, result, is_check=True)



class JobInput(BaseModel):
    company: str = Field(min_length=2, max_length=180)
    title: str = Field(min_length=4, max_length=220)
    post_url: HttpUrl
    location: str = Field(default='Not stated', max_length=180)
    jd: str = Field(default='', max_length=6000)
    author_name: str = Field(default='', max_length=150)
    post_age: str = Field(default='Not verified', max_length=150)

@app.post('/api/jobs/import')
def import_job(data: JobInput, user: str = Depends(owner)):
    from .discovery import canonical, source_type, ranking
    url = canonical(str(data.post_url))
    if not url.startswith('https://'):
        raise HTTPException(422, 'Only public HTTPS post URLs are accepted')
    score, why, gaps = ranking(data.company, data.title, data.location, data.jd, url)
    with session() as db:
        old = db.execute(select(Job).where(Job.post_url == url)).scalar_one_or_none()
        if old:
            return {'id': old.id, 'duplicate': True}
        row = Job(company=data.company, title=data.title, location=data.location, jd=data.jd,
                  post_url=url, source=source_type(url), post_age=data.post_age,
                  posting_date_kind='user_supplied_unverified', author_name=data.author_name,
                  verification='user_supplied_unverified', fit_score=score, fit_why=why, gaps=gaps)
        db.add(row)
        db.flush()
        log(db, 'job_imported', row.id, url=url)
        return {'id': row.id, 'duplicate': False}

class CsvImportInput(BaseModel):
    csv_text: str = Field(min_length=10, max_length=400000)


@app.get('/api/spreadsheets/template.xlsx')
def spreadsheet_template(user: str = Depends(owner)):
    from .spreadsheet_import import template_xlsx
    from io import BytesIO
    return StreamingResponse(BytesIO(template_xlsx()),
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="Atlas_Job_Import_Template.xlsx"'})


@app.post('/api/jobs/import-spreadsheet')
async def import_spreadsheet(file: UploadFile = File(...), user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Real spreadsheet import is disabled in demo mode.')
    from .spreadsheet_import import MAX_FILE_BYTES, parse_file, import_rows
    filename = file.filename or ''
    if not filename.lower().endswith(('.csv', '.xlsx')):
        raise HTTPException(422, 'Choose a .xlsx or .csv file.')
    try:
        content = await file.read(MAX_FILE_BYTES + 1)
        rows = parse_file(content, filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    finally:
        await file.close()
    with session() as db:
        result = import_rows(db, rows)
        log(db, 'spreadsheet_import_finished', **{k: result[k] for k in (
            'created', 'duplicates', 'skipped', 'contacts_created', 'rows_read')})
        return result


@app.post('/api/jobs/import-csv')
def import_pasted_csv(data: CsvImportInput, user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Real spreadsheet import is disabled in demo mode.')
    from .spreadsheet_import import parse_csv, import_rows
    try:
        rows = parse_csv(data.csv_text)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with session() as db:
        result = import_rows(db, rows)
        log(db, 'pasted_csv_import_finished', **{k: result[k] for k in (
            'created', 'duplicates', 'skipped', 'contacts_created', 'rows_read')})
        return result


@app.post('/api/outreach/{outreach_id}/prepare')
def prepare(outreach_id: str, user: str = Depends(owner)):
    with session() as db:
        try:
            return json_outreach(prepare_outreach(db, outreach_id))
        except ValueError as e:
            raise HTTPException(400, str(e))

@app.post('/api/outreach/{outreach_id}/cancel')
def cancel(outreach_id: str, user: str = Depends(owner)):
    with session() as db:
        try:
            return json_outreach(cancel_outreach(db, outreach_id))
        except ValueError as e:
            raise HTTPException(400, str(e))

@app.get('/api/outreach/{outreach_id}/resume.pdf')
def get_tailored_resume(outreach_id: str, user: str = Depends(owner)):
    with session() as db:
        row = db.get(Outreach, outreach_id)
        if not row:
            raise HTTPException(404, 'Resume not found')
        b = base64.b64decode(row.resume_b64)
        from io import BytesIO
        return StreamingResponse(BytesIO(b), media_type='application/pdf', headers={
          'Content-Disposition': f'attachment; filename="{row.resume_name}"'})

@app.post('/api/outreach/{outreach_id}/schedule')
def reschedule(outreach_id: str, user: str = Depends(owner)):
    with session() as db:
        row = db.get(Outreach, outreach_id)
        if not row:
            raise HTTPException(404, 'Outreach not found')
        if row.status not in ('scheduled', 'demo_draft', 'followup_scheduled'):
            raise HTTPException(409, 'This outreach cannot be rescheduled')
        row.due_at = next_fixed_send(now())
        log(db, 'outreach_rescheduled', row.job_id, row.id, due_at=row.due_at)
        return json_outreach(row)

class SearchPromptUpdate(BaseModel):
    prompt: str = Field(min_length=100, max_length=24000)


@app.get('/api/search/prompt')
def get_search_prompt(user: str = Depends(owner)):
    from .agentic_search import master_prompt
    with session() as db:
        override = get_kv(db, 'scheduled_prompt_override')
    output_contract = (ROOT / 'config' / 'scheduled_chatgpt_csv_prompt.md').read_text(encoding='utf-8')
    prompt = override or (master_prompt() + '\n\n---\n\n'
        + 'IMPORTANT: The following Excel/CSV output contract overrides the presentation '
          'format in the master prompt above. Use the candidate profile and research '
          'standards in the master prompt; produce only the structured spreadsheet/CSV.\n\n'
        + output_contract)
    return {'prompt': prompt, 'editable': True, 'source': 'master research prompt + Excel/CSV output contract'}


@app.put('/api/search/prompt')
def update_search_prompt(data: SearchPromptUpdate, user: str = Depends(owner)):
    with session() as db:
        set_kv(db, 'scheduled_prompt_override', data.prompt.strip())
        log(db, 'search_prompt_updated', length=len(data.prompt))
    return {'saved': True, 'characters': len(data.prompt)}


@app.get('/api/cron/tick')
def cron_tick(request: Request):
    secret = env('CRON_SECRET')
    if not secret or not hmac.compare_digest(request.headers.get('authorization', ''), f'Bearer {secret}'):
        raise HTTPException(401, 'Invalid scheduler credential')
    if demo_mode():
        return {'status': 'demo_mode', 'note': 'No discovery, sending or Gmail monitoring in demo'}
    with session() as db:
        # Reply monitoring FIRST so we do not follow up after a reply.
        replies = monitor_replies(db)
        db.flush()
        sending = send_due(db)
        return {'replies': replies, 'sending': sending, 'discovery': 'disabled; ChatGPT research only'}


