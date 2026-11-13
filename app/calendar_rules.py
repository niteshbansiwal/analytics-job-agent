from __future__ import annotations
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from .config import settings

UTC = timezone.utc

def aware(dt: datetime) -> datetime:
    # SQLite can return UTC stored timestamps without tz info.
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

def parse_clock(s: str) -> time:
    return time.fromisoformat(s)

def is_workday(day: date, cfg: dict | None = None) -> bool:
    cfg = cfg or settings()
    return day.weekday() in cfg['email']['allowed_weekdays'] and day.isoformat() not in cfg['email']['holidays']

def next_fixed_send(after_utc: datetime, cfg: dict | None = None) -> datetime:
    """Return the first future fixed local send slot, skipping holidays/weekends."""
    cfg = cfg or settings()
    tz = ZoneInfo(cfg['timezone'])
    earliest = aware(after_utc).astimezone(tz)
    send_time = parse_clock(cfg['email']['preferred_send_time'])
    for offset in range(370):
        day = earliest.date() + timedelta(days=offset)
        if not is_workday(day, cfg):
            continue
        candidate = datetime.combine(day, send_time, tz)
        if candidate >= earliest:
            return candidate.astimezone(UTC)
    raise RuntimeError('Unable to locate an eligible email send day')

def next_working_moment(after_utc: datetime, cfg: dict | None = None) -> datetime:
    cfg = cfg or settings()
    tz = ZoneInfo(cfg['timezone'])
    earliest = aware(after_utc).astimezone(tz)
    start = parse_clock(cfg['email']['start'])
    end = parse_clock(cfg['email']['end'])
    for offset in range(370):
        day = earliest.date() + timedelta(days=offset)
        if not is_workday(day, cfg):
            continue
        lower = datetime.combine(day, start, tz)
        upper = datetime.combine(day, end, tz)
        if earliest <= upper:
            return max(earliest, lower).astimezone(UTC)
    raise RuntimeError('No eligible workday')

def plus_business_hours(start_utc: datetime, hours: int, cfg: dict | None = None) -> datetime:
    cfg = cfg or settings()
    if hours < 0:
        raise ValueError('hours must not be negative')
    spot = next_working_moment(start_utc, cfg)
    remaining = timedelta(hours=hours)
    tz = ZoneInfo(cfg['timezone'])
    for _ in range(1000):
        local = spot.astimezone(tz)
        close = datetime.combine(local.date(), parse_clock(cfg['email']['end']), tz).astimezone(UTC)
        capacity = close - spot
        if remaining <= capacity:
            return spot + remaining
        remaining -= capacity
        spot = next_working_moment(close + timedelta(seconds=1), cfg)
    raise RuntimeError('Business-hours calculation exceeded limit')

def nth_working_day_after(sent_at: datetime, n: int, cfg: dict | None = None,
                          extra_holidays: set[str] | frozenset[str] = frozenset()) -> datetime:
    """Return the fixed send slot on the n-th working day after the local send date.

    The send date itself never counts: a Monday email's 3rd working day is Thursday.
    Weekends, configured holidays and any extra (owner-managed) holidays are skipped."""
    cfg = cfg or settings()
    if n < 1:
        raise ValueError('n must be at least 1')
    tz = ZoneInfo(cfg['timezone'])
    day = aware(sent_at).astimezone(tz).date()
    send_time = parse_clock(cfg['email']['preferred_send_time'])
    counted = 0
    for _ in range(370):
        day += timedelta(days=1)
        if is_workday(day, cfg) and day.isoformat() not in extra_holidays:
            counted += 1
            if counted == n:
                return datetime.combine(day, send_time, tz).astimezone(UTC)
    raise RuntimeError('Unable to locate an eligible follow-up day')

def followup_due(sent_at: datetime, cfg: dict | None = None,
                 extra_holidays: set[str] | frozenset[str] = frozenset()) -> datetime:
    cfg = cfg or settings()
    rule = cfg['email']['followup']
    if rule.get('delay_type') == 'working_days':
        return nth_working_day_after(sent_at, int(rule['days']), cfg, extra_holidays)
    delay = int(rule['hours'])
    base = (plus_business_hours(sent_at, delay, cfg) if rule['delay_type'] == 'business'
            else aware(sent_at) + timedelta(hours=delay))
    return next_fixed_send(base, cfg)

def daily_search_due(now_utc: datetime, last_run_local_day: str, cfg: dict | None = None) -> bool:
    cfg = cfg or settings()
    local = aware(now_utc).astimezone(ZoneInfo(cfg['timezone']))
    search = cfg['search']
    return (search['enabled'] and local.weekday() in search['weekdays'] and
            local.time() >= parse_clock(search['at']) and local.date().isoformat() != last_run_local_day)
