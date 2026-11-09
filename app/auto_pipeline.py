"""Owner-authorized Excel import automation. Runs in bounded batches on scheduler ticks."""
import base64
import json
import re
from datetime import timedelta
from sqlalchemy import select
from .db import Job, Contact, Outreach, Integration, get_kv, set_kv, log, now
from .config import demo_mode, env
from .resume_library import get_resume
from .gemini import build_email_preview
from .integrations import gmail_create_draft, gmail_get_draft, inspect_gmail_draft, rocketreach_lookup, rocketreach_check_status, send_telegram
from .scheduling import schedule_slot, holidays
from .calendar_rules import next_fixed_send
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KEY = 'atlas_auto_import_enabled'

def enabled(db):
    return get_kv(db, KEY, '') == 'true'

def enqueue_import(db, job_ids):
    """The upload is explicit authorization; never requeue existing outreach."""
    if demo_mode():
        return {'queued': 0}
    set_kv(db, KEY, 'true')
    count = 0
    for job_id in job_ids:
        job = db.get(Job, job_id)
        if not job or job.status == 'duplicate_review':
            continue
        if db.execute(select(Outreach.id).where(Outreach.job_id == job_id)).first():
            continue
        row = Outreach(job_id=job_id, status='pending_contact')
        db.add(row)
        db.flush()
        log(db, 'auto_outreach_queued', job_id, row.id)
        count += 1
    return {'queued': count}

def _failure(db, row, message):
    row.status = 'needs_attention'
    row.error = str(message)[:350]
    log(db, 'auto_outreach_failed', row.job_id, row.id, error=row.error)
    try:
        job = db.get(Job, row.job_id)
        send_telegram('Atlas attention required: ' + job.company + ' — ' + job.title + '\n' + row.error)
    except Exception:
        pass

def process_pending(db, limit=4):
    """Idempotent across successful drafts; unresolved contact lookup is retried on later ticks."""
    if demo_mode() or not enabled(db):
        return {'processed': 0, 'scheduled': 0}
    rows = db.execute(select(Outreach).where(Outreach.status == 'pending_contact')
                      .order_by(Outreach.created_at).limit(limit).with_for_update(skip_locked=True)).scalars().all()
    scheduled = 0
    for row in rows:
        job = db.get(Job, row.job_id)
        if not job or job.status in ('rejected', 'duplicate_review'):
            _failure(db, row, 'Job rejected or possible duplicate')
            continue
        contacts = db.execute(select(Contact).where(Contact.job_id == job.id)).scalars().all()
        contact = next((c for c in contacts if c.email and re.fullmatch(r'[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+', c.email)), None)
        if not contact:
            # Paid RocketReach lookups require a concrete person profile; never guess emails.
            if not env('ROCKETREACH_API_KEY'):
                _failure(db, row, 'No contact email and RocketReach is not configured')
                continue
            candidate = next((c for c in contacts if c.linkedin_url.startswith('https://www.linkedin.com/in/') or c.linkedin_url.startswith('https://linkedin.com/in/')), None)
            if not candidate:
                _failure(db, row, 'No usable LinkedIn person profile for enrichment')
                continue
            state_key = 'auto_rr:' + candidate.id
            state = json.loads(get_kv(db, state_key, '{}'))
            try:
                if not state:
                    result = rocketreach_lookup(candidate.linkedin_url)
                elif state.get('provider_id') and state.get('status') in ('pending','awaiting_email'):
                    result = rocketreach_check_status(state['provider_id'], candidate.linkedin_url)
                else:
                    _failure(db, row, 'RocketReach did not return a professional email')
                    continue
                state = {'status': result.get('status','api_error'), 'provider_id': str(result.get('provider_id',''))}
                set_kv(db, state_key, json.dumps(state))
                if result.get('status') == 'returned' and result.get('email'):
                    candidate.email = result['email']
                    candidate.email_status = result.get('email_status','rocketreach_unverified')
                    contact = candidate
                elif result.get('status') in ('pending','awaiting_email'):
                    log(db, 'auto_contact_enrichment_pending', job.id, row.id, contact_id=candidate.id)
                    continue
                else:
                    _failure(db, row, 'RocketReach lookup failed: '+str(result.get('status')))
                    continue
            except Exception as exc:
                _failure(db, row, 'RocketReach error: '+type(exc).__name__)
                continue
        if not db.get(Integration, 'gmail'):
            _failure(db, row, 'Gmail is not connected')
            continue
        try:
            item, pdf = get_resume(db, 'analytics')
            prompt = get_kv(db, 'email_generation_prompt', '')
            preview = build_email_preview(job.company, job.title, job.jd or '', contact.name, prompt)
            draft = gmail_create_draft(db, contact.email, preview['subject'], preview['body'], pdf, item['filename'])
            draft_id = draft['id']
            verified = inspect_gmail_draft(gmail_get_draft(db, draft_id))
            if verified['to'].lower() != contact.email.lower() or verified['attachment_count'] != 1 or not verified['body'].strip():
                raise ValueError('Gmail draft verification mismatch')
            row.contact_id = contact.id
            row.to_email = contact.email
            row.subject = preview['subject']
            row.body = preview['body']
            row.resume_b64 = base64.b64encode(pdf).decode()
            row.resume_name = item['filename']
            row.gmail_draft_id = draft_id
            # Find the next eligible slot using BOTH saved dashboard holidays and YAML rules.
            candidate = now() + timedelta(minutes=4)
            for _ in range(370):
                slot = next_fixed_send(candidate)
                local = slot.astimezone(ZoneInfo('Asia/Kolkata'))
                if local.date().isoformat() not in set(holidays(db)):
                    row.due_at = schedule_slot(db, slot)
                    break
                candidate = slot + timedelta(days=1)
            if row.due_at is None:
                raise ValueError('No eligible send slot found')
            row.status = 'scheduled'
            row.error = ''
            job.status = 'approved'
            scheduled += 1
            log(db, 'auto_email_drafted_and_scheduled', job.id, row.id, draft_id=draft_id, due_at=row.due_at)
        except Exception as exc:
            _failure(db, row, 'Draft/schedule error: '+type(exc).__name__+': '+str(exc)[:150])
    return {'processed': len(rows), 'scheduled': scheduled}
