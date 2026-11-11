"""Two owner-uploaded PDFs. Store and return original bytes; never render or tailor."""
import base64
import hashlib
import json
import fitz
from sqlalchemy import select
from .db import KV
from .db import get_kv, set_kv, now

KINDS = {'analytics': 'Analytics Professional', 'data_science': 'Data Science'}
MAX_BYTES = 4_000_000

def key(kind):
    if kind not in KINDS:
        raise ValueError('Choose Analytics Professional or Data Science.')
    return 'fixed_resume:' + kind

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def get_resume(db, kind):
    value = get_kv(db, key(kind), '')
    if not value:
        raise ValueError('Upload the selected resume in Configuration first.')
    try:
        item = json.loads(value)
        raw = base64.b64decode(item['pdf_b64'], validate=True)
        if digest(raw) != item['sha256']:
            raise ValueError('Stored resume integrity check failed.')
        return item, raw
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Stored resume is unavailable or invalid. Upload it again.') from exc

def list_resumes(db):
    # Metadata is separate from the multi-MB PDFs. Attachment retrieval still
    # verifies the original bytes and digest on every actual use.
    keys = ['fixed_resume_meta:'+kind for kind in KINDS]
    saved = dict(db.execute(select(KV.key, KV.value).where(KV.key.in_(keys))).all())
    result = []
    for kind,label in KINDS.items():
        metadata_key = 'fixed_resume_meta:'+kind
        try:
            if metadata_key in saved:
                item = json.loads(saved[metadata_key])
            else:
                item, _ = get_resume(db,kind)
                item = {k:v for k,v in item.items() if k != 'pdf_b64'}
                # Backfill legacy uploads once. Conflict-safe if two first reads
                # run concurrently; no PDF or existing metadata is overwritten.
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                insert = pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert
                db.execute(insert(KV).values(key=metadata_key,value=json.dumps(item))
                           .on_conflict_do_nothing(index_elements=['key']))
            result.append(item | {'configured':True})
        except (ValueError,TypeError,KeyError):
            result.append({'id':kind,'label':label,'configured':False})
    return result

def save_resume(db, kind, raw, *, expected_sha256=''):
    storage_key = key(kind)
    if not raw.startswith(b'%PDF-') or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Upload a valid PDF smaller than 4 MB.')
    try:
        with fitz.open(stream=raw, filetype='pdf') as doc:
            if doc.is_encrypted or doc.page_count < 1:
                raise ValueError('Use an unencrypted PDF with at least one page.')
            pages = doc.page_count
    except Exception as exc:
        raise ValueError('The PDF could not be read or is encrypted.') from exc
    previous = get_kv(db, storage_key, '')
    if previous and json.loads(previous)['sha256'] != expected_sha256:
        raise ValueError('Resume changed or replacement was not confirmed. Refresh and confirm replacement.')
    item = {'id': kind, 'label': KINDS[kind],
            'filename': 'Nitesh_Bansiwal_' + ('Analytics' if kind == 'analytics' else 'Data_Science') + '_Resume.pdf',
            'sha256': digest(raw), 'page_count': pages, 'bytes': len(raw),
            'uploaded_at': now().isoformat(), 'pdf_b64': base64.b64encode(raw).decode()}
    set_kv(db, storage_key, json.dumps(item))
    set_kv(db, 'fixed_resume_meta:'+kind, json.dumps({k:v for k,v in item.items() if k != 'pdf_b64'}))
    return {k: v for k, v in item.items() if k != 'pdf_b64'} | {'configured': True}
