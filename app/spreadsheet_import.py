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
from .duplicates import possible_matches

MAX_FILE_BYTES = 2_000_000
MAX_ROWS = 250

HEADERS = (
    'company', 'title', 'location', 'post_url', 'source', 'posted_at',
    'post_age', 'author_name', 'author_url', 'apply_url', 'jd',
    'jd_source_url', 'jd_access_status', 'fit_why',
    'contact_1_name', 'contact_1_title', 'contact_1_type', 'contact_1_linkedin',
    'contact_1_evidence', 'contact_1_email',
    'contact_2_name', 'contact_2_title', 'contact_2_type', 'contact_2_linkedin',
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
    # Human-readable ChatGPT approval exports (November 2026).
    'job_source_url': 'post_url',
    'posting_date_evidence': 'post_age',
    'contact_name': 'contact_1_name',
    'contact_role': 'contact_1_title',
    'linkedin_url': 'contact_1_linkedin',
    'fit_notes': 'fit_why',
    'fit_match': 'fit_why',
    'contact_association': 'contact_1_association',
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


REQUIRED_HEADERS = frozenset(('company', 'title', 'post_url'))


def _has_job_headers(values) -> bool:
    return REQUIRED_HEADERS.issubset({normalize_header(v) for v in values})


def _rows_from_values(values):
    """Accept both exact Atlas Jobs headers and ChatGPT's Approved Jobs layout.

    Search the first 20 rows for a header, ignoring title/metadata lines. An
    explicit Approval column is treated as a safety filter, not an instruction
    to import rejected or pending jobs.
    """
    it = iter(values)
    header_row, headers = None, None
    for number, values in enumerate(it, start=1):
        if number > 20:
            break
        if _has_job_headers(values):
            header_row = number
            headers = [normalize_header(h) for h in values]
            break
    if headers is None:
        raise ValueError(
            'Could not find job column headings in the first 20 rows. '
            'Expected company, title and post_url columns (also accepts Company, Role, Job Source URL). '
            'The Atlas template and ChatGPT Approved Jobs exports are both supported.'
        )
    if len([h for h in headers if h]) != len(set(h for h in headers if h)):
        raise ValueError('Duplicate column names found in the job header row.')
    result = []
    for number, values in enumerate(it, start=header_row + 1):
        if not any(cell_text(v) for v in values):
            continue
        item = {h: cell_text(v) for h, v in zip(headers, values) if h}
        # Only approved rows may cross from ChatGPT into Atlas.
        if 'approval' in headers and item.get('approval', '').strip().casefold() not in (
                'approved', 'approve', 'yes', 'true', '1'):
            continue
        if not any(item.get(k) for k in REQUIRED_HEADERS):
            continue  # trailing notes/metadata are not jobs
        if len(result) >= MAX_ROWS:
            raise ValueError(f'Maximum {MAX_ROWS} non-empty approved job rows per import.')
        result.append((number, item))
    if not result:
        raise ValueError('The file has column headings but no job rows marked Approved. Check the Approval column or use the Atlas Jobs template.')
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
            # Prefer the Atlas sheet, then ChatGPT's human-readable approval sheet.
            names = [n for n in ('Jobs', 'Approved Jobs') if n in wb.sheetnames]
            names += [n for n in wb.sheetnames if n not in names]
            for name in names:
                ws = wb[name]
                for number, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    if number > 20:
                        break
                    if _has_job_headers(row):
                        return _rows_from_values(ws.iter_rows(values_only=True))
            return _rows_from_values(wb[names[0]].iter_rows(values_only=True))
        finally:
            wb.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'Could not read XLSX file ({type(exc).__name__}). Use the Atlas template.') from exc


