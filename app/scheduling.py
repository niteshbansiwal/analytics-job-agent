from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .config import settings
from .db import Outreach, get_kv, set_kv, log, now

IST = ZoneInfo('Asia/Kolkata')
HOLIDAYS_KEY = 'email_holidays'

def holidays(db):
    raw = get_kv(db, HOLIDAYS_KEY, '')
    return sorted({date.fromisoformat(s).isoformat() for s in raw.split(',') if s})

def valid_slot(local: datetime):
    cfg = settings()['email']
    if local.weekday() not in cfg['allowed_weekdays']:
        raise ValueError('Choose a Monday–Friday send time.')
    if not time.fromisoformat(cfg['start']) <= local.time().replace(second=0, microsecond=0) <= time.fromisoformat(cfg['end']):
        raise ValueError('Choose a time within the configured sending window (IST).')

def shift_holiday(local: datetime, days: set[str]):
    for _ in range(370):
        if local.weekday() in settings()['email']['allowed_weekdays'] and local.date().isoformat() not in days:
            return local
        local += timedelta(days=1)
    raise ValueError('No available workday in the next year.')

def schedule_slot(db, requested: datetime):
    if requested.tzinfo is None:
        raise ValueError('Schedule needs an explicit timezone offset.')
    local = requested.astimezone(IST)
    valid_slot(local)
    local = shift_holiday(local, set(holidays(db)))
    utc = local.astimezone(timezone.utc)
    if utc <= now() + timedelta(minutes=2):
        raise ValueError('Choose a send time at least two minutes from now.')
    return utc

def manual_schedule_slot(requested: datetime):
    if requested.tzinfo is None:
        raise ValueError('Schedule needs an explicit timezone offset.')
    utc = requested.astimezone(timezone.utc)
    if utc <= now():
        raise ValueError('Choose a future send time.')
    return utc


def is_manual(db, outreach_id: str) -> bool:
    return get_kv(db, 'manual_schedule:' + outreach_id, '') == 'true'


def save_holidays(db, dates: list[str]):
    days = sorted({date.fromisoformat(value).isoformat() for value in dates})
    if len(days) > 200:
        raise ValueError('Keep at most 200 holiday dates.')
    set_kv(db, HOLIDAYS_KEY, ','.join(days))
    shifted = []
    rows = db.execute(select(Outreach).where(Outreach.status == 'scheduled', Outreach.due_at.is_not(None)).with_for_update()).scalars().all()
    for row in rows:
        if is_manual(db, row.id):
            continue
        original = row.due_at.replace(tzinfo=timezone.utc) if row.due_at.tzinfo is None else row.due_at
        local = original.astimezone(IST)
        new_local = shift_holiday(local, set(days))
        if new_local != local:
            row.due_at = new_local.astimezone(timezone.utc)
            shifted.append(row.id)
            log(db, 'scheduled_email_moved_for_holiday', row.job_id, row.id, from_at=original, due_at=row.due_at)
    return days, shifted
