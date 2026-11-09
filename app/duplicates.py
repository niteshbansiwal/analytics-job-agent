"""Conservative cross-source job deduplication.

Exact canonical posting/application links are duplicates. Similar company/title/
city across different links are review candidates, not automatically discarded:
employers often hire multiple people for one title in the same location.
"""
from __future__ import annotations
import re
from difflib import SequenceMatcher
from urllib.parse import urlsplit
from sqlalchemy import select
from .db import Job
from .discovery import canonical

_SUFFIXES = {'pvt', 'private', 'ltd', 'limited', 'inc', 'incorporated', 'llc', 'corp', 'corporation'}
_CITY = {
    'gurgaon': 'gurugram', 'gurugram': 'gurugram', 'delhi ncr': 'delhi ncr',
    'bangalore': 'bengaluru', 'bengaluru': 'bengaluru',
    'hyderabad': 'hyderabad', 'pune': 'pune', 'mumbai': 'mumbai',
    'noida': 'noida', 'delhi': 'delhi', 'chennai': 'chennai',
    'remote': 'remote',
}
def company_key(value: str) -> str:
    parts = re.findall(r'[a-z0-9]+', (value or '').lower())
    return ' '.join(x for x in parts if x not in _SUFFIXES)

def title_key(value: str) -> str:
    words = re.findall(r'[a-z0-9]+', (value or '').lower())
    words = [{'sr':'senior', 'mgr':'manager', 'analytics':'analytics', 'analyst':'analyst',
              'bi':'businessintelligence'}.get(w, w) for w in words]
    # Preserve seniority, specialty and all role tokens; sort only to equate
    # "Analytics Manager" and "Manager Analytics".
    return ' '.join(sorted(words))

def city_key(value: str) -> str:
    value = (value or '').lower().replace('–', '-')
    for name in sorted(_CITY, key=len, reverse=True):
        if re.search(r'(?<![a-z])'+re.escape(name)+r'(?![a-z])', value):
            return _CITY[name]
    return ''

def possible_matches(db, *, company: str, title: str, location: str,
                     post_url: str, apply_url: str = '', limit: int = 5):
    """Return (exact existing job, review candidates). Never silently merge
    same-title positions that may have distinct requisition IDs.
    """
    post = canonical(post_url)
    apply = canonical(apply_url) if apply_url else ''
    existing = db.execute(select(Job).where(Job.company.isnot(None), Job.status != 'archived')).scalars().all()
    candidates = []
    for job in existing:
        old_post = canonical(job.post_url)
        old_apply = canonical(job.apply_url) if job.apply_url else ''
        if post and post == old_post:
            return job, []
        if apply and (apply == old_post or (old_apply and apply == old_apply)):
            return job, []
        if not company_key(company) or company_key(company) != company_key(job.company):
            continue
        if not city_key(location) or city_key(location) != city_key(job.location):
            continue
        similarity = SequenceMatcher(None, title_key(title), title_key(job.title)).ratio()
        if similarity >= 0.94:
            candidates.append({'job_id': job.id, 'company': job.company,
                               'title': job.title, 'location': job.location,
                               'post_url': job.post_url, 'similarity': round(similarity, 3)})
            if len(candidates) >= limit:
                break
    return None, candidates
