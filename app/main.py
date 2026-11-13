from __future__ import annotations
import httpx
import base64
import hmac
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Depends, Request, Response, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, ConfigDict
from typing import Literal
from .resume_library import list_resumes, get_resume, save_resume, MAX_BYTES, digest
from sqlalchemy import select, func
from sqlalchemy.orm import Session, defer
from .config import ROOT, env, settings, demo_mode
from .db import session, engine, Job, Contact, Outreach, Integration, Event, get_kv, set_kv, seed_demo, log, now
from .calendar_rules import next_fixed_send
from .scheduling import holidays, schedule_slot, save_holidays, manual_schedule_slot
from .integrations import (google_oauth_url, google_code_exchange, google_userinfo, save_google_refresh,
    rocketreach_lookup, rocketreach_check_status)
from .workflow import approve_job, prepare_outreach, cancel_outreach, monitor_replies, send_due

app = FastAPI(title='Analytics Job Agent', version='0.1.0', docs_url=None, redoc_url=None)
if (ROOT / 'public').is_dir():
    app.mount('/assets', StaticFiles(directory=str(ROOT / 'public')), name='assets')

@app.middleware('http')
async def read_timing(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.method == 'GET' and request.url.path.startswith('/api/'):
        response.headers['Server-Timing'] = f'app;dur={(time.perf_counter()-started)*1000:.1f}'
    return response


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"

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

def json_job(job: Job, db: Session, contacts=None, outreach=None, rr_states=None):
    if contacts is None: contacts = db.execute(select(Contact).where(Contact.job_id == job.id)).scalars().all()
    if outreach is None: outreach = db.execute(select(Outreach).options(defer(Outreach.resume_b64)).where(Outreach.job_id == job.id).order_by(Outreach.created_at.desc()).limit(1)).scalars().first()
    rr_states = rr_states or {}
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
         'rocketreach_status': rr_states.get(c.id, {}).get('status', 'not_started'),
         'rocketreach_next_check_at': rr_states.get(c.id, {}).get('next_check_at', ''),
         'rocketreach_lookup_count': rr_states.get(c.id, {}).get('lookup_count', 0),
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
    return FileResponse(ROOT / 'public' / 'welcome.html', media_type='text/html', headers={'Cache-Control': 'no-store'})

@app.get('/app')
def workspace():
    if os.getenv('VERCEL'):
        return RedirectResponse('/index.html', status_code=307)
    return FileResponse(ROOT / 'public' / 'index.html', media_type='text/html', headers={'Cache-Control': 'no-store'})

@app.get('/api/health')
def health():
    return {'ok': True, 'demo': demo_mode(), 'version': '0.2.0',
            'commit': env('VERCEL_GIT_COMMIT_SHA', 'local'), 'resume_mode': 'fixed_pdf',
            'email_url_policy': 'preserve_all', 'rocketreach_state': 'shared'}

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
def overview(user: str = Depends(owner), section: str = '', page: int = 1, search: str = '', status: str = 'all'):
    if os.getenv('VERCEL') and not env('DATABASE_URL'):
        raise HTTPException(503, 'PostgreSQL DATABASE_URL is not configured yet. See the homepage setup checklist.')
    with session() as db:
        seed_demo(db)
        db.flush()
        from .read_model import totals, revision
        from collections import defaultdict
        from .db import KV
        overview_keys = ['atlas_auto_import_enabled', 'scheduler_interval_minutes', 'scheduler_last_result', 'scheduler_last_tick', 'scheduler_reply_last_result', 'scheduler_reply_last_run', 'scheduler_reply_last_success', 'scheduler_send_last_result', 'scheduler_send_last_run', 'scheduler_send_last_success', 'email_holidays']
        cached = dict(db.execute(select(KV.key,KV.value).where(KV.key.in_(overview_keys))).all())
        db.info['read_kv'] = {key:cached.get(key) for key in overview_keys}
        compact = bool(section)
        needs_jobs = not compact or section in ('jobs','overview')
        page = max(1, page)
        job_query = select(Job).where(Job.status != 'archived')
        if search:
            pattern = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            job_query = job_query.where((Job.company + ' ' + Job.title + ' ' + Job.location).ilike(pattern, escape='\\'))
        job_status = {'new': 'discovered', 'approved': 'approved', 'replies': 'reply_received'}.get(status)
        if job_status: job_query = job_query.where(Job.status == job_status)
        total = (db.scalar(select(func.count()).select_from(job_query.subquery())) or 0) if needs_jobs else 0
        page = min(page, max(1, (total + 19)//20))
        if compact:
            jobs = db.execute(job_query.order_by(Job.created_at.desc(), Job.id.desc()).offset((page-1)*20).limit(20)).scalars().all() if section in ('overview', 'jobs') else []
        else:
            jobs = db.execute(select(Job).order_by(Job.created_at.desc(), Job.id.desc())).scalars().all()
        ids = [j.id for j in jobs]
        letters = db.execute(select(Outreach).options(defer(Outreach.resume_b64)).where(Outreach.job_id.in_(ids))
            .order_by(Outreach.created_at.desc())).scalars().all() if ids else []
        contact_groups = defaultdict(list)
        contacts_all = db.execute(select(Contact).where(Contact.job_id.in_(ids))).scalars().all() if ids else []
        for c in contacts_all: contact_groups[c.job_id].append(c)
        outreach_latest = {}
        for o in letters: outreach_latest.setdefault(o.job_id, o)
        rr_states = {}
        if contacts_all:
            keys = ['rocketreach:' + c.id for c in contacts_all]
            for kv in db.execute(select(KV).where(KV.key.in_(keys))).scalars():
                try: rr_states[kv.key.split(':',1)[1]] = json.loads(kv.value)
                except (ValueError, TypeError): pass
        events = db.execute(select(Event).order_by(Event.created_at.desc()).limit(35)).scalars().all() if not compact or section == 'activity' else []
        integration = db.get(Integration, 'gmail')
        cfg = settings()
        return {
            'demo': demo_mode(),
            'resumes': list_resumes(db),
            'jobs': [json_job(j, db, contact_groups[j.id], outreach_latest.get(j.id, False), rr_states) for j in jobs],
            'totals': totals(db), 'revision': revision(db),
            'jobs_page': {'page': page, 'total': total, 'page_size': 20},
            'outreach': [json_outreach(o) for o in letters],
            'events': [{'action': e.action, 'details': json.loads(e.details or '{}'),
                        'at': e.created_at.isoformat(), 'job_id': e.job_id} for e in events],
            'integrations': {
               'gmail': bool(integration) and not demo_mode(),
               'telegram': bool(env('TELEGRAM_BOT_TOKEN') and env('TELEGRAM_CHAT_ID')),
               'rocketreach': bool(env('ROCKETREACH_API_KEY')),
                'gemini': bool(env('GEMINI_API_KEY')),
                'gemini_model': env('GEMINI_MODEL', 'gemini-3.5-flash-lite'),
               'serper': bool(env('SERPER_API_KEY')),
               'openai': bool(env('OPENAI_API_KEY')),
               'database': 'SQLite / local' if db.bind.dialect.name == 'sqlite' else 'PostgreSQL',
            },
            'settings': {
                'timezone': cfg['timezone'], 'search_time': cfg['search']['at'],
                'search_weekdays': cfg['search']['weekdays'], 'lookback_days': cfg['search']['lookback_days'],
                'max_jobs': cfg['search']['shortlist_size'], 'send_start': cfg['email']['start'],
                'send_end': cfg['email']['end'], 'send_time': cfg['email']['preferred_send_time'],
                'send_enabled': True,
                'scheduler_ready': bool(env('CRON_SECRET')),
                'scheduler_interval_minutes': int(get_kv(db, 'scheduler_interval_minutes', '5')),
                'scheduler_last_tick': get_kv(db, 'scheduler_last_tick', ''),
                'scheduler_send_last_run': get_kv(db, 'scheduler_send_last_run', ''),
                'scheduler_send_last_success': get_kv(db, 'scheduler_send_last_success', ''),
                'scheduler_send_last_result': get_kv(db, 'scheduler_send_last_result', ''),
                'scheduler_reply_last_run': get_kv(db, 'scheduler_reply_last_run', ''),
                'scheduler_reply_last_success': get_kv(db, 'scheduler_reply_last_success', ''),
                'scheduler_reply_last_result': get_kv(db, 'scheduler_reply_last_result', ''),
                'scheduler_cadence_minutes': 15,
                'scheduler_provider': 'Vercel Schedules (registration unverified)',
                'scheduler_last_result': get_kv(db, 'scheduler_last_result', ''),
                'auto_import_enabled': get_kv(db, 'atlas_auto_import_enabled', '') == 'true',
                'holidays': holidays(db),
                'followup_enabled': cfg['email']['followup']['enabled'],
                'followup_hours': cfg['email']['followup']['hours'],
                'followup_rule': (f"{_ordinal(int(cfg['email']['followup']['days']))} working day after the previous email"
                                  if cfg['email']['followup'].get('delay_type') == 'working_days'
                                  else f"{cfg['email']['followup']['hours']} elapsed hours"),
                'max_followups': cfg['email']['followup']['max_attempts'],
            },
        }

@app.get('/api/resumes')
def resumes_status(user: str = Depends(owner)):
    with session() as db:
        return {'resumes': list_resumes(db)}


@app.post('/api/resumes/{resume_id}')
async def upload_fixed_resume(resume_id: Literal['analytics', 'data_science'],
                              file: UploadFile = File(...),
                              expected_sha256: str = Form(''),
                              user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Resume upload is available only in live mode.')
    raw = await file.read(MAX_BYTES + 1)
    await file.close()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, 'Resume PDF must be smaller than 4 MB.')
    with session() as db:
        try:
            result = save_resume(db, resume_id, raw, expected_sha256=expected_sha256)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        log(db, 'fixed_resume_uploaded', resume_id=resume_id, sha256=result['sha256'])
        return result


@app.post('/api/integrations/gemini/test')
def test_gemini_api(user: str = Depends(owner)):
    """Owner-triggered live API smoke test. No job or personal data is transmitted."""
    if demo_mode():
        raise HTTPException(409, 'Gemini live test is disabled in demo mode.')
    from .gemini import GeminiError, test_gemini_connection
    try:
        return test_gemini_connection()
    except GeminiError as exc:
        raise HTTPException(502, str(exc))


@app.post('/api/jobs/{job_id}/gemini-preview')
def gemini_outreach_preview(job_id: str, user: str = Depends(owner), contact_id: str = '', resume_id: str = ''):
    """Fact-locked, review-only preview. Does NOT create a Gmail draft or send email."""
    if demo_mode():
        raise HTTPException(409, 'Gemini preview is disabled in demo mode.')
    from .gemini import build_email_preview, GeminiError
    with session() as db:
        job = db.get(Job, job_id)
        if not job or job.is_demo:
            raise HTTPException(404, 'Imported job not found.')
        available = [r for r in list_resumes(db) if r['configured']]
        if not available:
            raise HTTPException(409, 'Upload your Analytics or Data Science PDF in Configuration first.')
        contacts = db.execute(select(Contact).where(Contact.job_id == job.id)).scalars().all()
        contact = next((c for c in contacts if c.id == contact_id), None) if contact_id else next((c for c in contacts if c.email), None)
        if not contact:
            raise HTTPException(422, 'Choose a hiring contact before generating email')
        name = contact.name
        from .email_policy import load_settings
        controls = load_settings(db)
        if resume_id:
            if resume_id not in ('analytics','data_science'):
                raise HTTPException(422, 'Choose an uploaded resume version')
            controls['resume_id'] = resume_id
        try:
            resume_item, _ = get_resume(db, controls['resume_id'])
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        email_prompt = get_kv(db, 'email_generation_prompt')
        try:
            result = build_email_preview(job.company, job.title, job.jd or '', name, email_prompt, job.post_url, controls=controls)
        except GeminiError as exc:
            log(db, 'gemini_email_preview_failed', job.id, reason=str(exc)[:180],
                custom_prompt_configured=bool(email_prompt))
            db.commit()
            raise HTTPException(502, 'Email generation stopped: ' + str(exc)) from exc
        result['resumes'] = available
        result['contact_id'] = contact.id
        result['resume_sha256'] = resume_item['sha256']
        log(db, 'gemini_email_preview', job.id,
            mode=result['mode'], fact_ids=result['selected_fact_ids'],
            contact_id=contact.id, repair_attempts=result['repair_attempts'], validation_status='passed',
            resume_id=controls['resume_id'], resume_sha256=resume_item['sha256'],
            prompt_source=result['prompt_source'],
            prompt_fingerprint=result['prompt_fingerprint'],
            draft_created=False, email_sent=False)
        return result


class ResumePreviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resume_id: Literal['analytics', 'data_science']
    resume_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def selected_pdf(db, data):
    try:
        item, raw = get_resume(db, data.resume_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if item['sha256'] != data.resume_sha256:
        raise HTTPException(409, 'Resume changed. Reopen the preview and review the current PDF.')
    return item, raw


@app.post('/api/jobs/{job_id}/reviewed-resume.pdf')
def reviewed_resume_pdf(job_id: str, data: ResumePreviewInput,
                        user: str = Depends(owner)):
    with session() as db:
        job = db.get(Job, job_id)
        if not job or job.is_demo:
            raise HTTPException(404, 'Imported job not found.')
        item, raw = selected_pdf(db, data)
        return Response(raw, media_type='application/pdf', headers={
            'Content-Disposition': f'inline; filename="{item["filename"]}"',
            'Cache-Control': 'no-store', 'X-Resume-SHA256': item['sha256']})


class ReviewedDraftInput(ResumePreviewInput):
    contact_id: str = Field(min_length=1, max_length=36)
    recipient_email: str = Field(min_length=5, max_length=200)
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=30, max_length=8000)
    reviewed: bool = False
    replace_existing_draft: bool = False


@app.post('/api/jobs/{job_id}/reviewed-draft')
def create_reviewed_draft(job_id: str, data: ReviewedDraftInput,
                          user: str = Depends(owner)):
    """Owner-confirmed Gmail draft + verified PDF. Never sends or schedules."""
    if demo_mode():
        raise HTTPException(409, 'Real Gmail drafts are unavailable in demo mode.')
    if not data.reviewed:
        raise HTTPException(422, 'Review and confirm the email, recipient and resume first.')
    if ('\n' in data.subject or '\r' in data.subject
            or not data.subject.strip() or not data.body.strip()):
        raise HTTPException(422, 'Subject or email body is invalid.')
    from .integrations import (gmail_create_draft, gmail_update_draft,
                               gmail_get_draft, inspect_gmail_draft,
                               gmail_delete_draft)
    with session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        if not job or job.is_demo:
            raise HTTPException(404, 'Imported job not found.')
        if job.status == 'rejected':
            raise HTTPException(409, 'Rejected job: reopen it before preparing outreach.')
        if not job.post_url.startswith('https://'):
            raise HTTPException(409, 'Review and add a real HTTPS job source before outreach.')
        if not db.get(Integration, 'gmail'):
            raise HTTPException(409, 'Gmail is not connected. Connect it in Configuration.')
        contact = db.get(Contact, data.contact_id)
        if not contact or contact.job_id != job.id or not contact.email:
            raise HTTPException(422, 'Select a contact with a professional email for this job.')
        if (contact.email.strip().lower() != data.recipient_email.strip().lower()
                or not re.fullmatch(r'[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+', contact.email)):
            raise HTTPException(409, 'Contact email changed. Refresh and review the recipient.')
        from .email_policy import validate_final, EmailValidationError
        try:
            validate_final(data.subject, data.body, contact.name, job.post_url)
            if 'EXACTLY TWO NUMBERED BULLETS' in get_kv(db, 'email_generation_prompt', ''):
                from .email_policy import require_v2_format
                require_v2_format(data.body)
        except EmailValidationError as exc:
            raise HTTPException(422, str(exc))
        row = db.execute(select(Outreach).where(Outreach.job_id == job.id, Outreach.contact_id == contact.id)).scalar_one_or_none()
        if row and row.status in ('sent', 'sending', 'awaiting_reply',
                                  'followup_scheduled', 'responded', 'finished'):
            raise HTTPException(409, 'This job already has sent or active outreach; no duplicate draft created.')
        if row and row.gmail_draft_id and not data.replace_existing_draft:
            raise HTTPException(409, 'Existing Gmail draft: confirm replacement before overwriting its edits.')
        previous_due = row.due_at if row and row.status == 'scheduled' and row.due_at and row.due_at.replace(tzinfo=timezone.utc) > now() else None
        item, pdf = selected_pdf(db, data)
        filename = item['filename']
        draft_id = row.gmail_draft_id if row else ''
        created_new = not bool(draft_id)
        try:
            if draft_id:
                draft = gmail_update_draft(db, draft_id, contact.email, data.subject,
                                           data.body, pdf, filename)
            else:
                draft = gmail_create_draft(db, contact.email, data.subject,
                                           data.body, pdf, filename)
            verified_id = draft.get('id', '')
            if not verified_id or (draft_id and verified_id != draft_id):
                raise RuntimeError('Gmail returned an unexpected draft identifier.')
            draft_id = verified_id
            verified = inspect_gmail_draft(gmail_get_draft(db, draft_id))
            if (verified['to'].lower() != contact.email.lower()
                    or job.post_url not in verified.get('html_links', [])
                    or verified['subject'] != data.subject
                    or verified['attachment_count'] != 1
                    or verified['attachment_filenames'] != [filename]
                    or verified['attachment_sha256'] != [digest(pdf)]
                    or verified['body'].strip().replace('\r\n', '\n')
                       != data.body.strip().replace('\r\n', '\n')):
                raise RuntimeError('Gmail draft verification mismatch; review in Gmail.')
        except Exception as exc:
            if created_new and draft_id:
                try:
                    gmail_delete_draft(db, draft_id)
                except Exception:
                    pass
            detail = (f'Gmail API returned HTTP {exc.response.status_code}'
                      if isinstance(exc, httpx.HTTPStatusError)
                      else type(exc).__name__)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
                try:
                    provider = exc.response.json().get('error', {})
                    reason = next((e.get('reason', '') for e in provider.get('errors', []) if e.get('reason')), '')
                except (ValueError, TypeError, AttributeError):
                    reason = ''
                if reason in ('insufficientPermissions', 'ACCESS_TOKEN_SCOPE_INSUFFICIENT'):
                    detail += ': Gmail draft permission is missing. Reconnect Gmail in Configuration and approve the requested scopes'
                elif reason in ('accessNotConfigured', 'SERVICE_DISABLED'):
                    detail += ': Gmail API is disabled for the Google Cloud project; enable it and reconnect Gmail'
                else:
                    detail += ': Gmail denied draft access. Reconnect Gmail in Configuration; if it persists, check Gmail API access in Google Cloud'
            raise HTTPException(502, f'Gmail draft creation/verification failed: {detail}. No email sent.')
        if not row:
            row = Outreach(job_id=job.id)
            db.add(row)
        row.contact_id = contact.id
        row.to_email = contact.email
        row.subject = data.subject
        row.body = data.body
        row.resume_b64 = base64.b64encode(pdf).decode()
        row.resume_name = filename
        row.gmail_draft_id = draft_id
        row.status = 'scheduled' if previous_due else 'draft_ready'
        row.due_at = previous_due
        row.error = ''
        job.status = 'approved'
        db.flush()
        log(db, 'reviewed_gmail_draft_verified', job.id, row.id,
            gmail_draft_id=draft_id, recipient=contact.email,
            resume_id=data.resume_id, resume_sha256=item['sha256'], sent=False)
        return {'verified': True, 'sent': False, 'scheduled': bool(previous_due),
                'draft_id': draft_id, 'resume_filename': filename,
                'recipient_email': contact.email,
                'gmail_url': 'https://mail.google.com/mail/u/0/#drafts',
                'outreach': json_outreach(row)}

class ScheduleInput(BaseModel):
    due_at: datetime

@app.put('/api/outreach/{outreach_id}/schedule')
def schedule_reviewed(outreach_id: str, data: ScheduleInput, user: str = Depends(owner)):
    with session() as db:
        row = db.get(Outreach, outreach_id, with_for_update=True)
        if not row or row.status not in ('draft_ready', 'scheduled') or not row.gmail_draft_id:
            raise HTTPException(409, 'Only a verified, unsent Gmail draft can be scheduled or edited.')
        if not db.get(Integration, 'gmail'):
            raise HTTPException(409, 'Gmail is disconnected.')
        try:
            due = manual_schedule_slot(data.due_at)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        row.due_at = due
        row.status = 'scheduled'
        set_kv(db, 'manual_schedule:' + row.id, 'true')
        log(db, 'reviewed_email_scheduled', row.job_id, row.id, due_at=due, mode='manual')
        return {'outreach': json_outreach(row), 'scheduled': True}

@app.post('/api/outreach/{outreach_id}/unschedule')
def unschedule_reviewed(outreach_id: str, user: str = Depends(owner)):
    with session() as db:
        row = db.get(Outreach, outreach_id, with_for_update=True)
        if not row or row.status != 'scheduled':
            raise HTTPException(409, 'This item is not scheduled.')
        row.status, row.due_at = 'draft_ready', None
        log(db, 'reviewed_email_unscheduled', row.job_id, row.id)
        return {'outreach': json_outreach(row)}

@app.post('/api/email/replies/check-now')
def check_replies_now(user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Reply monitoring unavailable in demo mode')
    with session() as db:
        result = monitor_replies(db)
        db.commit()
        return {'replies': result}


@app.post('/api/integrations/telegram/test')
def test_telegram_notification(user: str = Depends(owner)):
    from .integrations import send_telegram
    if not env('TELEGRAM_BOT_TOKEN') or not env('TELEGRAM_CHAT_ID'):
        raise HTTPException(409, 'Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Vercel Production environment, then redeploy.')
    try:
        result = send_telegram('Atlas test notification: Telegram is connected and ready for reply alerts.')
    except Exception as exc:
        raise HTTPException(502, 'Telegram test failed: ' + str(exc)[:250])
    return {'sent': bool(result.get('sent'))}


class SchedulerConfigInput(BaseModel):
    interval_minutes: Literal[5, 10, 15, 30, 60]


@app.put('/api/email/scheduler')
def update_scheduler_config(data: SchedulerConfigInput, user: str = Depends(owner)):
    with session() as db:
        set_kv(db, 'scheduler_interval_minutes', str(data.interval_minutes))
        log(db, 'scheduler_interval_updated', minutes=data.interval_minutes)
        return {'interval_minutes': data.interval_minutes, 'base_worker_minutes': 5}


@app.post('/api/email/scheduler/check-now')
def check_scheduler_now(user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Sending unavailable in demo mode')
    with session() as db:
        result = send_due(db)
        log(db, 'manual_scheduler_check', **result)
        return {'sending': result}


class HolidaysInput(BaseModel):
    dates: list[str] = Field(max_length=200)

@app.put('/api/email/holidays')
def update_email_holidays(data: HolidaysInput, user: str = Depends(owner)):
    with session() as db:
        try:
            days, shifted = save_holidays(db, data.dates)
        except ValueError as exc:
            raise HTTPException(422, f'Invalid holiday date: {exc}')
        log(db, 'email_holidays_updated', count=len(days), shifted=len(shifted))
        return {'holidays': days, 'rescheduled_ids': shifted}


@app.delete('/api/jobs/{job_id}')
def delete_imported_job(job_id: str, user: str = Depends(owner)):
    """Delete an unsent job and its dependent rows; archive any sent history."""
    if demo_mode():
        raise HTTPException(409, 'Deletion disabled in demo mode')
    from sqlalchemy import delete
    from .db import KV
    try:
        with session() as db:
            job = db.get(Job, job_id)
            if job is None or job.is_demo:
                raise HTTPException(404, 'Imported job not found')
            rows = db.execute(select(Outreach).where(Outreach.job_id == job_id)).scalars().all()
            sent = any(row.sent_at or row.sent_message_id or row.gmail_thread_id or row.status in
                       ('sent', 'sending', 'awaiting_reply', 'followup_scheduled', 'responded', 'finished')
                       for row in rows)
            if sent:
                job.status = 'archived'
                log(db, 'job_archived_preserving_sent_history', job_id, outreach_count=len(rows))
                db.flush()
                return {'deleted': False, 'archived': True, 'job_id': job_id}

            # Do not call the external Gmail API inside the database transaction:
            # a deleted/expired Gmail draft must never block deleting an unsent job.
            draft_ids = [row.gmail_draft_id for row in rows if row.gmail_draft_id]
            contact_ids = db.execute(select(Contact.id).where(Contact.job_id == job_id)).scalars().all()
            company, title = job.company, job.title
            db.execute(delete(Outreach).where(Outreach.job_id == job_id))
            db.execute(delete(Contact).where(Contact.job_id == job_id))
            for contact_id in contact_ids:
                db.execute(delete(KV).where(KV.key == 'auto_rr:' + contact_id))
            db.execute(delete(Job).where(Job.id == job_id))
            log(db, 'imported_job_deleted', job_id, company=company, title=title,
                contacts=len(contact_ids), unsent_outreach=len(rows))
            db.flush()

        # Cleanup is best-effort AFTER the DB transaction has committed.
        warnings = []
        if draft_ids:
            from .integrations import gmail_delete_draft
            for draft_id in draft_ids:
                try:
                    with session() as cleanup_db:
                        gmail_delete_draft(cleanup_db, draft_id)
                except Exception as exc:
                    if '404' not in str(exc) and 'not found' not in str(exc).lower():
                        warnings.append('Could not delete a Gmail draft; remove it manually in Gmail.')
        return {'deleted': True, 'archived': False, 'job_id': job_id, 'warnings': warnings}
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.exception('Atlas job deletion failed for %s', job_id)
        # Show a safe diagnostic rather than an opaque 500; no credentials or SQL data.
        raise HTTPException(409, 'Job deletion failed (' + type(exc).__name__ +
                            '). Check Vercel runtime logs for Atlas job deletion failed.')


@app.get('/api/outreach/dashboard')
def outreach_dashboard(user: str = Depends(owner)):
    with session() as db:
        rows = db.execute(select(Outreach).join(Job, Outreach.job_id == Job.id)
                          .order_by(Job.created_at.desc(), Outreach.created_at.desc(), Outreach.id.desc())).scalars().all()
        jobs = {j.id: j for j in db.execute(select(Job)).scalars().all()}
        contacts = {c.id: c for c in db.execute(select(Contact)).scalars().all()}
        items = []
        for row in rows:
            job = jobs.get(row.job_id)
            contact = contacts.get(row.contact_id)
            replied = bool(row.last_reply_at)
            read = get_kv(db, 'reply_read:' + row.id, '') == row.last_reply_message_id if replied else False
            action_done = get_kv(db, 'reply_action_done:' + row.id, '') == row.last_reply_message_id if replied else False
            items.append({'id': row.id, 'job_id': row.job_id, 'company': job.company if job else '',
                'title': job.title if job else '', 'contact_name': contact.name if contact else '',
                'to_email': row.to_email, 'status': row.status,
                'sent_at': row.sent_at.isoformat() if row.sent_at else None,
                'last_reply_at': row.last_reply_at.isoformat() if row.last_reply_at else None,
                'last_reply_class': row.last_reply_class, 'gmail_thread_id': row.gmail_thread_id,
                'reply_read': read, 'action_done': action_done})
        sent = [x for x in items if x['sent_at']]
        replied = [x for x in items if x['last_reply_at']]
        return {'summary': {'sent': len(sent), 'replied': len(replied),
            'unread': sum(not x['reply_read'] for x in replied),
            'action_pending': sum(not x['action_done'] and x['last_reply_class'] not in ('bounce', 'out_of_office') for x in replied)},
            'items': items}


class ReplyActionInput(BaseModel):
    value: bool = True


@app.put('/api/outreach/{outreach_id}/reply-read')
def mark_reply_read(outreach_id: str, data: ReplyActionInput, user: str = Depends(owner)):
    with session() as db:
        row = db.get(Outreach, outreach_id)
        if not row or not row.last_reply_message_id:
            raise HTTPException(404, 'No tracked reply for this outreach')
        set_kv(db, 'reply_read:' + row.id, row.last_reply_message_id if data.value else '')
        log(db, 'reply_read_updated', row.job_id, row.id, read=data.value)
        return {'ok': True}


@app.put('/api/outreach/{outreach_id}/action-done')
def mark_reply_action_done(outreach_id: str, data: ReplyActionInput, user: str = Depends(owner)):
    with session() as db:
        row = db.get(Outreach, outreach_id)
        if not row or not row.last_reply_message_id:
            raise HTTPException(404, 'No tracked reply for this outreach')
        set_kv(db, 'reply_action_done:' + row.id, row.last_reply_message_id if data.value else '')
        log(db, 'reply_action_updated', row.job_id, row.id, done=data.value)
        return {'ok': True}


@app.get('/api/imports/{batch_id}/progress')
def import_batch_progress(batch_id: str, user: str = Depends(owner)):
    """Read one persisted upload batch; never start or stop background processing."""
    if not re.fullmatch(r'[0-9a-f]{32}', batch_id):
        raise HTTPException(404, 'Upload batch not found')
    with session() as db:
        raw = get_kv(db, 'import_batch:' + batch_id, '')
        if not raw:
            raise HTTPException(404, 'Upload batch not found')
        batch = json.loads(raw)
        ids = list(dict.fromkeys(batch.get('job_ids', [])))[:250]
        scope = batch.get('contacts_by_job', {})
        if not ids:
            return {'batch_id': batch_id, 'created_at': batch['created_at'],
                    'filename': batch['filename'], 'summary': batch['summary'],
                    'jobs': [], 'events': [], 'status': 'nothing_queued'}
        jobs = db.execute(select(Job).where(Job.id.in_(ids))).scalars().all()
        jobs_by_id = {j.id: j for j in jobs}
        allowed = list(dict.fromkeys(cid for jid in ids for cid in scope.get(jid, [])))
        contacts = db.execute(select(Contact).where(Contact.id.in_(allowed))).scalars().all() if allowed else []
        by_id = {c.id: c for c in contacts}
        rows = db.execute(select(Outreach).where(Outreach.contact_id.in_(allowed))
                          .order_by(Outreach.created_at.desc())).scalars().all() if allowed else []
        by_contact = {}
        for row in rows:
            by_contact.setdefault(row.contact_id, row)
        outreach_ids = [row.id for row in by_contact.values()]
        events = db.execute(select(Event).where(
            Event.outreach_id.in_(outreach_ids),
            Event.created_at >= datetime.fromisoformat(batch['created_at']))
            .order_by(Event.created_at.desc()).limit(1500)).scalars().all() if outreach_ids else []
        items = []
        for jid in ids:
            job = jobs_by_id.get(jid)
            if not job:
                continue
            people = []
            for cid in scope.get(jid, []):
                contact = by_id.get(cid)
                if not contact:
                    continue
                row = by_contact.get(cid)
                people.append({
                    'id': cid, 'name': contact.name, 'email': contact.email,
                    'email_status': contact.email_status,
                    'has_linkedin': bool(contact.linkedin_url),
                    'outreach_id': row.id if row else '',
                    'status': row.status if row else 'not_queued',
                    'due_at': row.due_at.isoformat() if row and row.due_at else None,
                    'sent_at': row.sent_at.isoformat() if row and row.sent_at else None,
                    'draft_created': bool(row and row.gmail_draft_id),
                    'error': row.error if row else '',
                })
            items.append({'id': job.id, 'company': job.company,
                          'title': job.title, 'contacts': people})
        return {
            'batch_id': batch_id, 'created_at': batch['created_at'],
            'filename': batch['filename'], 'summary': batch['summary'],
            'jobs': items,
            'events': [{'id': e.id, 'job_id': e.job_id, 'outreach_id': e.outreach_id,
                        'action': e.action, 'details': json.loads(e.details or '{}'),
                        'at': e.created_at.isoformat()} for e in events],
            'status': 'tracking',
        }


@app.get('/api/workspace/changes')
def workspace_changes(response: Response, user: str = Depends(owner)):
    from .read_model import revision
    response.headers['Cache-Control'] = 'private, no-store'
    with session() as db:
        return {'revision': revision(db)}


@app.get('/api/workspace/outreach')
def workspace_outreach(user: str = Depends(owner), page: int = 1, page_size: int = 25,
                       search: str = '', status: str = 'all'):
    from .read_model import outreach_page
    with session() as db:
        return outreach_page(db, max(1, page), min(100, max(1, page_size)), search[:200], status)


@app.get('/api/workspace/logs')
def workspace_logs(user: str = Depends(owner), page: int = 1, page_size: int = 25,
                   search: str = '', batch: str = '', start: str = '', end: str = '', event_type: str = '', job_id: str = ''):
    from .read_model import job_logs
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    try:
        def date_bound(value):
            return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=ZoneInfo('Asia/Kolkata')).astimezone(timezone.utc) if value else None
        lower, upper = date_bound(start), date_bound(end)
        if upper: upper += timedelta(days=1)
    except ValueError:
        raise HTTPException(422, 'Use YYYY-MM-DD for log date filters')
    with session() as db:
        return job_logs(db, max(1, page), min(100, max(1, page_size)), search[:200], batch[:32], lower, upper, event_type[:120], job_id[:36])


@app.get('/api/workspace/jobs/{job_id}/timeline')
def workspace_timeline(job_id: str, user: str = Depends(owner), page: int = 1):
    from .read_model import timeline
    with session() as db:
        if not db.get(Job, job_id): raise HTTPException(404, 'Job not found')
        return timeline(db, job_id, max(1, page))


@app.get('/api/workspace/outreach/{outreach_id}')
def workspace_outreach_detail(outreach_id: str, user: str = Depends(owner)):
    with session() as db:
        row = db.get(Outreach, outreach_id)
        if not row: raise HTTPException(404, 'Outreach not found')
        job = db.get(Job, row.job_id)
        return {'outreach': json_outreach(row), 'job': json_job(job, db)}


@app.get('/api/jobs/{job_id}')
def job_details(job_id: str, user: str = Depends(owner)):
    with session() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, 'Job not found')
        contacts = db.execute(select(Contact).where(Contact.job_id == job_id)).scalars().all()
        states = {c.id: _rocketreach_state(db, c.id) for c in contacts}
        return json_job(job, db, contacts=contacts, rr_states=states)

@app.post('/api/jobs/{job_id}/approve')
def approve(job_id: str, user: str = Depends(owner)):
    with session() as db:
        try:
            row = approve_job(db, job_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return json_outreach(row)

@app.post('/api/jobs/{job_id}/test-gmail-draft')
def retired_gmail_test(job_id: str, user: str = Depends(owner)):
    raise HTTPException(410, 'Custom resume tests have been retired. Use the reviewed fixed-PDF draft flow.')


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
    if data.email and not re.fullmatch(r'[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+', data.email):
        raise HTTPException(422, 'Enter a valid professional email address')
    from .linkedin_urls import linkedin_profile_url
    if data.linkedin_url and not linkedin_profile_url(data.linkedin_url):
        raise HTTPException(422, 'Provide a safe HTTPS LinkedIn URL; country-specific subdomains are supported.')
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
    check_count = int(previous.get('check_count') or 0) + 1 if is_check else 0
    lookup_count = int(previous.get('lookup_count') or 0) + (0 if is_check else 1)

    if status in ('pending', 'awaiting_email') and not provider_id:
        status = 'not_found' if status == 'awaiting_email' else 'api_error'
    if status == 'awaiting_email' and check_count >= 3:
        status = 'not_found'
    if status == 'pending' and check_count >= 5:
        status = 'pending_manual'
    if status == 'returned':
        contact.email = result['email']
        contact.email_status = result.get('email_status', 'rocketreach_unverified')
        log(db, 'contact_enrichment_completed', contact.job_id, contact_id=contact.id, source='RocketReach')
    # A temporary provider failure should not erase a live lookup.
    temporary_error = status in ('api_error', 'rate_limited') and is_check
    if temporary_error and previous.get('status') in ('pending', 'awaiting_email', 'pending_manual'):
        status = previous['status']
        check_count = int(previous.get('check_count') or 0)

    delays = (12, 40, 120, 180)
    next_check_at = ((timestamp + timedelta(seconds=delays[min(check_count, len(delays) - 1)])).isoformat()
                     if status in ('pending', 'awaiting_email', 'pending_manual') and provider_id else '')
    if temporary_error and status in ('pending', 'awaiting_email', 'pending_manual'):
        next_check_at = (timestamp + timedelta(seconds=90)).isoformat()
    detail = str(result.get('detail') or '')[:180]
    if status == 'not_found' and provider_id:
        detail = 'No professional email after checking the existing RocketReach lookup; optional paid retry available later.'
    if status == 'pending_manual':
        detail = 'Still processing after five checks. Automatic polling stopped; check manually later.'
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
        from .linkedin_urls import linkedin_profile_url
        if not linkedin_profile_url(contact.linkedin_url):
            raise HTTPException(422, 'A safe HTTPS LinkedIn URL is required.')
        previous = _rocketreach_state(db, contact.id)
        if previous:
            if not data.retry:
                result = _rocketreach_public(previous, contact)
                result['detail'] = 'Existing lookup retained; use status recheck instead of another paid lookup.'
                return result
            if previous.get('status') in ('pending', 'awaiting_email', 'pending_manual') and previous.get('provider_id'):
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
        if previous.get('status') not in ('pending', 'awaiting_email', 'pending_manual') or not previous.get('provider_id'):
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
    jd: str = Field(default='', max_length=12000)
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
        old = db.execute(select(Job).where(Job.post_url == url, Job.status != 'archived')).scalar_one_or_none()
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

def save_import_batch(db, result, filename, started_at):
    """Save the exact job/contact scope for this upload in persistent storage."""
    batch_id = secrets.token_hex(16)
    contact_scope = result.pop('batch_contacts', {})
    summary = {key: result.get(key) for key in (
        'created', 'duplicates', 'skipped', 'contacts_created',
        'rows_read', 'possible_duplicates', 'errors')}
    set_kv(db, 'import_batch:' + batch_id, json.dumps({
        'created_at': started_at, 'filename': filename[:120],
        'job_ids': result['job_ids'], 'contacts_by_job': contact_scope,
        'summary': summary,
    }))
    for job_id in result['job_ids']:
        log(db, 'job_uploaded', job_id, batch_id=batch_id, filename=filename[:120])
    result['batch_id'] = batch_id
    return contact_scope


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
async def import_spreadsheet(background_tasks: BackgroundTasks, file: UploadFile = File(...), user: str = Depends(owner)):
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
        started_at = now().isoformat()
        result = import_rows(db, rows)
        scope = save_import_batch(db, result, filename, started_at)
        from .auto_pipeline import enqueue_import
        result['automation'] = enqueue_import(db, result['job_ids'], scope)
        from .auto_pipeline import process_import_batch
        background_tasks.add_task(process_import_batch, result['batch_id'])
        result['automation']['started_immediately'] = True
        log(db, 'spreadsheet_import_finished', **{k: result[k] for k in (
            'created', 'duplicates', 'skipped', 'contacts_created', 'rows_read')})
        return result


@app.post('/api/jobs/import-csv')
def import_pasted_csv(data: CsvImportInput, background_tasks: BackgroundTasks, user: str = Depends(owner)):
    if demo_mode():
        raise HTTPException(409, 'Real spreadsheet import is disabled in demo mode.')
    from .spreadsheet_import import parse_csv, import_rows
    try:
        rows = parse_csv(data.csv_text)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with session() as db:
        started_at = now().isoformat()
        result = import_rows(db, rows)
        scope = save_import_batch(db, result, 'Pasted CSV', started_at)
        from .auto_pipeline import enqueue_import
        result['automation'] = enqueue_import(db, result['job_ids'], scope)
        from .auto_pipeline import process_import_batch
        background_tasks.add_task(process_import_batch, result['batch_id'])
        result['automation']['started_immediately'] = True
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
    if not demo_mode():
        raise HTTPException(409, 'Scheduled sending is disabled. Use reviewed drafts only.')
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


class EmailPromptUpdate(BaseModel):
    prompt: str = Field(min_length=100, max_length=12000)


class EmailTemplateControls(BaseModel):
    model_config = ConfigDict(extra='forbid')
    subjects: list[str]
    subject_mode: Literal['rotate','fixed']
    subject_index: int
    body_template: str
    followup_template: str
    resume_id: Literal['analytics','data_science']
    max_repairs: int
    allowed_fact_ids: list[str]


@app.get('/api/email/templates')
def email_templates(user: str = Depends(owner)):
    from .email_policy import load_settings, TOKENS
    from .gemini import CAREER_FACTS
    with session() as db:
        return {'controls':load_settings(db), 'facts':CAREER_FACTS,
                'variables':sorted(TOKENS), 'resumes':list_resumes(db)}


@app.put('/api/email/templates')
def save_email_templates(data: EmailTemplateControls, user: str = Depends(owner)):
    from .email_policy import validate_settings, EmailValidationError
    try:
        value = validate_settings(data.model_dump())
    except EmailValidationError as exc:
        raise HTTPException(422,str(exc))
    with session() as db:
        set_kv(db,'email_template_controls',json.dumps(value))
        log(db,'email_templates_updated',subject_count=len(value['subjects']),resume_id=value['resume_id'])
    return {'saved':True, 'controls':value}


@app.get('/api/email/prompt')
def get_email_prompt(user: str = Depends(owner)):
    from .gemini import DEFAULT_EMAIL_PROMPT
    with session() as db:
        saved = get_kv(db, 'email_generation_prompt')
    return {'prompt': saved or DEFAULT_EMAIL_PROMPT, 'customized': bool(saved)}


@app.put('/api/email/prompt')
def update_email_prompt(data: EmailPromptUpdate, user: str = Depends(owner)):
    with session() as db:
        set_kv(db, 'email_generation_prompt', data.prompt.strip())
        log(db, 'email_prompt_updated', length=len(data.prompt))
    return {'saved': True}


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


@app.post('/api/imports/{batch_id}/process-now')
def process_uploaded_batch_now(batch_id: str, user: str = Depends(owner)):
    """Immediately process only this upload, independent of the cron schedule."""
    if not re.fullmatch(r'[0-9a-f]{32}', batch_id):
        raise HTTPException(404, 'Upload batch not found')
    with session() as db:
        if not get_kv(db, 'import_batch:' + batch_id, ''):
            raise HTTPException(404, 'Upload batch not found')
    from .auto_pipeline import process_import_batch
    return {'automation': process_import_batch(batch_id)}


@app.post('/api/automation/run-now')
def run_automation_now(user: str = Depends(owner)):
    """Owner-authenticated recovery and diagnostic trigger for queued imports."""
    if demo_mode():
        raise HTTPException(409, 'Automation unavailable in demo mode')
    from .auto_pipeline import process_pending
    with session() as db:
        result = process_pending(db, limit=3)
        db.commit()
        return {'automation': result}


@app.get('/api/cron/tick')
def cron_tick(request: Request):
    """Internal Vercel Schedule dispatch, authenticated with CRON_SECRET."""
    secret = env('CRON_SECRET')
    if not secret or not hmac.compare_digest(request.headers.get('authorization', ''), f'Bearer {secret}'):
        raise HTTPException(401, 'Invalid scheduler credential')
    if demo_mode():
        return {'status': 'demo_mode'}
    with session() as db:
        from .auto_pipeline import process_pending
        tick_at = now()
        interval = int(get_kv(db, 'scheduler_interval_minutes', '5'))
        previous = get_kv(db, 'scheduler_last_processed', '')
        try:
            should_process = not previous or (tick_at - datetime.fromisoformat(previous)).total_seconds() >= interval * 60 - 15
        except ValueError:
            should_process = True
        results = {'sending': {}, 'replies': {}, 'automation': {'skipped': True}}
        set_kv(db, 'scheduler_last_tick', tick_at.isoformat())
        db.commit()

        set_kv(db, 'scheduler_send_last_run', now().isoformat())
        db.commit()
        try:
            results['sending'] = send_due(db)
            set_kv(db, 'scheduler_send_last_result', json.dumps(results['sending'], default=str)[:800])
            if not results['sending'].get('error') and not results['sending'].get('manual_review_required'):
                set_kv(db, 'scheduler_send_last_success', now().isoformat())
        except Exception as exc:
            db.rollback()
            results['sending'] = {'error': type(exc).__name__ + ': ' + str(exc)[:200]}
            set_kv(db, 'scheduler_send_last_result', json.dumps(results['sending']))
            log(db, 'scheduler_send_error', error=str(exc)[:300])
        db.commit()

        set_kv(db, 'scheduler_reply_last_run', now().isoformat())
        db.commit()
        try:
            results['replies'] = monitor_replies(db)
            set_kv(db, 'scheduler_reply_last_result', json.dumps(results['replies'], default=str)[:800])
            set_kv(db, 'scheduler_reply_last_success', now().isoformat())
        except Exception as exc:
            db.rollback()
            results['replies'] = {'error': type(exc).__name__ + ': ' + str(exc)[:200]}
            set_kv(db, 'scheduler_reply_last_result', json.dumps(results['replies']))
            log(db, 'scheduler_reply_error', error=str(exc)[:300])
        db.commit()

        if should_process:
            try:
                results['automation'] = process_pending(db, limit=3)
            except Exception as exc:
                db.rollback()
                results['automation'] = {'error': str(exc)[:300]}
                log(db, 'scheduler_automation_error', error=str(exc)[:300])
            set_kv(db, 'scheduler_last_processed', tick_at.isoformat())
            db.commit()
        set_kv(db, 'scheduler_last_result', json.dumps(results, default=str)[:3000])
        log(db, 'scheduler_tick', **results)
        db.commit()
        return {**results, 'interval_minutes': interval, 'heartbeat_minutes': 15}


# Match Vercel's CDN script URLs locally without catching unknown API routes.
@app.get('/{asset_name}.js')
def frontend_script(asset_name: str):
    if asset_name not in ('app','workspace','email-controls'):
        raise HTTPException(404,'Asset not found')
    return FileResponse(ROOT / 'public' / (asset_name+'.js'), media_type='application/javascript')
