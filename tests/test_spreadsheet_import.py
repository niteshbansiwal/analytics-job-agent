"""Offline tests for ChatGPT spreadsheet import. No paid APIs or network access."""
import csv
import io
import os

os.environ['DEMO_MODE'] = 'true'

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base, Job, Contact
from app.spreadsheet_import import HEADERS, parse_csv, parse_file, import_rows, template_xlsx


def sample_row(url='https://www.linkedin.com/jobs/view/123456'):
    return {
        'company': 'Example Payments', 'title': 'Manager - Analytics',
        'location': 'Gurugram', 'post_url': url, 'posted_at': '',
        'contact_1_name': 'Sample Contact', 'contact_1_title': 'Director, Analytics',
        'contact_1_linkedin': 'https://www.linkedin.com/in/sample-contact',
    }


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_template_round_trip():
    raw = template_xlsx()
    rows = parse_file(raw, 'atlas.xlsx') if False else None
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw), read_only=True)
    assert wb.sheetnames == ['Jobs', 'Instructions']
    assert list(next(wb['Jobs'].values)) == list(HEADERS)
    wb.close()


def test_csv_import_and_dedup(db):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(HEADERS))
    writer.writeheader()
    writer.writerow(sample_row())
    parsed = parse_csv(output.getvalue())
    first = import_rows(db, parsed)
    db.flush()
    second = import_rows(db, parsed)
    assert first['created'] == 1
    assert first['contacts_created'] == 1
    assert second['created'] == 0
    assert second['duplicates'] == 1
    job = db.execute(select(Job)).scalar_one()
    contact = db.execute(select(Contact)).scalar_one()
    assert job.verification == 'chatgpt_spreadsheet_unverified'
    assert contact.relationship == 'potential'
    assert job.author_name == ''
    assert job.post_url == 'https://www.linkedin.com/jobs/view/123456'


def test_bad_urls_are_skipped(db):
    rows = [
        (2, sample_row('http://example.com/job')),
        (3, sample_row('https://localhost/job')),
        (4, sample_row('https://127.0.0.1/private')),
        (5, sample_row('javascript:alert(1)')),
    ]
    result = import_rows(db, rows)
    assert result['created'] == 0
    assert result['skipped'] == 4


def test_contact_without_profile_or_evidence_is_omitted(db):
    row = sample_row()
    row['contact_1_linkedin'] = ''
    result = import_rows(db, [(2, row)])
    assert result['created'] == 1
    assert result['contacts_created'] == 0
    assert any('contact 1 omitted' in e for e in result['errors'])


def test_invalid_headers_rejected():
    with pytest.raises(ValueError, match='company, title and post_url'):
        parse_csv('employer_name,title,link\nAcme,Analytics Manager,https://example.com/job')


def test_csv_with_duplicate_headers_rejected():
    with pytest.raises(ValueError, match='Duplicate'):
        parse_csv('company,title,post_url,company\nAcme,Analytics Manager,https://example.com/job,Acme')


def test_no_job_rows_rejected():
    with pytest.raises(ValueError, match='no job rows'):
        parse_csv('company,title,post_url\n')


def test_csv_unicode_and_quoted_commas():
    parsed = parse_csv('company,title,post_url,location\n"Acme, Inc","Senior Manager, Analytics",https://example.com/jobs/1,"Gurugram, India"\n')
    assert parsed[0][1]['company'] == 'Acme, Inc'
    assert parsed[0][1]['title'] == 'Senior Manager, Analytics'