def import_rows(db, rows):
    created, duplicates, skipped, contacts_created = 0, 0, 0, 0
    errors, ids, seen = [], [], set()
    possible_duplicates, duplicate_candidates = 0, []
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
        location = item.get('location', '').strip()[:180] or 'Not stated'
        exact, candidates = possible_matches(
            db, company=company, title=title, location=location,
            post_url=url, apply_url=item.get('apply_url', ''))
        if exact:
            duplicates += 1
            continue
        if candidates:
            possible_duplicates += 1
            duplicate_candidates.append({'row': number, 'company': company,
                                         'title': title, 'matches': candidates})
        jd = item.get('jd', '').strip()[:12000]
        jd_source = _public_https_url(item.get('jd_source_url', ''))
        jd_access = item.get('jd_access_status', '').strip().lower()
        if jd_access == 'full' and len(jd) < 200:
            skipped += 1
            errors.append(f'Row {number}: full JD requires at least 200 source-grounded characters in jd.')
            continue
        if jd_access and jd_access not in ('full', 'partial', 'blocked'):
            errors.append(f'Row {number}: jd_access_status must be full, partial or blocked.')
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
            jd=jd, fit_score=score, fit_why=why,
            gaps=gaps + (f'; JD source: {jd_source}' if jd_source else ''),
            verification='chatgpt_spreadsheet_unverified',
            leadership='Manager title; duties unverified',
            status='duplicate_review' if candidates else 'discovered',
        )
        db.add(job)
        db.flush()
        ids.append(job.id)
        created += 1
        log(db, 'job_spreadsheet_imported', job.id, source_url=url, row=number,
            research_source=item.get('source', '')[:150], jd_source=jd_source,
            jd_access=jd_access, evidence='owner_uploaded_spreadsheet')
        if candidates:
            log(db, 'possible_duplicate_requires_review', job.id,
                candidate_ids=[x['job_id'] for x in candidates], row=number)
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
            contact_type = item.get(prefix + 'type', '').strip().lower()
            is_poster = bool(author_url and profile and author_url == profile)
            if is_poster:
                contact_type = 'poster'
            if contact_type not in ('poster', 'hr', 'senior_analytics', ''):
                errors.append(f'Row {number}: contact {index} type unknown; use poster, hr or senior_analytics.')
                contact_type = ''
            db.add(Contact(
                job_id=job.id, name=name, title=item.get(prefix + 'title', '').strip()[:200],
                category={'poster': 'Original job poster', 'hr': 'Talent acquisition contact',
                          'senior_analytics': 'Senior analytics professional'}.get(
                              contact_type, 'Potential hiring-side contact (spreadsheet)'),
                linkedin_url=profile, evidence_url=evidence,
                relationship='poster' if is_poster else 'potential',
                reason=('Original posting names this author; verify before outreach' if is_poster
                        else 'User-supplied ChatGPT research; hiring relationship unverified'),
                email=email, email_status='user_supplied_unverified' if email else 'unavailable',
            ))
            contacts_created += 1
    return {
        'created': created, 'duplicates': duplicates, 'skipped': skipped,
        'possible_duplicates': possible_duplicates,
        'duplicate_candidates': duplicate_candidates[:20],
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
    ws.auto_filter.ref = f'A1:{ws.cell(1, len(HEADERS)).column_letter}1'
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
            'jd_source_url',
        ) else (70 if col[0].value == 'jd' else 22)
    notes = wb.create_sheet('Instructions')
    instructions = [
        ('ATLAS · CHATGPT JOB RESEARCH IMPORT',),
        ('One row per real job. The Jobs sheet is the only sheet imported.',),
        ('Required columns: company, title, post_url. Include a detailed source-grounded JD for useful email drafts.',),
        ('Use an original public HTTPS URL for each job. Do not invent URLs.',),
        ('posted_at: exact ISO date/time only when supported by the original source.',),
        ('If a posting time is unknown, leave posted_at blank; do not estimate.',),
        ('jd: transcribe the accessible responsibilities, qualifications and team context. No invented text. Limit 12,000 characters.',),
        ('jd_source_url: original JD page; jd_access_status: full, partial or blocked. Full requires at least 200 characters.',),
        ('author_name/author_url: only when the actual hiring post author is known.',),
        ('If poster is verified, put that person in contact_1 as type poster; leave contact_2 blank.',),
        ('Otherwise contact_1 is HR (type hr), contact_2 is senior analytics (type senior_analytics).',),
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
