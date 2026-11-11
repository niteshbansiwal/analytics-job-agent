"""Owner-authorized Excel import automation. Runs in bounded batches on scheduler ticks."""
import base64
import json
import re
import time
from datetime import timedelta
from sqlalchemy import select, or_
from sqlalchemy.orm import aliased
from .db import Job, Contact, Outreach, Integration, KV, get_kv, set_kv, log, now
from .config import demo_mode, env
from .resume_library import get_resume
from .gemini import build_email_preview, GeminiError
from .integrations import gmail_create_draft, gmail_get_draft, inspect_gmail_draft, rocketreach_lookup, rocketreach_lookup_name, rocketreach_check_status, send_telegram
from .scheduling import schedule_slot, holidays
from .calendar_rules import next_fixed_send
from .linkedin_urls import linkedin_profile_url
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KEY = 'atlas_auto_import_enabled'
GEMINI_MAX_ATTEMPTS = 3
GEMINI_RETRY_PREFIX = 'auto_gemini_attempts:'
GEMINI_RETRY_AFTER_PREFIX = 'auto_gemini_retry_after:'
# A short defer prevents immediate upload workers from retrying in a tight loop;
# the existing 15-minute Vercel scheduler will pick the task up on its next tick.
GEMINI_RETRY_DEFER_SECONDS = 90
TRANSIENT_LEGACY_GEMINI_ERRORS = (
    'Gemini service temporarily unavailable (HTTP 500)',
    'Gemini service temporarily unavailable (HTTP 502)',
    'Gemini service temporarily unavailable (HTTP 503)',
    'Gemini service temporarily unavailable (HTTP 504)',
    'Gemini quota or rate limit exceeded (HTTP 429)',
    'Email model request timed out',
    'Gemini API network request failed',
)

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

def _recover_transient_gemini_errors(db, *, contact_ids=None):
    """Reopen legacy Gemini 429/5xx/timeout errors on the next available tick.

    Do not reopen manual cancellations, non-transient validation errors,
    or rows that already have an external Gmail draft or sent message.
    """
    query = select(Outreach).where(
        Outreach.status == 'needs_attention',
        Outreach.error.like('Draft/schedule error: GeminiError:%'))
    if contact_ids is not None:
        if not contact_ids:
            return 0
        query = query.where(Outreach.contact_id.in_(contact_ids))
    rows = db.execute(query.order_by(Outreach.created_at).limit(300)
                      .with_for_update(skip_locked=True)).scalars().all()
    recovered = 0
    for row in rows:
        if row.sent_at or row.gmail_draft_id or row.due_at:
            continue
        if not any(err in row.error for err in TRANSIENT_LEGACY_GEMINI_ERRORS):
            continue
        job = db.get(Job, row.job_id)
        if not job or job.status in ('rejected', 'duplicate_review', 'archived'):
            continue
        key = GEMINI_RETRY_PREFIX + row.id
        try:
            attempts = int(get_kv(db, key, '1') or '1')
        except (ValueError, TypeError):
            attempts = 1
        if attempts >= GEMINI_MAX_ATTEMPTS:
            continue
        set_kv(db, key, str(attempts))
        row.status = 'pending_contact'
        row.error = (f'Gemini transient failure; attempt {attempts}/{GEMINI_MAX_ATTEMPTS}. '
                     'Retry queued for scheduler.')
        recovered += 1
        log(db, 'auto_gemini_legacy_retry_requeued', row.job_id, row.id,
            attempt=attempts, max_attempts=GEMINI_MAX_ATTEMPTS)
    return recovered


