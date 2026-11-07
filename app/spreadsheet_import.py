"""Import ChatGPT-supplied CSV/XLSX into Atlas without any paid AI API.

All imported evidence is user-supplied and UNVERIFIED. The importer does not
visit URLs, call LLMs, enrich contacts, send email, or overwrite approved jobs.
"""
from __future__ import annotations

import csv
import io
import ipaddress
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select

from .db import Job, Contact, log
from .discovery import canonical, ranking, source_type

MAX_FILE_BYTES = 2_000_000
MAX_ROWS = 250

HEADERS = (
    'company', 'title', 'location', 'post_url', 'source', 'posted_at',
    'post_age', 'author_name', 'author_url', 'apply_url', 'jd', 'fit_why',
    'contact_1_name', 'contact_1_title', 'contact_1_linkedin',
    'contact_1_evidence', 'contact_1_email',
    'contact_2_name', 'contact_2_title', 'contact_2_linkedin',
    'contact_2_evidence', 'contact_2_email',
)
ALIASES = {
    'job_title': 'title', 'role': 'title', 'job_role': 'title',
    'employer': 'company', 'organisation': 'company', 'organization': 'company',
    'job_link': 'post_url', 'job_url': 'post_url', 'posting_url': 'post_url',
    'original_post_url': 'post_url', 'source_url': 'post_url',
    'poster_name': 'author_name', 'poster_url': 'author_url',
    'posting_time': 'posted_at', 'posted_date': 'posted_at', 'post_date': 'posted_at',
    'job_description': 'jd', 'description': 'jd', 'match_reason': 'fit_why',
    'contact_1_profile': 'contact_1_linkedin', 'contact_2_profile': 'contact_2_linkedin',
    'contact_1_linkedin_url': 'contact_1_linkedin',
    'contact_2_linkedin_url': 'contact_2_linkedin',
}


def cell_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def normalize_header(value) -> str:
    header = re.sub(r'[^a-z0-9]+', '_', cell_text(value).lower()).strip('_')
    return ALIASES.get(header, header)


def _public_https_url(value: str) -> str:
    value = cell_text(value)
    if not value:
        return ''
    try:
        p = urlsplit(value)
        host = (p.hostname or '').lower()
        if p.scheme.lower() != 'https' or not host or '.' not in host or p.username or p.password:
            return ''
        if host == 'localhost' or host.endswith(('.local', '.internal')):
            return ''
        try:
            addr = ipaddress.ip_address(host)
            if not addr.is_global:
                return ''
        except ValueError:
            pass
        return canonical(value)
    except ValueError:
        return ''


def _rows_from_values(values):
    it = iter(values)
    try:
        headers = [normalize_header(h) for h in next(it)]
    except StopIteration:
        raise ValueError('The spreadsheet is empty.')
    if not {'company', 'title', 'post_url'}.issubset(set(headers)):
        raise ValueError('The first row must include company, title and post_url columns. Download the Atlas template.')
    if len(headers) != len(set(h for h in headers if h)):
        raise ValueError('Duplicate column names found in the header row.')
    result = []
    for number, values in enumerate(it, start=2):
        if not any(cell_text(v) for v in values):
            continue
        if len(result) >= MAX_ROWS:
            raise ValueError(f'Maximum {MAX_ROWS} non-empty job rows per import.')
        item = {h: cell_text(v) for h, v in zip(headers, values) if h}
        result.append((number, item))
    if not result:
        raise ValueError('The file has column headings but no job rows.')
    return result


def parse_csv(text: str):
    if len(text.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('CSV is too large (maximum 2 MB).')
    try:
        return _rows_from_values(csv.reader(io.StringIO(text.lstrip('\ufeff'), newline='')))
    except csv.Error as exc:
        raise ValueError(f'Invalid CSV format: {exc}') from exc


def parse_file(data: bytes, filename: str):
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError('Choose a non-empty file no larger than 2 MB.')
    suffix = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if suffix == 'csv':
        try:
            return parse_csv(data.decode('utf-8-sig'))
        except UnicodeDecodeError as exc:
            raise ValueError('CSV must be saved as UTF-8.') from exc
    if suffix != 'xlsx':
        raise ValueError('Supported file types: .xlsx and .csv only.')
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            ws = wb['Jobs'] if 'Jobs' in wb.sheetnames else wb.active
            return _rows_from_values(ws.iter_rows(values_only=True))
        finally:
            wb.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'Could not read XLSX file ({type(exc).__name__}). Use the Atlas template.') from exc


