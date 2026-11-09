from __future__ import annotations
import base64
import re
from datetime import datetime, timezone
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from .config import settings, demo_mode, env
from .db import Job, Contact, Outreach, Event, Integration, log, now
from .calendar_rules import next_fixed_send, followup_due, is_workday, aware
from .scheduling import holidays
from .resume import tailored_pdf, resume_facts
from .integrations import (gmail_create_draft, gmail_get_draft, inspect_gmail_draft,
     gmail_send_existing_draft, gmail_get_thread, gmail_get_message, gmail_delete_draft,
     message_header, send_telegram)


def build_outreach(job: Job, contact: Contact | None):
    info = resume_facts()
    name = (contact.name.split()[0] if contact and contact.name else 'Hiring Team')
    title = job.title
    low = (job.title + ' ' + job.jd).lower()
    points = []
    if any(x in low for x in ['risk', 'fraud', 'credit', 'payment']):
        points.append('At American Express, I build enterprise analytics and GenAI workflows for banking operations.')
        points.append('At Plivo, my fraud analytics work delivered approximately $400K-$450K in annual savings.')
    elif any(x in low for x in ['commercial', 'revenue', 'sales', 'finance']):
        points.append('At American Express, I own analytics projects including US SME account distribution and territory mapping.')
        points.append('At Plivo, pricing and margin analytics helped improve SMS margins by 15%.')
    else:
        points.append('At American Express, I lead end-to-end analytics delivery and guide two analysts.')
        points.append('I have built production GenAI/RAG solutions and BI dashboards using SQL, Python, Tableau and Power BI.')
    subject = f'Interest in {title} | Analytics & GenAI | IIT Mandi'
    body = (f"Hi {name},\n\nI came across the {title} opportunity at {job.company} and wanted to reach out directly. "
            f"I bring 8+ years in analytics spanning financial services, telecom and consulting.\n\n"
            + ''.join(f'- {point}\n' for point in points)
            + f"\nI have attached my resume tailored to this opportunity. I'd welcome a brief conversation if my background aligns with your team.\n\n"
            f"Regards,\n{info['name']}\n{info['contact']['linkedin']}")
    return subject, body

def preferred_contact(db: Session, job_id: str) -> Contact | None:
    contacts = db.execute(select(Contact).where(Contact.job_id == job_id)).scalars().all()
    with_emails = [c for c in contacts if c.email and '@' in c.email]
    if not with_emails:
        return None
    weights = {'confirmed': 0, 'potential': 1}
    return sorted(with_emails, key=lambda c: (weights.get(c.relationship, 2),
          0 if 'recruiter' in c.category.lower() else 1))[0]

def approve_job(db: Session, job_id: str) -> Outreach:
    if not demo_mode():
        raise ValueError('Use Review email + resume with Gemini. Live outreach requires approved master PDF, resume preview and explicit Gmail draft review.')
    job = db.get(Job, job_id)
    if not job:
        raise ValueError('Job not found')
    if job.status == 'rejected':
        raise ValueError('Job has been rejected. Reopen it before approving.')
    existing = db.execute(select(Outreach).where(Outreach.job_id == job_id)).scalar_one_or_none()
    if existing:
        return existing
    contact = preferred_contact(db, job_id)
    subject, body = build_outreach(job, contact)
    attachment = tailored_pdf(job.jd, job.title)
    row = Outreach(job_id=job.id, contact_id=(contact.id if contact else ''),
                   to_email=(contact.email if contact else ''), subject=subject, body=body,
                   resume_b64=base64.b64encode(attachment).decode(),
                   resume_name=settings()['resume']['attachment_filename'],
                   due_at=next_fixed_send(now()))
    job.status = 'approved'
    db.add(row)
    db.flush()
    log(db, 'job_approved', job.id, row.id, contact_found=bool(contact), due=row.due_at)
    if contact and not demo_mode() and db.get(Integration, 'gmail'):
        try:
            draft = gmail_create_draft(db, row.to_email, subject, body, attachment, row.resume_name)
            row.gmail_draft_id = draft['id']
            row.status = 'scheduled'
            log(db, 'gmail_draft_created', job.id, row.id, gmail_draft_id=row.gmail_draft_id)
        except Exception as exc:
            row.status = 'needs_attention'
            row.error = f'Draft creation failed: {type(exc).__name__}: {exc}'[:350]
            log(db, 'draft_creation_failed', job.id, row.id, error=row.error)
    elif contact and demo_mode():
        row.status = 'demo_draft'
    elif contact:
        row.status = 'awaiting_gmail'
    else:
        row.status = 'pending_contact'
    return row

