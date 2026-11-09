"""Owner-authorized Excel import automation. Runs in bounded batches on scheduler ticks."""
import base64
import json
import re
import time
from datetime import timedelta
from sqlalchemy import select
from .db import Job, Contact, Outreach, Integration, get_kv, set_kv, log, now
from .config import demo_mode, env
from .resume_library import get_resume
from .gemini import build_email_preview
from .integrations import gmail_create_draft, gmail_get_draft, inspect_gmail_draft, rocketreach_lookup, rocketreach_check_status, send_telegram
from .scheduling import schedule_slot, holidays
from .calendar_rules import next_fixed_send
from .linkedin_urls import linkedin_profile_url
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KEY = 'atlas_auto_import_enabled'

def enabled(db):
    return get_kv(db, KEY, '') == 'true'

def enqueue_import(db, job_ids, batch_contacts=None):
    """Queue one independent outreach per contact. Upload authorizes these sends."""
    if demo_mode():
        return {'queued': 0}
    set_kv(db, KEY, 'true')
    count = 0
    for job_id in job_ids:
        job = db.get(Job, job_id)
        if not job or job.status == 'duplicate_review':
            continue
        allowed = set((batch_contacts or {}).get(job_id, [])) if batch_contacts is not None else None
        contacts = db.execute(select(Contact).where(Contact.job_id == job_id).order_by(Contact.created_at, Contact.id)).scalars().all()
        if allowed is not None:
            contacts = [contact for contact in contacts if contact.id in allowed]
        for index, contact in enumerate(contacts, 1):
            if db.execute(select(Outreach.id).where(Outreach.job_id == job_id, Outreach.contact_id == contact.id)).first():
                continue
            row = Outreach(job_id=job_id, contact_id=contact.id, status='pending_contact')
            db.add(row)
            db.flush()
            log(db, 'auto_contact_queued', job_id, row.id, contact_id=contact.id, contact_number=index, contact_name=contact.name)
            count += 1
        if not contacts:
            log(db, 'auto_job_no_contacts', job_id, detail='No contact supplied in spreadsheet')
    return {'queued': count}

def _failure(db, row, message):
    row.status = 'needs_attention'
    row.error = str(message)[:350]
    log(db, 'auto_outreach_failed', row.job_id, row.id, contact_id=row.contact_id, error=row.error)
    try:
        job = db.get(Job, row.job_id)
        send_telegram('Atlas attention required: ' + job.company + ' — ' + job.title + '\n' + row.error)
    except Exception:
        pass

def recover_rejected_linkedin_contacts(db, *, contact_ids=None):
    """Requeue legacy Atlas URL-validation failures when a real URL is recoverable.

    Older imports discarded some country-specific LinkedIn URLs. Only restore
    from a contact-specific evidence URL, or the named poster's author URL.
    Never guess an address, invent a profile, or retry already drafted mail.
    """
    query = select(Outreach).where(
        Outreach.status == 'needs_attention',
        Outreach.error.like('No usable LinkedIn person profile for enrichment%'))
    if contact_ids is not None:
        if not contact_ids:
            return 0
        query = query.where(Outreach.contact_id.in_(contact_ids))
    failures = db.execute(query.limit(300)).scalars().all()
    recovered = 0
    for row in failures:
        if row.gmail_draft_id or row.sent_at:
            continue
        contact = db.get(Contact, row.contact_id)
        if not contact:
            continue
        profile = linkedin_profile_url(contact.linkedin_url)
        if not profile:
            profile = linkedin_profile_url(contact.evidence_url)
        if not profile:
            job = db.get(Job, row.job_id)
            if (job and job.author_name and contact.name
                    and job.author_name.strip().casefold() == contact.name.strip().casefold()):
                profile = linkedin_profile_url(job.author_url)
        if not profile:
            continue
        if not contact.linkedin_url:
            contact.linkedin_url = profile
        row.status, row.error = 'pending_contact', ''
        recovered += 1
        log(db, 'auto_linkedin_url_recovered', row.job_id, row.id,
            contact_id=contact.id, source='stored_contact_or_verified_author_evidence')
    return recovered


def recover_rejected_email_links(db, *, contact_ids=None):
    """Requeue only legacy Gemini link/email validation failures, once per row.

    This never touches a Gmail draft or sent message. The import previously
    authorized automation, and a subsequent worker tick may regenerate the
    failed email with the fixed sanitizer. Other failures stay held.
    """
    query = select(Outreach).where(
        Outreach.status == 'needs_attention',
        Outreach.error.like(
            'Draft/schedule error: GeminiError: Gemini added a link or email address not supplied by Atlas%'))
    if contact_ids is not None:
        if not contact_ids:
            return 0
        query = query.where(Outreach.contact_id.in_(contact_ids))
    rows = db.execute(query.limit(300)).scalars().all()
    recovered = 0
    for row in rows:
        if row.gmail_draft_id or row.sent_at:
            continue
        retry_key = 'auto_email_link_retry:' + row.id
        if get_kv(db, retry_key, ''):
            continue
        set_kv(db, retry_key, now().isoformat())
        row.status, row.error = 'pending_contact', ''
        recovered += 1
        log(db, 'auto_email_link_validation_recovered', row.job_id, row.id,
            contact_id=row.contact_id, retry_limit=1)
    return recovered


