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