def prepare_outreach(db: Session, outreach_id: str) -> Outreach:
    if not demo_mode():
        raise ValueError('Use the reviewed application workflow; automatic legacy Gmail draft preparation is disabled.')
    row = db.get(Outreach, outreach_id)
    if not row:
        raise ValueError('Outreach not found')
    if row.status not in ('pending_contact', 'awaiting_gmail', 'needs_attention', 'demo_draft'):
        raise ValueError('Only an unprepared outreach can be prepared again')
    if demo_mode():
        raise ValueError('Connect a real account in live mode to create Gmail drafts')
    if not db.get(Integration, 'gmail'):
        raise ValueError('Gmail OAuth is not connected')
    if not row.to_email:
        contact = preferred_contact(db, row.job_id)
        if not contact:
            raise ValueError('No verified or supplied professional contact email is available')
        row.contact_id = contact.id
        row.to_email = contact.email
        job = db.get(Job, row.job_id)
        row.subject, row.body = build_outreach(job, contact)
    attachment = base64.b64decode(row.resume_b64)
    draft = gmail_create_draft(db, row.to_email, row.subject, row.body, attachment, row.resume_name)
    row.gmail_draft_id = draft['id']
    row.due_at = next_fixed_send(now())
    row.status = 'scheduled'
    row.error = ''
    log(db, 'gmail_draft_created', row.job_id, row.id, gmail_draft_id=row.gmail_draft_id)
    return row

def cancel_outreach(db: Session, outreach_id: str):
    row = db.get(Outreach, outreach_id)
    if not row:
        raise ValueError('Outreach not found')
    if row.status in ('sent', 'responded', 'awaiting_reply', 'finished'):
        raise ValueError('Already sent; cannot cancel sent mail. Cancel future follow-up only.')
    if row.status == 'sending':
        raise ValueError('Send is already in progress; check Gmail first')
    if row.gmail_draft_id and not demo_mode() and db.get(Integration, 'gmail'):
        try:
            gmail_delete_draft(db, row.gmail_draft_id)
            row.gmail_draft_id = ''
        except Exception:
            # Fail-closed: cancel in DB even if Gmail draft deletion failed.
            pass
    row.status = 'cancelled'
    row.due_at = None
    log(db, 'outreach_cancelled', row.job_id, row.id)
    return row

def classify_reply(text: str, subject: str) -> str:
    lowered = (text + ' ' + subject).lower()
    if any(x in lowered for x in ['automatic reply', 'out of office', 'auto-reply', 'vacation responder']):
        return 'out_of_office'
    if any(x in lowered for x in ['unsubscribe', 'remove me', 'do not contact', 'stop emailing']):
        return 'opt_out'
    if any(x in lowered for x in ['undeliverable', 'delivery status notification', 'address not found']):
        return 'bounce'
    if any(x in lowered for x in ['unfortunately', 'not proceeding', 'not moving forward', 'position filled']):
        return 'rejection'
    if any(x in lowered for x in ['interview', 'availability', 'schedule a call', 'speak with you']):
        return 'interview_interest'
    if any(x in lowered for x in ['send your resume', 'share your cv', 'additional details']):
        return 'information_requested'
    return 'reply_received'