def process_pending(db, limit=4, *, contact_ids=None, exclude_ids=None):
    """Idempotent across successful drafts; unresolved contact lookup is retried on later ticks."""
    if demo_mode() or not enabled(db):
        return {'processed': 0, 'scheduled': 0}
    recover_rejected_linkedin_contacts(db, contact_ids=contact_ids)
    recover_rejected_email_links(db, contact_ids=contact_ids)
    # Never let an unresolved contact on an old job block later jobs.
    query = select(Outreach).where(Outreach.status == 'pending_contact')
    if contact_ids is not None:
        if not contact_ids:
            return {'processed': 0, 'scheduled': 0, 'outreach_ids': []}
        query = query.where(Outreach.contact_id.in_(contact_ids))
    if exclude_ids:
        query = query.where(Outreach.id.not_in(exclude_ids))
    rows = db.execute(query.order_by(Outreach.created_at, Outreach.id)
                      .limit(limit).with_for_update(skip_locked=True)).scalars().all()
    scheduled = 0
    for row in rows:
        job = db.get(Job, row.job_id)
        if not job or job.status in ('rejected', 'duplicate_review'):
            _failure(db, row, 'Job rejected or possible duplicate')
            continue
        contact = db.get(Contact, row.contact_id)
        if not contact:
            _failure(db, row, 'Contact no longer exists')
            continue
        log(db, 'auto_contact_processing', job.id, row.id, contact_id=contact.id, contact_name=contact.name)
        if contact.email and re.fullmatch(r'[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+', contact.email):
            log(db, 'auto_contact_email_found', job.id, row.id, contact_id=contact.id, source='spreadsheet')
        else:
            contact = None
        if not contact:
            # Paid RocketReach lookups require a concrete person profile; never guess emails.
            if not env('ROCKETREACH_API_KEY'):
                _failure(db, row, 'No contact email and RocketReach is not configured')
                continue
            candidate = db.get(Contact, row.contact_id)
            if candidate and not linkedin_profile_url(candidate.linkedin_url):
                candidate = None
            if not candidate:
                _failure(db, row, 'No usable LinkedIn person profile for enrichment')
                continue
            state_key = 'auto_rr:' + candidate.id
            state = json.loads(get_kv(db, state_key, '{}'))
            try:
                log(db, 'auto_contact_email_search', job.id, row.id, contact_id=candidate.id, provider='RocketReach')
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
                    log(db, 'auto_contact_email_found', job.id, row.id, contact_id=candidate.id, source='RocketReach')
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
            log(db, 'auto_draft_building', job.id, row.id, contact_id=contact.id)
            item, pdf = get_resume(db, 'analytics')
            prompt = get_kv(db, 'email_generation_prompt', '')
            preview = build_email_preview(job.company, job.title, job.jd or '', contact.name, prompt)
            draft = gmail_create_draft(db, contact.email, preview['subject'], preview['body'], pdf, item['filename'])
            draft_id = draft['id']
            log(db, 'auto_gmail_draft_created', job.id, row.id, contact_id=contact.id, draft_id=draft_id)
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
            log(db, 'auto_email_drafted_and_scheduled', job.id, row.id, contact_id=contact.id, draft_id=draft_id, due_at=row.due_at)
        except Exception as exc:
            _failure(db, row, 'Draft/schedule error: '+type(exc).__name__+': '+str(exc)[:150])
    return {'processed': len(rows), 'scheduled': scheduled, 'outreach_ids': [row.id for row in rows]}


def process_import_batch(batch_id, *, max_seconds=230):
    """Run a newly uploaded batch immediately after the upload response.

    Work is saved after every contact; the Vercel scheduler remains the
    recovery path for interrupted work and RocketReach pending results.
    """
    from .db import session
    with session() as db:
        raw = get_kv(db, 'import_batch:' + batch_id, '')
        if not raw:
            return {'error': 'Unknown upload batch'}
        scope = json.loads(raw).get('contacts_by_job', {})
        contact_ids = list(dict.fromkeys(cid for ids in scope.values() for cid in ids))
        log(db, 'import_batch_worker_started', batch_id=batch_id, contacts=len(contact_ids))
    processed = set()
    scheduled = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < max_seconds:
            with session() as db:
                result = process_pending(db, limit=1, contact_ids=contact_ids, exclude_ids=processed)
                if not result['processed']:
                    break
                processed.update(result['outreach_ids'])
                scheduled += result['scheduled']
        with session() as db:
            log(db, 'import_batch_worker_finished', batch_id=batch_id,
                processed=len(processed), scheduled=scheduled,
                time_budget_exhausted=(time.monotonic()-started >= max_seconds))
        return {'processed': len(processed), 'scheduled': scheduled}
    except Exception as exc:
        with session() as db:
            log(db, 'import_batch_worker_error', batch_id=batch_id,
                error=type(exc).__name__ + ': ' + str(exc)[:300])
        raise
