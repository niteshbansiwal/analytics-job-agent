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


# Regression: the ChatGPT task produced this human-friendly layout, which
# previously failed with 'first row must include company, title, post_url'.
def test_chatgpt_approved_jobs_sheet_imports_without_manual_reformatting(db):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Read Me'
    ws.append(['ATLAS APPROVAL EXPORT'])
    ws = wb.create_sheet('Approved Jobs')
    ws.append(['Job ID', 'Company', 'Role', 'Location', 'Match Score',
               'Approval', 'Job Source URL', 'Posting Date Evidence',
               'Posting Date Verified', 'Contact Name', 'Contact Role',
               'LinkedIn URL', 'Contact Association', 'Fit / Notes',
               'Application Status'])
    ws.append(['J2', 'Indus Insights', 'Manager / Senior Manager Analytics',
               'Gurugram', '9.5/10', 'Approved',
               'https://in.linkedin.com/jobs/view/4386654436',
               'LinkedIn displayed 2 weeks ago', 'No', 'Anjali Yadav',
               'Associate Manager, Talent Acquisition',
               'https://in.linkedin.com/in/anjali-yadav-97572017a',
               'Identified as job poster', 'BFSI analytics and SQL', 'Not applied'])
    ws.append(['J1', 'Not Approved Co', 'Analytics Manager', 'Delhi',
               '8/10', 'Rejected', 'https://example.com/jobs/not-approved',
               '', '', '', '', '', '', '', 'Not applied'])
    out = io.BytesIO()
    wb.save(out)
    parsed = parse_file(out.getvalue(), 'Atlas_Approved_Jobs.xlsx')
    assert len(parsed) == 1
    assert parsed[0][1]['company'] == 'Indus Insights'
    assert parsed[0][1]['title'] == 'Manager / Senior Manager Analytics'
    assert parsed[0][1]['contact_1_name'] == 'Anjali Yadav'
    assert parsed[0][1]['contact_1_linkedin'].startswith('https://')
    assert parsed[0][1]['post_age'] == 'LinkedIn displayed 2 weeks ago'
    result = import_rows(db, parsed)
    assert result['created'] == 1
    assert result['contacts_created'] == 1
    assert result['skipped'] == 0
    assert db.execute(select(Job)).scalar_one().company == 'Indus Insights'


def test_jobs_sheet_with_preamble_and_aliases():
    parsed = parse_csv('ATLAS approved jobs\nPrepared,2026-11-08\n\nCompany,Role,Job Source URL,Approval\nAcme,Analytics Manager,https://example.com/jobs/1,Approved\n')
    assert len(parsed) == 1
    assert parsed[0][0] == 5
    assert parsed[0][1]['post_url'] == 'https://example.com/jobs/1'


def test_rejected_rows_cannot_be_imported():
    with pytest.raises(ValueError, match='no job rows'):
        parse_csv('Company,Role,Job Source URL,Approval\\nAcme,Analytics Manager,https://example.com/jobs/1,Rejected\\n')


def test_exact_atlas_xlsx_contract_is_accepted():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Jobs'
    ws.append(HEADERS)
    values = sample_row()
    ws.append([values.get(h, '') for h in HEADERS])
    out = io.BytesIO()
    wb.save(out)
    parsed = parse_file(out.getvalue(), 'atlas_jobs.xlsx')
    assert len(parsed) == 1
    assert parsed[0][1]['company'] == 'Example Payments'
