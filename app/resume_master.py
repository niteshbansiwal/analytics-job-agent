"""Pixel-preserving one-page tailoring of the user's approved PDF master."""
from __future__ import annotations
import base64
import hashlib
import math
import re
import fitz
from .db import get_kv, set_kv
from .resume import resume_facts

MASTER_SHA256 = '9a20324249a5d23bd6024b96b6ad7d8b084dee20b21ac70bf2fc50a917138e80'
MASTER_NAME = 'Nitesh_Bansiwal_Resume_-_2026-08(1).pdf'
KV_KEY = 'approved_resume_master_pdf_b64'
# Exact project regions of the original US Letter PDF. All other page objects remain untouched.
SECTIONS = (
    ('American Express', 286, 453, 7, 295.61, 449.3),
    ('Plivo', 482, 569, 7, 491.5, 564.1),
    ('ZS Associates', 597, 635, 3, 606.2, 630.35),
    ('Futures First', 664, 677, 1, 672.5, 672.5),
)
LOCKED = {'0': (6,), '3': (0,)}
MIN_PROJECTS = math.ceil(sum(s[3] for s in SECTIONS) * .80)  # 15/18


def _projects(page, low, high, bulleted):
    lines = [line for block in page.get_text('dict')['blocks']
             for line in block.get('lines', []) if low <= line['bbox'][1] < high]
    lines.sort(key=lambda line: (round(line['bbox'][1], 1), line['bbox'][0]))
    projects = []
    for line in lines:
        text = ''.join(s['text'] for s in line['spans']).strip()
        if bulleted and line['bbox'][0] < 60 and text.startswith('●'):
            projects.append({'segments': [], 'start_y': line['spans'][0]['origin'][1]})
        elif line['bbox'][0] >= 67 or not bulleted:
            if not projects:
                projects.append({'segments': [], 'start_y': line['spans'][0]['origin'][1]})
            segments = [(s['text'], 'Bold' in s['font']) for s in line['spans'] if s['text'].strip()]
            if not segments:
                continue
            if projects[-1]['segments']:
                prev = projects[-1]['segments'][-1][0]
                if not prev.endswith(('-', ' ')) and not segments[0][0].startswith(' '):
                    segments[0] = (' ' + segments[0][0], segments[0][1])
            projects[-1]['segments'].extend(segments)
    return projects


def validate_master(pdf: bytes) -> None:
    if len(pdf) > 1_000_000 or not pdf.startswith(b'%PDF'):
        raise ValueError('Upload the original one-page resume PDF (maximum 1 MB).')
    if hashlib.sha256(pdf).hexdigest() != MASTER_SHA256:
        raise ValueError('Use the exact approved Nitesh_Bansiwal_Resume_-_2026-08(1).pdf.')
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        if len(doc) != 1 or abs(doc[0].rect.width - 612) > .1 or abs(doc[0].rect.height - 792) > .1:
            raise ValueError('The approved master must be one US Letter page.')
        for i, (_, low, high, count, _, _) in enumerate(SECTIONS):
            if len(_projects(doc[0], low, high, i != 3)) != count:
                raise ValueError('Approved master PDF has an unexpected project layout.')


def save_master(db, pdf: bytes) -> None:
    validate_master(pdf)
    set_kv(db, KV_KEY, base64.b64encode(pdf).decode('ascii'))


def get_master(db) -> bytes | None:
    encoded = get_kv(db, KV_KEY, '')
    if not encoded:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
        validate_master(data)
        return data
    except Exception as exc:
        raise ValueError('Saved master PDF is invalid. Upload the approved PDF again.') from exc


def master_selection(pdf: bytes, jd: str = '', title: str = '') -> dict:
    validate_master(pdf)
    info = resume_facts()
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        positions = []
        for i, (company, low, high, _, _, _) in enumerate(SECTIONS):
            projects = _projects(doc[0], low, high, i != 3)
            positions.append({
                'index': i, 'company': company,
                'title': info['experience'][i]['title'], 'dates': info['experience'][i]['dates'],
                'bullets': [{'index': j, 'text': ''.join(t for t, _ in p['segments']).strip(),
                             'selected': True, 'locked': j in LOCKED.get(str(i), ())}
                            for j, p in enumerate(projects)]
            })
    return {'source': MASTER_NAME, 'positions': positions, 'all_claims_from_master_resume': True,
            'original_layout_preserved': True, 'minimum_original_projects': MIN_PROJECTS,
            'original_project_count': 18, 'page_count': 1}


def validate_selection(selection: dict | None) -> dict[str, list[int]]:
    if selection is None:
        return {str(i): list(range(s[3])) for i, s in enumerate(SECTIONS)}
    if not isinstance(selection, dict) or set(selection) != {'0', '1', '2', '3'}:
        raise ValueError('Project selection must include all four employers.')
    chosen = {}
    for i, (_, _, _, count, _, _) in enumerate(SECTIONS):
        key = str(i)
        values = selection[key]
        if (not isinstance(values, list) or not values or
                any(type(v) is not int or v < 0 or v >= count for v in values)
                or len(set(values)) != len(values)):
            raise ValueError('Invalid, duplicate or empty project selection.')
        if not set(LOCKED.get(key, ())).issubset(values):
            raise ValueError('Core leadership and earlier-career achievements cannot be removed.')
        chosen[key] = sorted(values)
    if sum(len(v) for v in chosen.values()) < MIN_PROJECTS:
        raise ValueError('Keep at least 80% of the original project bullets (15 of 18).')
    return chosen