def monitor_replies(db: Session) -> dict:
    if demo_mode() or not db.get(Integration, 'gmail'):
        return {'checked': 0, 'new_replies': 0}
    rows = db.execute(select(Outreach).where(Outreach.gmail_thread_id != '',
                     Outreach.status.in_(['awaiting_reply', 'followup_scheduled', 'finished']))).scalars().all()
    new = 0
    sender = db.get(Integration, 'gmail').owner_email.lower()
    for row in rows:
        try:
            thread = gmail_get_thread(db, row.gmail_thread_id)
            messages = thread.get('messages') or []
            candidates = []
            for msg in messages:
                from_addr = re.search(r'<([^>]+)>', message_header(msg, 'From'))
                from_addr = from_addr.group(1).lower() if from_addr else message_header(msg, 'From').strip().lower()
                if from_addr == sender or not from_addr or 'me' == from_addr:
                    continue
                if msg['id'] in (row.sent_message_id, row.last_reply_message_id):
                    continue
                received_ms = int(msg.get('internalDate') or 0)
                if row.sent_at and received_ms and received_ms < int(aware(row.sent_at).timestamp() * 1000):
                    continue
                candidates.append(msg)
            if not candidates:
                continue
            message = candidates[-1]
            raw_snippet = message.get('snippet', '')
            category = classify_reply(raw_snippet, message_header(message, 'Subject'))
            row.last_reply_class = category
            row.last_reply_message_id = message['id']
            row.last_reply_at = now()
            row.status = 'responded'
            row.due_at = None
            if row.gmail_draft_id:
                try:
                    gmail_delete_draft(db, row.gmail_draft_id)
                except Exception:
                    pass
                row.gmail_draft_id = ''
            job = db.get(Job, row.job_id)
            job.status = 'reply_received'
            log(db, 'reply_received', row.job_id, row.id, reply_class=category, message_id=message['id'])
            new += 1
            if category != "out_of_office":
                send_telegram(f"Atlas: {'Delivery failed' if category == 'bounce' else 'Human reply received'} — {job.company} - {job.title}\nClassification: {category}\nGmail thread: https://mail.google.com/mail/u/0/#all/{row.gmail_thread_id}\nFollow-ups stopped.")
        except Exception as exc:
            log(db, 'reply_check_failed', row.job_id, row.id, error=str(exc)[:300])
    return {'checked': len(rows), 'new_replies': new}

def _create_followup_draft(db: Session, row: Outreach):
    previous = gmail_get_message(db, row.sent_message_id)
    rfc_id = message_header(previous, 'Message-ID')
    subject = inspect_subject = row.subject
    if not subject.lower().startswith('re:'):
        subject = 'Re: ' + subject
    body = ("Hello,\n\nJust following up on my earlier note about the analytics opportunity. "
            "I'd welcome the chance to share how my experience could support the team.\n\n"
            "Regards,\nNitesh Bansiwal")
    draft = gmail_create_draft(db, row.to_email, subject, body, b'', '', row.gmail_thread_id, rfc_id)
    row.gmail_draft_id = draft['id']
    row.subject = subject
    row.body = body
    row.due_at = followup_due(row.sent_at)
    row.status = 'followup_scheduled'
    log(db, 'followup_draft_created', row.job_id, row.id, due_at=row.due_at, attempt=row.followup_count + 1)

