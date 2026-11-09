from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest
from app.db import Job, Outreach, session
from app.scheduling import schedule_slot, save_holidays, holidays

IST = ZoneInfo('Asia/Kolkata')

def test_holiday_moves_pending_scheduled_item_to_next_workday(monkeypatch):
    monkeypatch.setattr('app.scheduling.now', lambda: datetime(2026, 11, 9, tzinfo=timezone.utc))
    with session() as db:
        job = Job(company='Example', title='Analytics Manager', source='test', post_url='https://example.com/job')
        db.add(job)
        db.flush()
        row = Outreach(job_id=job.id, status='scheduled', gmail_draft_id='draft-1',
                       due_at=datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc))
        db.add(row)
        db.flush()
        dates, shifted = save_holidays(db, ['2026-11-10'])
        assert dates == ['2026-11-10']
        assert shifted == [row.id]
        assert row.due_at.astimezone(IST).strftime('%Y-%m-%d %H:%M') == '2026-11-11 10:30'
        assert holidays(db) == ['2026-11-10']

def test_requested_slot_preserves_clock_when_holiday(monkeypatch):
    monkeypatch.setattr('app.scheduling.now', lambda: datetime(2026, 11, 9, tzinfo=timezone.utc))
    with session() as db:
        save_holidays(db, ['2026-11-10'])
        slot = schedule_slot(db, datetime(2026, 11, 10, 14, 0, tzinfo=IST))
        assert slot.astimezone(IST).strftime('%Y-%m-%d %H:%M') == '2026-11-11 14:00'
        with pytest.raises(ValueError, match='Monday'):
            schedule_slot(db, datetime(2026, 11, 14, 14, 0, tzinfo=IST))
