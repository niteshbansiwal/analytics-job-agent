from __future__ import annotations
import base64
import hmac
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .config import ROOT, env, settings, demo_mode
from .db import session, engine, Job, Contact, Outreach, Integration, Event, get_kv, set_kv, seed_demo, log, now
from .calendar_rules import next_fixed_send, daily_search_due
from .integrations import (google_oauth_url, google_code_exchange, google_userinfo, save_google_refresh,
    send_telegram, answer_telegram_callback, rocketreach_lookup)
from .discovery import discover, research_contacts
from .workflow import approve_job, prepare_outreach, cancel_outreach, monitor_replies, send_due

app = FastAPI(title='Analytics Job Agent', version='0.1.0', docs_url=None, redoc_url=None)
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

@app.get('/')
def index():
    return FileResponse(ROOT / 'public' / 'index.html', media_type='text/html')

@app.get('/api/health')
def health():
    return {'ok': True, 'demo': demo_mode(), 'version': '0.1.0'}

@app.get('/api/auth/google/start')
def google_auth_start():
    if not all([env('GOOGLE_CLIENT_ID'), env('GOOGLE_CLIENT_SECRET'), env('APP_BASE_URL'), env('ALLOWED_GOOGLE_EMAIL')]):
        raise HTTPException(400, 'Google OAuth is not configured; see .env.example')
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(google_oauth_url(state), status_code=302)
    response.set_cookie('oauth_state', state, httponly=True, secure=is_https(), samesite='lax', max_age=600)
    return response

@app.get('/api/auth/google/callback')
def google_auth_callback(request: Request, code: str = '', state: str = ''):
    expected = request.cookies.get('oauth_state', '')
    if not expected or not hmac.compare_digest(expected, state) or not code:
        raise HTTPException(400, 'OAuth state validation failed')
    if demo_mode():
        raise HTTPException(400, 'Disable DEMO_MODE before connecting live Gmail')
    try:
        result = google_code_exchange(code)
        account = google_userinfo(result['access_token'])
        email = account.get('email', '').lower()
        if not account.get('email_verified') or email != env('ALLOWED_GOOGLE_EMAIL').lower():
            raise HTTPException(403, 'This Google account is not the configured app owner')
        with session() as db:
            refresh = result.get('refresh_token')
            if not refresh and not db.get(Integration, 'gmail'):
                raise HTTPException(400, 'Google did not issue a refresh token. Reconnect with consent.')
            if refresh:
                save_google_refresh(db, email, refresh)
            log(db, 'gmail_connected', owner=email)
        response = RedirectResponse('/?connected=gmail', status_code=302)
        response.set_cookie('job_agent_session', create_session(email), httponly=True,
                            secure=is_https(), samesite='lax', max_age=7*86400)
        response.delete_cookie('oauth_state')
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

@app.post('/api/discover')
def discover_now(user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Demo never imports real jobs; switch to live mode and configure Serper')
    with session() as db:
        try:
            result = discover(db)
        except RuntimeError as e:
            raise HTTPException(422, str(e))
        return result

@app.post('/api/jobs/{job_id}/research')
def research_now(job_id: str, user: str = Depends(owner)):
    with session() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Job not found')
        if demo_mode():
            raise HTTPException(409, 'Discovery disabled in demo')
        contacts = research_contacts(db, job, limit=2)
        return {'contacts_found': len(contacts)}

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
        last_day = get_kv(db, 'last_search_ist_date')
        search = {'skipped': True}
        if daily_search_due(now(), last_day):
            try:
                search = discover(db)
                if not search['errors'] or search['created']:
                    today = now().astimezone(ZoneInfo(settings()['timezone'])).date().isoformat()
                    set_kv(db, 'last_search_ist_date', today)
            except Exception as exc:
                search = {'error': str(exc)[:200]}
        return {'replies': replies, 'sending': sending, 'discovery': search}

@app.post('/api/telegram/webhook')
async def telegram_webhook(request: Request):
    secret = env('TELEGRAM_WEBHOOK_SECRET')
    if not secret or not hmac.compare_digest(request.headers.get('x-telegram-bot-api-secret-token', ''), secret):
        raise HTTPException(401, 'Invalid Telegram webhook signature')
    update = await request.json()
    callback = update.get('callback_query') or {}
    data = callback.get('data') or ''
    chat_id = str((callback.get('message') or {}).get('chat', {}).get('id', ''))
    if chat_id != env('TELEGRAM_CHAT_ID'):
        return {'ok': False, 'reason': 'Unrecognized Telegram chat'}
    if data.startswith('approve:') or data.startswith('reject:'):
        action, job_id = data.split(':', 1)
        if not re_uuid(job_id):
            return {'ok': False}
        result = ''
        with session() as db:
            job = db.get(Job, job_id)
            if not job or job.is_demo:
                result = 'Job not found'
            elif action == 'approve':
                try:
                    row = approve_job(db, job_id)
                    result = f'Approved: {row.status}'
                except ValueError as exc:
                    result = str(exc)
            elif action == 'reject':
                if db.execute(select(Outreach).where(Outreach.job_id == job_id)).scalar_one_or_none():
                    result = 'Already approved; cancel in dashboard'
                else:
                    job.status = 'rejected'
                    log(db, 'job_rejected', job_id, via='telegram')
                    result = 'Rejected'
        answer_telegram_callback(callback.get('id', ''), result)
        return {'ok': True, 'message': result}
    return {'ok': True}

def re_uuid(v: str) -> bool:
    import uuid
    try:
        uuid.UUID(v)
        return True
    except Exception:
        return False