def send_due(db: Session, *, clock: datetime | None = None) -> dict:
    cfg = settings()['email']
    if demo_mode():
        return {'sent': 0, 'blocked': 'Demo mode; no emails sent'}
    if not db.get(Integration, 'gmail'):
        return {'sent': 0, 'blocked': 'Gmail not connected'}
    at = aware(clock or now())
    local = at.astimezone(__import__('zoneinfo').ZoneInfo(settings()['timezone']))
    from .scheduling import is_manual
    automatic_window = (is_workday(local.date()) and local.date().isoformat() not in holidays(db)
                        and cfg['start'] <= local.strftime('%H:%M') <= cfg['end'])
    today_start_utc = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    recent_sends = db.execute(select(func.count()).select_from(Event).where(
         Event.action == 'email_sent', Event.created_at >= today_start_utc)).scalar_one()
    auto_slots = max(0, int(cfg['max_sends_per_day']) - recent_sends)
    due_rows = db.execute(select(Outreach).where(
        Outreach.status.in_(['scheduled', 'followup_scheduled']), Outreach.due_at <= at)
        .order_by(Outreach.due_at).limit(250)).scalars().all()
    rows = []
    for candidate in due_rows:
        if is_manual(db, candidate.id):
            rows.append(candidate)
        elif automatic_window and auto_slots > 0:
            rows.append(candidate)
            auto_slots -= 1
    if not rows:
        return {'sent': 0, 'blocked': 'No eligible due emails'}
    sent, errors = 0, []
    for row in rows:
        # Claim only the scheduled state; a second concurrent worker cannot process it.
        desired_state = row.status
        result = db.execute(update(Outreach).where(Outreach.id == row.id, Outreach.status == desired_state,
                          Outreach.due_at <= at)
                       .values(status='sending'))
        db.commit()
        if result.rowcount != 1:
            continue
        try:
            if not row.gmail_draft_id:
                raise RuntimeError('No Gmail draft linked to the scheduled message')
            if desired_state == 'followup_scheduled':
                # Fail closed: on EVERY follow-up, re-check the Gmail thread at send time.
                # If Gmail cannot be reached, the message stays held for manual review.
                if not row.gmail_thread_id:
                    raise RuntimeError('Missing Gmail thread ID: follow-up requires manual review')
                thread = gmail_get_thread(db, row.gmail_thread_id)
                sender = db.get(Integration, 'gmail').owner_email.lower()
                for msg in thread.get('messages') or []:
                    origin = re.search(r'<([^>]+)>', message_header(msg, 'From'))
                    origin = origin.group(1).lower() if origin else message_header(msg, 'From').strip().lower()
                    received_ms = int(msg.get('internalDate') or 0)
                    # Check for inbound message newer than the latest email we sent.
                    if (origin and origin != sender and origin != 'me' and
                            msg.get('id') != row.sent_message_id and
                            (not received_ms or not row.sent_at or
                             received_ms >= int(aware(row.sent_at).timestamp() * 1000))):
                        row.last_reply_message_id = msg.get('id', '')
                        row.last_reply_at = now()
                        row.last_reply_class = classify_reply(msg.get('snippet', ''), message_header(msg, 'Subject'))
                        row.status = 'responded'
                        row.due_at = None
                        try:
                            gmail_delete_draft(db, row.gmail_draft_id)
                        except Exception:
                            pass
                        row.gmail_draft_id = ''
                        db.get(Job, row.job_id).status = 'reply_received'
                        log(db, 'followup_stopped_on_reply', row.job_id, row.id,
                            reply_class=row.last_reply_class, message_id=row.last_reply_message_id)
                        db.commit()
                        break
                if row.status == 'responded':
                    continue
            live_draft = gmail_get_draft(db, row.gmail_draft_id)
            latest = inspect_gmail_draft(live_draft)
            if not latest['subject'].strip() or not latest['body'].strip():
                raise RuntimeError('Draft subject or body missing; refused to send')
            # User-edited body, subject, recipient(s), and attachments take precedence.
            response = gmail_send_existing_draft(db, row.gmail_draft_id)
            row.to_email = latest['to']
            row.subject = latest['subject']
            row.body = latest['body']
            row.sent_message_id = response.get('id', '')
            row.gmail_thread_id = response.get('threadId') or row.gmail_thread_id
            row.gmail_draft_id = ''
            row.sent_at = at
            row.error = ''
            sent += 1
            log(db, 'email_sent', row.job_id, row.id, gmail_message_id=row.sent_message_id,
                recipient=row.to_email, attachment_count=latest['attachment_count'], attempt=row.followup_count)
            if desired_state == 'followup_scheduled':
                row.followup_count += 1
                log(db, 'followup_sent', row.job_id, row.id, attempt=row.followup_count)
            if cfg['followup']['enabled'] and row.followup_count < cfg['followup']['max_attempts']:
                try:
                    _create_followup_draft(db, row)
                except Exception as exc:
                    row.status = 'awaiting_reply'
                    row.error = f'Follow-up draft failed: {exc}'[:300]
            else:
                row.status = 'awaiting_reply'
                row.due_at = None
            db.commit()
            try:
                job = db.get(Job, row.job_id)
                send_telegram(f"Sent: {job.company} - {job.title}\nTo: {row.to_email}\nFollow-up: {row.due_at or 'not scheduled'}")
            except Exception:
                pass
        except Exception as exc:
            # Ambiguous provider failures are held for manual review, NEVER automatically retried.
            row.status = 'sending_review'
            row.error = f'{type(exc).__name__}: {exc}'[:450]
            log(db, 'send_needs_review', row.job_id, row.id, error=row.error)
            try:
                job = db.get(Job, row.job_id)
                send_telegram(f'Atlas send failure: {job.company} — {job.title}\\n{row.error}')
            except Exception:
                pass
            errors.append(row.id)
            db.commit()
    return {'sent': sent, 'manual_review_required': errors}