def import_rows(db, rows):
    created, duplicates, skipped, contacts_created = 0, 0, 0, 0
    errors, ids, seen = [], [], set()
    for number, item in rows:
        company = item.get('company', '').strip()[:180]
        title = item.get('title', '').strip()[:220]
        url = _public_https_url(item.get('post_url', ''))
        if len(company) < 2 or len(title) < 4 or not url:
            skipped += 1
            errors.append(f'Row {number}: company, title (4+ chars) and a real HTTPS post_url are required.')
            continue
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        old = db.execute(select(Job).where(Job.post_url == url)).scalars().first()
        if old:
            duplicates += 1
            continue
        location = item.get('location', '').strip()[:180] or 'Not stated'
        jd = item.get('jd', '').strip()[:6000]
        score, why, gaps = ranking(company, title, location, jd, url)
        research_note = item.get('fit_why', '').strip()[:600]
        if research_note:
            why += '; ChatGPT research note (unverified): ' + research_note
        posted_at = item.get('posted_at', '').strip()[:95]
        post_age = item.get('post_age', '').strip()[:95]
        author_url = _public_https_url(item.get('author_url', ''))
        apply_url = _public_https_url(item.get('apply_url', ''))
        job = Job(
            company=company, title=title, location=location,
            source=('ChatGPT spreadsheet / ' + source_type(url))[:100],
            post_url=url, apply_url=apply_url,
            post_age=posted_at or post_age or 'Not verified',
            posting_date_kind='spreadsheet_claim_unverified' if (posted_at or post_age) else 'unknown',
            author_name=item.get('author_name', '').strip()[:150], author_url=author_url,
            jd=jd, fit_score=score, fit_why=why, gaps=gaps,
            verification='chatgpt_spreadsheet_unverified',
            leadership='Manager title; duties unverified', status='discovered',
        )
        db.add(job)
        db.flush()
        ids.append(job.id)
        created += 1
        log(db, 'job_spreadsheet_imported', job.id, source_url=url, row=number,
            research_source=item.get('source', '')[:150], evidence='owner_uploaded_spreadsheet')
        for index in (1, 2):
            prefix = f'contact_{index}_'
            name = item.get(prefix + 'name', '').strip()[:120]
            profile = _public_https_url(item.get(prefix + 'linkedin', ''))
            evidence = _public_https_url(item.get(prefix + 'evidence', ''))
            if not name:
                continue
            if not (profile or evidence):
                errors.append(f'Row {number}: contact {index} omitted; add a LinkedIn profile or evidence URL.')
                continue
            email = item.get(prefix + 'email', '').strip()[:180]
            if email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
                errors.append(f'Row {number}: contact {index} email omitted (invalid format).')
                email = ''
            db.add(Contact(
                job_id=job.id, name=name, title=item.get(prefix + 'title', '').strip()[:200],
                category='Potential hiring-side contact (spreadsheet)',
                linkedin_url=profile, evidence_url=evidence,
                relationship='potential', reason='User-supplied ChatGPT research; hiring relationship unverified',
                email=email, email_status='user_supplied_unverified' if email else 'unavailable',
            ))
            contacts_created += 1
    return {
        'created': created, 'duplicates': duplicates, 'skipped': skipped,
        'contacts_created': contacts_created, 'rows_read': len(rows),
        'job_ids': ids[:20], 'errors': errors[:30],
        'note': 'All spreadsheet claims remain unverified. No messages or outreach were sent.',
    }


def template_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = 'Jobs'
    ws.append(HEADERS)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:V1'
    ws.row_dimensions[1].height = 32
    for col in ws[1]:
        col.font = Font(name='Aptos', size=10, bold=True, color='FFFFFF')
        col.fill = PatternFill('solid', fgColor='17253A')
        col.alignment = Alignment(wrap_text=True, vertical='center')
    for col in ws.columns:
        letter = col[0].column_letter
        ws.column_dimensions[letter].width = 35 if col[0].value in (
            'post_url', 'author_url', 'apply_url', 'contact_1_linkedin',
            'contact_2_linkedin', 'contact_1_evidence', 'contact_2_evidence',
        ) else (50 if col[0].value == 'jd' else 22)
    notes = wb.create_sheet('Instructions')
    instructions = [
        ('ATLAS · CHATGPT JOB RESEARCH IMPORT',),
        ('One row per real job. The Jobs sheet is the only sheet imported.',),
        ('Required columns: company, title, post_url. All others are optional.',),
        ('Use an original public HTTPS URL for each job. Do not invent URLs.',),
        ('posted_at: exact ISO date/time only when supported by the original source.',),
        ('If a posting time is unknown, leave posted_at blank; do not estimate.',),
        ('author_name/author_url: only when the actual hiring post author is known.',),
        ('contact_1/2: potential hiring managers or talent acquisition profiles, with URLs.',),
        ('Do not claim a potential contact is the confirmed hiring manager.',),
        ('No API keys needed. Upload the completed .xlsx or .csv from the Atlas dashboard.',),
        ('Importing never sends an email. Each job still requires owner approval.',),
    ]
    for row in instructions:
        notes.append(row)
    notes.column_dimensions['A'].width = 112
    notes['A1'].font = Font(size=14, bold=True, color='17253A')
    notes.freeze_panes = 'A2'
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
