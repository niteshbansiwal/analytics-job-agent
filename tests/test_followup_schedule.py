"""Follow-ups: 3rd working day after the previous email, up to 3, IST fixed slot."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.calendar_rules import followup_due
from app.config import settings

IST = ZoneInfo('Asia/Kolkata')


def _due(local: datetime, extra=frozenset()):
    return followup_due(local, extra_holidays=extra).astimezone(IST).strftime('%a %Y-%m-%d %H:%M')


def test_production_config_enables_three_followups_on_third_working_day():
    rule = settings()['email']['followup']
    assert rule['enabled'] is True
    assert rule['delay_type'] == 'working_days' and rule['days'] == 3
    assert rule['max_attempts'] == 3 and rule['stop_on_any_reply'] is True


def test_monday_email_follows_up_thursday():
    assert _due(datetime(2026, 11, 16, 10, 30, tzinfo=IST)) == 'Thu 2026-11-19 10:30'


def test_thursday_and_friday_emails_skip_the_weekend():
    assert _due(datetime(2026, 11, 19, 10, 30, tzinfo=IST)) == 'Tue 2026-11-24 10:30'
    assert _due(datetime(2026, 11, 20, 14, 0, tzinfo=IST)) == 'Wed 2026-11-25 10:30'


def test_late_evening_send_still_counts_from_its_own_day():
    # 23:30 IST Monday is 18:00 UTC Monday; must count from the IST date.
    assert _due(datetime(2026, 11, 16, 23, 30, tzinfo=IST)) == 'Thu 2026-11-19 10:30'


def test_owner_holidays_are_skipped():
    assert _due(datetime(2026, 11, 16, 10, 30, tzinfo=IST), {'2026-11-18'}) == 'Fri 2026-11-20 10:30'


def test_three_followups_chain_from_each_previous_send():
    first = datetime(2026, 11, 16, 10, 30, tzinfo=IST)  # Mon
    f1 = followup_due(first)
    f2 = followup_due(f1)
    f3 = followup_due(f2)
    assert [d.astimezone(IST).strftime('%a %d') for d in (f1, f2, f3)] == ['Thu 19', 'Tue 24', 'Fri 27']


def _sent_row(db, *, sent_at, **over):
    from app.db import Job, Contact, Outreach, Integration
    job = Job(company='Tredence Inc.', title='Senior Data Science Manager', source='test', post_url='https://example.com/job')
    db.add(job); db.flush()
    c = Contact(job_id=job.id, name='Ritesh Tiwari', email='ritesh@example.com')
    db.add(c); db.flush()
    if not db.get(Integration, 'gmail'):
        db.add(Integration(provider='gmail', owner_email='owner@example.com'))
    fields = dict(job_id=job.id, contact_id=c.id, to_email=c.email, subject='Senior Data Science Manager',
                  status='awaiting_reply', sent_message_id='m1', gmail_thread_id='t1', sent_at=sent_at)
    fields.update(over)
    row = Outreach(**fields); db.add(row); db.commit()
    return row


def _mock_gmail(monkeypatch, drafts):
    from app import workflow
    monkeypatch.setattr(workflow, 'demo_mode', lambda: False)
    monkeypatch.setattr(workflow, 'gmail_get_message', lambda *a: {'payload': {'headers': [{'name': 'Message-ID', 'value': '<m1@x>'}]}})
    monkeypatch.setattr(workflow, 'gmail_create_draft', lambda *a: drafts.append(a) or {'id': f'd{len(drafts)}'})


def test_backfill_schedules_existing_sent_emails_and_skips_replied(monkeypatch):
    from app import workflow
    from app.db import session
    drafts = []
    _mock_gmail(monkeypatch, drafts)
    monkeypatch.setattr(workflow, 'now', lambda: datetime(2026, 11, 13, 11, 45, tzinfo=IST))
    with session() as db:
        fresh = _sent_row(db, sent_at=datetime(2026, 11, 13, 10, 44, tzinfo=IST))  # Fri
        replied = _sent_row(db, sent_at=datetime(2026, 11, 13, 10, 44, tzinfo=IST),
                            last_reply_at=datetime(2026, 11, 13, 11, 0, tzinfo=IST))
        result = workflow.schedule_missing_followups(db)
        assert result == {'scheduled': 1, 'failed': 0}
        assert fresh.status == 'followup_scheduled' and fresh.gmail_draft_id == 'd1'
        assert workflow.aware(fresh.due_at).astimezone(IST).strftime('%a %d %H:%M') == 'Wed 18 10:30'
        assert replied.status == 'awaiting_reply' and replied.gmail_draft_id == ''
        # Idempotent: a second tick does nothing more.
        assert workflow.schedule_missing_followups(db) == {'scheduled': 0, 'failed': 0}


def test_backfill_never_schedules_an_old_email_for_immediately(monkeypatch):
    from app import workflow
    from app.db import session
    drafts = []
    _mock_gmail(monkeypatch, drafts)
    monkeypatch.setattr(workflow, 'now', lambda: datetime(2026, 11, 13, 12, 0, tzinfo=IST))  # Fri noon
    with session() as db:
        old = _sent_row(db, sent_at=datetime(2026, 11, 2, 10, 30, tzinfo=IST))  # due Thu 5 Nov, long past
        workflow.schedule_missing_followups(db)
        assert workflow.aware(old.due_at).astimezone(IST).strftime('%a %d %H:%M') == 'Mon 16 10:30'