def _wrap(segments, regular, bold, width=509):
    lines, used = [[]], 0.
    for text, is_bold in segments:
        for token in re.findall(r'\s*\S+\s*', text):
            value = token.lstrip() if not lines[-1] else token
            font = bold if is_bold else regular
            length = font.text_length(value, fontsize=9.48)
            if used + length > width and lines[-1]:
                lines.append([])
                used = 0.
                value = token.lstrip()
                length = font.text_length(value, fontsize=9.48)
            if length > width:
                raise ValueError('A project line cannot fit the original layout.')
            lines[-1].append((value, is_bold))
            used += length
    return lines


def render_master(pdf: bytes, selection: dict | None = None) -> bytes:
    validate_master(pdf)
    chosen = validate_selection(selection)
    if all(chosen[str(i)] == list(range(s[3])) for i, s in enumerate(SECTIONS)):
        return pdf  # No changes: exact original PDF bytes and layout.
    doc = fitz.open(stream=pdf, filetype='pdf')
    page = doc[0]
    originals = [_projects(page, low, high, i != 3)
                 for i, (_, low, high, _, _, _) in enumerate(SECTIONS)]
    total_chars = sum(len(''.join(text for text, _ in project['segments']))
                      for section in originals for project in section)
    kept_chars = sum(len(''.join(text for text, _ in originals[i][j]['segments']))
                     for i in range(len(SECTIONS)) for j in chosen[str(i)])
    if kept_chars < total_chars * .80:
        raise ValueError('Keep at least 80% of the original project text; select more core projects.')

    regular_bytes, bold_bytes = doc.extract_font(9)[3], doc.extract_font(5)[3]
    regular, bold = fitz.Font(fontbuffer=regular_bytes), fitz.Font(fontbuffer=bold_bytes)
    updates = {}
    for i, (_, low, high, count, first, last) in enumerate(SECTIONS):
        selected = chosen[str(i)]
        if selected == list(range(count)):
            continue
        blocks = [_wrap(originals[i][j]['segments'], regular, bold) for j in selected]
        total_lines = sum(len(b) for b in blocks)
        if total_lines < 2:
            raise ValueError('Too few projects to preserve the original page fullness.')
        leading = 11.6
        gap = (last - first - (total_lines - 1) * leading) / max(1, len(blocks) - 1)
        if gap < .8:
            leading = max(9.48, (last - first - .8 * (len(blocks) - 1)) / max(1, total_lines - 1))
            gap = .8
        elif gap > 10:
            leading = min(15., (last - first - 10 * (len(blocks) - 1)) / max(1, total_lines - 1))
            gap = (last - first - (total_lines - 1) * leading) / max(1, len(blocks) - 1)
        if (leading < 9.48 or gap < 0 or
                first + (total_lines - 1) * leading + (len(blocks) - 1) * gap > high - 2):
            raise ValueError('Selected projects do not fit the original one-page layout.')
        page.add_redact_annot(fitz.Rect(40 if i == 3 else 49, low - 1, 582, high + .1),
                              fill=(1, 1, 1), cross_out=False)
        updates[i] = (blocks, first, leading, gap)
    page.apply_redactions(images=0, graphics=0, text=0)
    # Re-embed fonts from the source PDF; never distribute standalone font files.
    page.insert_font(fontname='AtlasCalibri', fontbuffer=regular_bytes)
    page.insert_font(fontname='AtlasCalibriBold', fontbuffer=bold_bytes)
    for i, (blocks, first, leading, gap) in updates.items():
        y = first
        for bi, lines in enumerate(blocks):
            if i != 3:
                page.insert_text((50.04, y), '●', fontname='AtlasCalibri', fontsize=9.48)
            for li, line in enumerate(lines):
                x = 42 if i == 3 else 68.064
                for value, is_bold in line:
                    page.insert_text((x, y), value,
                                     fontname='AtlasCalibriBold' if is_bold else 'AtlasCalibri',
                                     fontsize=9.48)
                    x += (bold if is_bold else regular).text_length(value, fontsize=9.48)
                if li != len(lines) - 1:
                    y += leading
            if bi != len(blocks) - 1:
                y += leading + gap
    output = doc.tobytes(garbage=4, deflate=True)
    with fitz.open(stream=output, filetype='pdf') as verified:
        if len(verified) != 1 or abs(verified[0].rect.width - 612) > .1 or abs(verified[0].rect.height - 792) > .1:
            raise ValueError('Resume must remain exactly one page.')
        text = verified[0].get_text()
        for anchor in ('PROFESSIONAL SUMMARY', 'TECHNICAL SKILLS', 'PROFESSIONAL EXPERIENCE',
                       'American Express', 'Plivo', 'ZS Associates', 'Futures First',
                       'EDUCATION', 'Indian Institute of Technology'):
            if anchor not in text:
                raise ValueError('Resume structure validation failed: ' + anchor)
        bottom = max(block[3] for block in verified[0].get_text('blocks'))
        if bottom < 710 or bottom > 744:
            raise ValueError('Resume page fullness validation failed.')
    return output
