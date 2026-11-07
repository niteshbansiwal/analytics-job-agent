from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from sqlalchemy import create_engine, ForeignKey, String, Text, Integer, DateTime, Float, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import ROOT, env, demo_mode

def now() -> datetime:
    return datetime.now(timezone.utc)

def uid() -> str:
    return str(uuid.uuid4())

class Base(DeclarativeBase):
    pass

class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(250))
    location: Mapped[str] = mapped_column(String(250), default='Not stated')
    arrangement: Mapped[str] = mapped_column(String(80), default='Not stated')
    experience: Mapped[str] = mapped_column(String(80), default='Not stated')
    salary: Mapped[str] = mapped_column(String(80), default='Not stated')
    source: Mapped[str] = mapped_column(String(100))
    post_url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str] = mapped_column(Text, default='')
    post_age: Mapped[str] = mapped_column(String(100), default='Not verified')
    posting_date_kind: Mapped[str] = mapped_column(String(40), default='unknown')
    author_name: Mapped[str] = mapped_column(String(200), default='')
    author_url: Mapped[str] = mapped_column(Text, default='')
    jd: Mapped[str] = mapped_column(Text, default='')
    fit_score: Mapped[float] = mapped_column(Float, default=0)
    fit_why: Mapped[str] = mapped_column(Text, default='')
    gaps: Mapped[str] = mapped_column(Text, default='')
    leadership: Mapped[str] = mapped_column(String(100), default='Unknown / check JD')
    verification: Mapped[str] = mapped_column(String(50), default='search_index_only')
    status: Mapped[str] = mapped_column(String(35), default='discovered')
    is_demo: Mapped[int] = mapped_column(Integer, default=0)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Contact(Base):
    __tablename__ = 'contacts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey('jobs.id'), index=True)
    name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(250), default='')
    category: Mapped[str] = mapped_column(String(80), default='Potential hiring-side contact')
    linkedin_url: Mapped[str] = mapped_column(Text, default='')
    evidence_url: Mapped[str] = mapped_column(Text, default='')
    reason: Mapped[str] = mapped_column(Text, default='')
    relationship: Mapped[str] = mapped_column(String(40), default='potential')
    email: Mapped[str] = mapped_column(String(200), default='')
    email_status: Mapped[str] = mapped_column(String(70), default='unavailable')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Outreach(Base):
    __tablename__ = 'outreach'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey('jobs.id'), index=True)
    contact_id: Mapped[str] = mapped_column(String(36), default='')
    to_email: Mapped[str] = mapped_column(String(200), default='')
    subject: Mapped[str] = mapped_column(Text, default='')
    body: Mapped[str] = mapped_column(Text, default='')
    resume_b64: Mapped[str] = mapped_column(Text, default='')
    resume_name: Mapped[str] = mapped_column(String(200), default='')
    gmail_draft_id: Mapped[str] = mapped_column(String(200), default='')
    gmail_thread_id: Mapped[str] = mapped_column(String(200), default='')
    sent_message_id: Mapped[str] = mapped_column(String(200), default='')
    status: Mapped[str] = mapped_column(String(40), default='pending_contact')
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reply_class: Mapped[str] = mapped_column(String(80), default='')
    last_reply_message_id: Mapped[str] = mapped_column(String(200), default='')
    followup_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class TelegramReview(Base):
    """Telegram-first candidate queue; never enters the Atlas jobs table until Excel upload."""
    __tablename__ = 'telegram_reviews'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(250))
    post_url: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Integration(Base):
    __tablename__ = 'integrations'
    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_email: Mapped[str] = mapped_column(String(200), default='')
    refresh_ciphertext: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Event(Base):
    __tablename__ = 'events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String(36), default='', index=True)
    outreach_id: Mapped[str] = mapped_column(String(36), default='')
    action: Mapped[str] = mapped_column(String(120))
    details: Mapped[str] = mapped_column(Text, default='{}')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class KV(Base):
    __tablename__ = 'kv'
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default='')

_engine = None
_Session = None

def engine():
    global _engine, _Session
    if _engine is None:
        url = env('DATABASE_URL') or f"sqlite:///{ROOT / 'data' / 'agent.db'}"
        if url.startswith('postgres://'):
            url = 'postgresql+psycopg://' + url[len('postgres://'):]
        elif url.startswith('postgresql://'):
            url = 'postgresql+psycopg://' + url[len('postgresql://'):]
        if not demo_mode() and url.startswith('sqlite') and env('VERCEL'):
            raise RuntimeError('Production requires persistent PostgreSQL DATABASE_URL.')
        kwargs = {'connect_args': {'check_same_thread': False}} if url.startswith('sqlite') else {'pool_pre_ping': True}
        _engine = create_engine(url, **kwargs)
        Base.metadata.create_all(_engine)
        _Session = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine

@contextmanager
def session():
    engine()
    db = _Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def log(db, action: str, job_id: str = '', outreach_id: str = '', **details):
    db.add(Event(action=action, job_id=job_id, outreach_id=outreach_id, details=json.dumps(details, default=str)))

def get_kv(db, key: str, default=''):
    entry = db.get(KV, key)
    return entry.value if entry else default

def set_kv(db, key: str, val: str):
    entry = db.get(KV, key)
    if entry:
        entry.value = val
    else:
        db.add(KV(key=key, value=val))

# Fictional records are visibly marked as samples, and their URLs are examples, not vacancy links.
def seed_demo(db):
    if not demo_mode() or db.query(Job).count():
        return
    data = [
        ('Northstar Payments (DEMO)', 'Analytics Manager - Commercial Strategy', 'Gurugram', 9.3, 'Payments, SQL, forecasting and commercial analytics'),
        ('Helix Financial (DEMO)', 'Risk Analytics Manager', 'Bengaluru', 9.0, 'Fraud, risk, decisioning and data products'),
        ('Orion Commerce (DEMO)', 'Senior Manager, Business Intelligence', 'Hyderabad', 8.8, 'Leading dashboards, stakeholders and analysts'),
        ('Summit Networks (DEMO)', 'Manager, Revenue Analytics', 'Noida', 8.5, 'Pricing, margins, revenue and automation'),
        ('Bluepeak Insights (DEMO)', 'Analytics Consulting Manager', 'Mumbai', 8.2, 'Consulting, forecasting and cross-functional delivery'),
    ]
    for company, title, city, score, fit in data:
        db.add(Job(company=company, title=title, location=city, arrangement='Hybrid (sample)', experience='7-10 years (sample)',
            source='SAMPLE - NOT A REAL POST', post_url='', post_age='Sample only - no posting date',
            verification='fictional_demo', jd='Fictional demonstration vacancy. No real employer or open position is claimed.',
            fit_score=score, fit_why=fit, gaps='Source/job description must be verified before live use.', leadership='Managerial (sample)', is_demo=1))