def _handle_gemini_error(db, row, exc):
    """Persist a bounded transient retry; fail closed after three failed calls."""
    key = GEMINI_RETRY_PREFIX + row.id
    try:
        attempts = int(get_kv(db, key, '0') or '0') + 1
    except (ValueError, TypeError):
        attempts = 1
    set_kv(db, key, str(attempts))
    if (exc.retryable and attempts < GEMINI_MAX_ATTEMPTS
            and not row.gmail_draft_id and not row.sent_at):
        ready_at = now() + timedelta(seconds=GEMINI_RETRY_DEFER_SECONDS)
        set_kv(db, GEMINI_RETRY_AFTER_PREFIX + row.id, ready_at.isoformat())
        row.status = 'pending_contact'
        row.error = (f'Gemini temporarily unavailable: {exc}. '
                     f'Attempt {attempts}/{GEMINI_MAX_ATTEMPTS}; '
                     'retry on the next scheduler trigger.')
        log(db, 'auto_gemini_retry_queued', row.job_id, row.id,
            contact_id=row.contact_id, attempt=attempts,
            max_attempts=GEMINI_MAX_ATTEMPTS, next_eligible_at=ready_at,
            reason=str(exc)[:170])
        return True
    _failure(db, row, 'Draft/schedule error: GeminiError: ' + str(exc)[:190] +
             (f' (max {GEMINI_MAX_ATTEMPTS} attempts reached; review required)'
              if exc.retryable else ' (non-retryable)'))
    return False


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
    failed email with the current URL-preserving generator. Other failures stay held.
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
        retry_key = 'auto_email_link_retry_v2:' + row.id
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
    recovered_gemini = _recover_transient_gemini_errors(db, contact_ids=contact_ids)
    # Never retry already drafted/sent work. Skip a new 503/timeout until the
    # following scheduler tick, without blocking other pending contacts.
    retry_at = aliased(KV)
    query = (select(Outreach)
             .outerjoin(retry_at, retry_at.key == GEMINI_RETRY_AFTER_PREFIX + Outreach.id)
             .where(Outreach.status == 'pending_contact',
                    Outreach.gmail_draft_id == '',
                    Outreach.sent_at.is_(None),
                    or_(retry_at.value.is_(None),
                        retry_at.value <= now().isoformat())))
    if contact_ids is not None:
        if not contact_ids:
            return {'processed': 0, 'scheduled': 0, 'outreach_ids': []}
        query = query.where(Outreach.contact_id.in_(contact_ids))
    if exclude_ids:
        query = query.where(Outreach.id.not_in(exclude_ids))
    rows = db.execute(query.order_by(Outreach.created_at, Outreach.id)
                      .limit(limit).with_for_update(skip_locked=True)).scalars().all()
    scheduled = 0
    gemini_retries_queued = 0
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
            # Search the named person at the employer first. Use LinkedIn only if that fails.
            if not env('ROCKETREACH_API_KEY'):
                _failure(db, row, 'No contact email and RocketReach is not configured')
                continue
            candidate = db.get(Contact, row.contact_id)
            if not candidate or (candidate.name == 'Hiring Team' and not linkedin_profile_url(candidate.linkedin_url)):
                _failure(db, row, 'No usable named contact or LinkedIn person profile for enrichment')
                continue
            # Share the same persisted RocketReach lookup state used by manual
            # enrichment and the dashboard. Never start a second paid lookup
            # when the provider has already issued a person ID.
            from .main import _rocketreach_state, _save_rocketreach_result
            state = _rocketreach_state(db, candidate.id)
            mode = get_kv(db, 'auto_rr_mode:' + candidate.id, '') or ('linkedin' if state else '')
            if not state:
                # Migrate in-flight lookups created by older auto workers.
                try:
                    legacy = json.loads(get_kv(db, 'auto_rr:' + candidate.id, '{}'))
                except (ValueError, TypeError):
                    legacy = {}
                if legacy.get('provider_id') and legacy.get('status') in ('pending', 'awaiting_email'):
                    state = {
                        'status': legacy['status'],
                        'provider_id': str(legacy['provider_id']),
                        'lookup_count': 1, 'check_count': 0,
                        'next_check_at': now().isoformat(),
                        'detail': 'Continuing an existing automated RocketReach lookup',
                    }
                    set_kv(db, 'rocketreach:' + candidate.id, json.dumps(state))
                    mode = 'linkedin'
                    set_kv(db, 'auto_rr_mode:' + candidate.id, mode)
                    log(db, 'auto_rocketreach_legacy_lookup_migrated', job.id, row.id,
                        contact_id=candidate.id)
            try:
                if state.get('status') in ('pending', 'awaiting_email') and state.get('provider_id'):
                    next_check = state.get('next_check_at', '')
                    if next_check:
                        try:
                            if now() < datetime.fromisoformat(next_check):
                                log(db, 'auto_contact_enrichment_waiting', job.id, row.id,
                                    contact_id=candidate.id, next_check_at=next_check)
                                continue
                        except ValueError:
                            pass
                    log(db, 'auto_contact_enrichment_check', job.id, row.id,
                        contact_id=candidate.id, provider='RocketReach',
                        provider_id=state['provider_id'])
                    result = (rocketreach_check_status(state['provider_id'], '', candidate.name, job.company)
                              if mode == 'name' else rocketreach_check_status(state['provider_id'], candidate.linkedin_url))
                    status = _save_rocketreach_result(db, candidate, result, is_check=True)
                elif not state:
                    mode = 'name' if candidate.name != 'Hiring Team' else 'linkedin'
                    set_kv(db, 'auto_rr_mode:' + candidate.id, mode)
                    log(db, 'auto_contact_email_search', job.id, row.id,
                        contact_id=candidate.id, provider='RocketReach', method=mode)
                    result = (rocketreach_lookup_name(candidate.name, job.company, candidate.title)
                              if mode == 'name' else rocketreach_lookup(candidate.linkedin_url))
                    status = _save_rocketreach_result(db, candidate, result, is_check=False)
                elif (mode == 'name' and int(state.get('lookup_count') or 0) < 2
                      and state.get('status') in ('not_found', 'identity_unverified')
                      and linkedin_profile_url(candidate.linkedin_url)):
                    set_kv(db, 'auto_rr_mode:' + candidate.id, 'linkedin')
                    mode = 'linkedin'
                    log(db, 'auto_contact_email_search', job.id, row.id,
                        contact_id=candidate.id, provider='RocketReach', method='linkedin')
                    result = rocketreach_lookup(candidate.linkedin_url)
                    status = _save_rocketreach_result(db, candidate, result, is_check=False)
                else:
                    # Existing lookup is terminal or requires manual attention.
                    # Never initiate a duplicate paid lookup automatically.
                    _failure(db, row, 'RocketReach needs review: ' +
                             str(state.get('status', 'unknown')))
                    continue
                if status.get('status') == 'returned' and candidate.email:
                    contact = candidate
                    log(db, 'auto_contact_email_found', job.id, row.id,
                        contact_id=candidate.id, source='RocketReach')
                elif status.get('status') in ('pending', 'awaiting_email'):
                    log(db, 'auto_contact_enrichment_pending', job.id, row.id,
                        contact_id=candidate.id,
                        next_check_at=status.get('next_check_at', ''))
                    continue
                else:
                    if mode == 'name' and status.get('status') in ('not_found', 'identity_unverified') and linkedin_profile_url(candidate.linkedin_url):
                        log(db, 'auto_contact_profile_fallback_queued', job.id, row.id, contact_id=candidate.id)
                        continue
                    _failure(db, row, 'RocketReach lookup needs review: ' +
                             str(status.get('status', 'unknown')) + ' — ' +
                             str(status.get('detail', ''))[:110])
                    continue
            except Exception as exc:
                _failure(db, row, 'RocketReach error: ' + type(exc).__name__)
                continue
        if not db.get(Integration, 'gmail'):
            _failure(db, row, 'Gmail is not connected')
            continue
        try:
            log(db, 'auto_draft_building', job.id, row.id, contact_id=contact.id)
            from .email_policy import load_settings
            controls = load_settings(db)
            item, pdf = get_resume(db, controls['resume_id'])
            prompt = get_kv(db, 'email_generation_prompt', '')
            preview = build_email_preview(job.company, job.title, job.jd or '', contact.name, prompt, job.post_url, controls=controls)
            draft = gmail_create_draft(db, contact.email, preview['subject'], preview['body'], pdf, item['filename'])
            draft_id = draft['id']
            log(db, 'auto_gmail_draft_created', job.id, row.id, contact_id=contact.id, draft_id=draft_id,
                repair_attempts=preview['repair_attempts'], validation_status='passed',
                subject_index=preview['subject_index'], resume_id=item['id'], prompt_fingerprint=preview['prompt_fingerprint'])
            verified = inspect_gmail_draft(gmail_get_draft(db, draft_id))
            if (verified['to'].lower() != contact.email.lower() or verified['attachment_count'] != 1
                    or verified['subject'] != preview['subject']
                    or verified['body'].strip().replace('\r\n','\n') != preview['body'].strip()
                    or job.post_url not in verified.get('html_links', [])
                    or verified['attachment_sha256'] != [item['sha256']]):
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
        except GeminiError as exc:
            if _handle_gemini_error(db, row, exc):
                gemini_retries_queued += 1
        except Exception as exc:
            _failure(db, row, 'Draft/schedule error: '+type(exc).__name__+': '+str(exc)[:150])
    return {'processed': len(rows), 'scheduled': scheduled,
            'gemini_retries_queued': gemini_retries_queued,
            'legacy_gemini_recovered': recovered_gemini,
            'gemini_max_attempts': GEMINI_MAX_ATTEMPTS,
            'outreach_ids': [row.id for row in rows]}


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
