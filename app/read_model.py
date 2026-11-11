"""Bounded, owner-only dashboard reads. Never calls an external provider."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select, func, or_, exists
from sqlalchemy.orm import defer
from .db import Job, Contact, Outreach, Event, KV


def stamp(value):
    return value.isoformat() if value else None


def details(event):
    try:
        value = json.loads(event.details or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def totals(db):
    # Aggregates use metadata columns only; PDFs and email bodies never load.
    result = {}
    for name, model, condition in (
        ('jobs', Job, Job.status != 'archived'),
        ('new_jobs', Job, Job.status == 'discovered'),
        ('approved', Job, Job.status.in_(['approved', 'reply_received'])),
        ('pipeline', Outreach, Outreach.status.in_(['pending_contact', 'draft_ready', 'scheduled', 'followup_scheduled'])),
        ('sent', Outreach, Outreach.sent_at.is_not(None)),
        ('replied', Outreach, Outreach.last_reply_at.is_not(None)),
        ('attention', Outreach, Outreach.status == 'needs_attention'),
    ):
        result[name] = db.scalar(select(func.count()).select_from(model).where(condition)) or 0
    read = select(KV.key).where(KV.key == 'reply_read:' + Outreach.id,
                                KV.value == Outreach.last_reply_message_id).exists()
    done = select(KV.key).where(KV.key == 'reply_action_done:' + Outreach.id,
                                KV.value == Outreach.last_reply_message_id).exists()
    result['unread'] = db.scalar(select(func.count()).select_from(Outreach).where(Outreach.last_reply_at.is_not(None), ~read)) or 0
    result['action_pending'] = db.scalar(select(func.count()).select_from(Outreach).where(
        Outreach.last_reply_at.is_not(None), ~done,
        Outreach.last_reply_class.not_in(['bounce', 'out_of_office']))) or 0
    return result


def revision(db):
    # Event IDs break timestamp ties. KV changes include scheduler heartbeats.
    last = db.execute(select(Event.id, Event.created_at).order_by(Event.created_at.desc(), Event.id.desc()).limit(1)).first()
    ticks = db.execute(select(KV.key, KV.value).where(KV.key.in_(
        ['scheduler_last_tick', 'scheduler_last_result'])) .order_by(KV.key)).all()
    value = json.dumps([list(map(str, last)) if last else [], [list(t) for t in ticks]])
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def outreach_page(db, page=1, size=25, search='', status='all'):
    query = select(Outreach, Job.company, Job.title, Contact.name).join(Job, Job.id == Outreach.job_id).outerjoin(Contact, Contact.id == Outreach.contact_id)
    if search:
        pattern = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = query.where(or_(*(col.ilike(pattern, escape='\\') for col in
            (Job.company, Job.title, Contact.name, Outreach.to_email))))
    read = select(KV.key).where(KV.key == 'reply_read:' + Outreach.id, KV.value == Outreach.last_reply_message_id).exists()
    done = select(KV.key).where(KV.key == 'reply_action_done:' + Outreach.id, KV.value == Outreach.last_reply_message_id).exists()
    conditions = {
        'processing': Outreach.status.in_(['pending_contact', 'awaiting_gmail']),
        'review': Outreach.status == 'draft_ready',
        'scheduled': Outreach.status.in_(['scheduled', 'followup_scheduled']),
        'awaiting': Outreach.status == 'awaiting_reply',
        'replied': Outreach.last_reply_at.is_not(None),
        'unread': (Outreach.last_reply_at.is_not(None) & ~read),
        'pending': (Outreach.last_reply_at.is_not(None) & ~done & Outreach.last_reply_class.not_in(['bounce', 'out_of_office'])),
        'attention': Outreach.status == 'needs_attention',
    }
    if status in conditions:
        query = query.where(conditions[status])
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = min(page, max(1, (total + size - 1) // size))
    rows = db.execute(query.options(defer(Outreach.resume_b64), defer(Outreach.body))
        .order_by(Job.created_at.desc(), Outreach.created_at.desc(), Outreach.id.desc()).offset((page-1)*size).limit(size)).all()
    keys = [prefix + r.id for r, *_ in rows for prefix in ('reply_read:', 'reply_action_done:')]
    flags = dict(db.execute(select(KV.key, KV.value).where(KV.key.in_(keys))).all()) if keys else {}
    items = []
    for row, company, title, name in rows:
        item = {key: stamp(getattr(row, key)) if key.endswith('_at') else getattr(row, key) for key in
            ('id', 'job_id', 'contact_id', 'to_email', 'subject', 'resume_name', 'gmail_draft_id',
             'gmail_thread_id', 'status', 'due_at', 'sent_at', 'last_reply_at', 'last_reply_class', 'followup_count', 'error', 'created_at')}
        item.update(company=company, title=title, contact_name=name or '',
            reply_read=bool(row.last_reply_message_id and flags.get('reply_read:' + row.id) == row.last_reply_message_id),
            action_done=bool(row.last_reply_message_id and flags.get('reply_action_done:' + row.id) == row.last_reply_message_id))
        items.append(item)
    return {'items': items, 'total': total, 'page': page, 'page_size': size, 'summary': totals(db)}


MILESTONES = {
    'uploaded_at': ['job_spreadsheet_imported', 'job_uploaded'],
    'enriched_at': ['auto_contact_email_found', 'contact_email_available', 'contact_enrichment_completed'],
    'drafted_at': ['auto_gmail_draft_created', 'gmail_draft_created', 'reviewed_gmail_draft_verified'],
    'first_sent_at': ['email_sent'],
    'reply_detected_at': ['reply_received', 'followup_stopped_on_reply'],
}


def job_logs(db, page=1, size=25, search='', batch='', start=None, end=None, event_type='', job_id=''):
    query = select(Job.id, Job.company, Job.title, Job.created_at)
    if job_id: query = query.where(Job.id == job_id)
    if search:
        pattern = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        contact_match = select(Contact.id).where(Contact.job_id == Job.id,
            or_(Contact.name.ilike(pattern, escape='\\'), Contact.email.ilike(pattern, escape='\\'))).exists()
        query = query.where(or_(Job.company.ilike(pattern, escape='\\'), Job.title.ilike(pattern, escape='\\'), contact_match))
    if batch:
        saved = db.get(KV, 'import_batch:' + batch)
        try:
            ids = json.loads(saved.value).get('job_ids', []) if saved else []
        except (ValueError, TypeError):
            ids = []
        query = query.where(Job.id.in_(ids))
    if start or end or event_type:
        match = select(Event.id).where(Event.job_id == Job.id)
        if start: match = match.where(Event.created_at >= start)
        if end: match = match.where(Event.created_at < end)
        if event_type: match = match.where(Event.action.in_(MILESTONES.get(event_type, [event_type])))
        query = query.where(match.exists())
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = min(page, max(1, (total + size - 1)//size))
    jobs = db.execute(query.order_by(Job.created_at.desc(), Job.id.desc()).offset((page-1)*size).limit(size)).all()
    ids = [j.id for j in jobs]
    actions = list({a for values in MILESTONES.values() for a in values})
    first = {(jid, action): at for jid, action, at in db.execute(select(Event.job_id, Event.action, func.min(Event.created_at))
        .where(Event.job_id.in_(ids), Event.action.in_(actions)).group_by(Event.job_id, Event.action))} if ids else {}
    items = []
    for job in jobs:
        item = {'id': job.id, 'company': job.company, 'title': job.title, 'record_created_at': stamp(job.created_at)}
        for name, values in MILESTONES.items():
            dates = [first[(job.id, a)] for a in values if (job.id, a) in first]
            item[name] = stamp(min(dates)) if dates else None
        items.append(item)
    return {'items': items, 'page': page, 'page_size': size, 'total': total}


def timeline(db, job_id, page=1, size=50):
    total = db.scalar(select(func.count()).select_from(Event).where(Event.job_id == job_id)) or 0
    events = db.execute(select(Event).where(Event.job_id == job_id)
        .order_by(Event.created_at.asc(), Event.id.asc()).offset((page-1)*size).limit(size)).scalars().all()
    contacts = {c.id: c.name for c in db.execute(select(Contact).where(Contact.job_id == job_id)).scalars()}
    links = dict(db.execute(select(Outreach.id, Outreach.contact_id).where(Outreach.job_id == job_id)).all())
    items = []
    for event in events:
        data = details(event)
        cid = data.get('contact_id') or links.get(event.outreach_id)
        # Explicit allowlist keeps provider responses, tokens and email bodies out of log UI.
        safe = {key: data[key] for key in ('filename', 'batch_id', 'source', 'status', 'error', 'detail',
                'attempt', 'due_at', 'reply_class', 'received_at', 'detected_at', 'model', 'prompt_fingerprint') if key in data}
        items.append({'id': event.id, 'action': event.action, 'at': stamp(event.created_at),
            'occurred_at': data.get('received_at') or stamp(event.created_at),
            'contact_id': cid, 'contact_name': contacts.get(cid, ''), 'details': safe})
    return {'items': items, 'total': total, 'page': page, 'page_size': size}
