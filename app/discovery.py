from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import env, settings
from .db import Job, Contact, log, get_kv, set_kv
from .integrations import rocketreach_lookup, send_telegram

SEARCH_QUERIES = [
  'site:linkedin.com/posts/ "hiring" "Analytics Manager" India',
  'site:linkedin.com/posts/ "my team is hiring" analytics India',
  'site:linkedin.com/posts/ "Senior Manager" "Analytics" hiring',
  'site:linkedin.com/feed/update/ "analytics manager" hiring',
  'site:linkedin.com/jobs/view/ "analytics manager" India',
  'site:naukri.com/job-listings "analytics manager"',
  'site:jobs.lever.co "analytics manager" India',
  'site:job-boards.greenhouse.io "analytics manager" India',
  'site:iimjobs.com "manager analytics"',
  'site:wellfound.com/jobs "analytics manager"',
  'site:instahyre.com "analytics manager"',
]

def search_public_index(query: str, count: int = 8) -> list[dict]:
    api_key = env('SERPER_API_KEY')
    if not api_key:
        raise RuntimeError('SERPER_API_KEY is missing. Public search cannot run.')
    with httpx.Client(timeout=30) as client:
        r = client.post('https://google.serper.dev/search',
                        headers={'X-API-KEY': api_key},
                        json={'q': query, 'gl': 'in', 'hl': 'en', 'num': count, 'tbs': 'qdr:m'})
        r.raise_for_status()
        return r.json().get('organic', [])

def canonical(url: str):
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), '', ''))

def llm_extract(item: dict) -> dict | None:
    """Use only indexed excerpt as evidence; never let model invent absent fields."""
    key = env('OPENAI_API_KEY')
    if not key:
        return strict_parse(item)
    prompt = f"""Extract a REAL CURRENT analytics manager vacancy only if company and role are explicitly present in the provided search-result title/snippet. Respond JSON object only. If unclear set valid=false. Never invent names, dates, titles, company, employment mode or experience. A post about advice or unrelated roles is not a vacancy. Fields: valid(bool), company(string), title(string), location(string or empty), arrangement(string or empty), experience(string or empty), salary(string or empty), poster_name(string or empty), displayed_post_age(string or empty), hiring_post(bool), rationale(string). Do not infer poster if none is explicit. Search date metadata is an INDEX date, not necessarily original job posting date.\nSOURCE TITLE: {item.get('title','')}\nSOURCE SNIPPET: {item.get('snippet','')}\nSOURCE DATE: {item.get('date','')}"""
    try:
        with httpx.Client(timeout=40) as client:
            r = client.post('https://api.openai.com/v1/chat/completions',
                            headers={'Authorization': f'Bearer {key}'},
                            json={'model': env('OPENAI_MODEL', 'gpt-4.1-mini'),
                                  'messages': [{'role': 'system', 'content': 'Strict evidence extraction from short web snippets. Return valid JSON only.'},
                                               {'role': 'user', 'content': prompt}],
                                  'response_format': {'type': 'json_object'}, 'temperature': 0})
            r.raise_for_status()
            data = json.loads(r.json()['choices'][0]['message']['content'])
            return data if data.get('valid') else None
    except (httpx.HTTPError, ValueError, KeyError):
        return strict_parse(item)

def strict_parse(item: dict) -> dict | None:
    title = item.get('title', '')
    # Never guess company from LinkedIn author's account, or role from a vague post.
    patterns = [
        r'(?i)^(?P<role>(?:senior\s+)?(?:manager[, -]+)?(?:analytics|business analytics|data analytics|risk analytics|bi)[^\-|]{0,65})\s*[-|]\s*(?P<company>[^\-|]{3,70})',
        r'(?i)^(?P<company>[^\-|]{3,70})\s*[-|]\s*(?P<role>(?:senior\s+)?(?:analytics manager|manager analytics|risk analytics manager|business intelligence manager)[^\-|]{0,50})',
    ]
    for pattern in patterns:
        m = re.search(pattern, title)
        if m:
            company = m.group('company').replace('LinkedIn', '').strip(' .:-')
            role = m.group('role').strip(' .:-')
            if len(company) >= 3 and len(role) >= 6 and company.lower() not in {'jobs', 'hiring', 'india'}:
                return {'valid': True, 'company': company, 'title': role,
                        'location': '', 'arrangement': '', 'experience': '', 'salary': '',
                        'poster_name': '', 'displayed_post_age': '', 'hiring_post': False}
    return None

def source_type(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if 'linkedin.com' in host:
        return 'LinkedIn hiring post' if ('/posts/' in url or '/feed/update/' in url) else 'LinkedIn Jobs'
    for word in ['naukri', 'instahyre', 'wellfound', 'iimjobs', 'greenhouse', 'lever']:
        if word in host:
            return word.title()
    return 'Company / indexed careers page'

def ranking(company: str, title: str, location: str, desc: str, post_url: str) -> tuple[float, str, str]:
    source = (title + ' ' + desc).lower()
    points = 5.2
    matches = []
    for needle, weight, reason in [
        ('manager', 1.2, 'Manager-level title'),
        ('analytics', .9, 'Direct analytics relevance'),
        ('risk', .35, 'Financial/risk analytics'), ('payments', .35, 'Payments domain'),
        ('fraud', .35, 'Fraud detection experience'), ('sql', .2, 'Advanced SQL'),
        ('tableau', .2, 'BI dashboard experience'), ('python', .2, 'Python analytics'),
        ('genai', .35, 'Applied GenAI experience'), ('lead', .25, 'Team leadership'),
    ]:
        if needle in source:
            points += weight
            matches.append(reason)
    l = location.lower()
    if any(x in l for x in ['gurugram', 'gurgaon', 'delhi', 'noida', 'ncr']):
        points += .5
    elif any(x in l for x in ['bengaluru', 'hyderabad', 'mumbai', 'pune']):
        points += .2
    if '/posts/' in post_url or '/feed/update/' in post_url:
        points += .25
    return min(9.7, round(points, 1)), '; '.join(matches[:5]) or 'Preliminary role-title match', 'Full JD not independently accessible; verify requirements and seniority.'

def discover_api(db: Session, *, notify: bool = True) -> dict:
    cfg = settings()['search']
    if not cfg['enabled']:
        raise RuntimeError('Discovery is disabled in settings.yaml')
    if not env('SERPER_API_KEY'):
        raise RuntimeError('Add SERPER_API_KEY before live discovery (never invent job listings).')
    errors, seen, created = [], set(), []
    for q in SEARCH_QUERIES[:cfg['max_queries']]:
        try:
            hits = search_public_index(q)
        except Exception as exc:
            errors.append(f'{q}: {type(exc).__name__}')
            continue
        for hit in hits:
            url = hit.get('link', '')
            if not url or not url.startswith('https://'):
                continue
            normalized = canonical(url)
            if normalized in seen or db.execute(select(Job).where(Job.post_url == normalized)).scalar_one_or_none():
                continue
            seen.add(normalized)
            parsed = llm_extract(hit)
            if not parsed:
                continue
            title = parsed.get('title', '').strip()
            company = parsed.get('company', '').strip()
            if not title or not company or not any(v in title.lower() for v in ['manager', 'lead', 'director', 'head']):
                continue
            if not any(v in title.lower() for v in ['analytic', 'intelligence', 'science', 'insights', 'data strategy']):
                continue
            score, why, gaps = ranking(company, title, parsed.get('location', ''), hit.get('snippet', ''), normalized)
            job = Job(company=company, title=title, location=parsed.get('location') or 'Not stated',
                arrangement=parsed.get('arrangement') or 'Not stated', experience=parsed.get('experience') or 'Not stated',
                salary=parsed.get('salary') or 'Not stated', source=source_type(normalized), post_url=normalized,
                post_age=(parsed.get('displayed_post_age') or hit.get('date') or 'Not stated'),
                posting_date_kind='index_relative' if hit.get('date') else 'unknown',
                author_name=parsed.get('poster_name') or '', jd=hit.get('snippet') or '',
                fit_score=score, fit_why=why, gaps=gaps, verification='indexed_excerpt_not_full_post',
                leadership='Manager title; duties unverified', status='discovered')
            db.add(job)
            db.flush()
            created.append(job)
            log(db, 'job_discovered', job.id, source_url=normalized, evidence='public search index')
    created.sort(key=lambda j: j.fit_score, reverse=True)
    # Contact research is deliberately conservative: a search result is a potential contact, not proof of ownership.
    for job in created[:cfg['shortlist_size']]:
        try:
            research_contacts(db, job, limit=cfg['max_contacts_per_job'])
        except Exception as e:
            errors.append(f'Contact research for {job.company}: {type(e).__name__}')
    if notify and created and settings()['notifications']['telegram_enabled']:
        for job in created[:cfg['shortlist_size']]:
            contacts = db.execute(select(Contact).where(Contact.job_id == job.id)).scalars().all()
            url = job.post_url
            text = (f"JOB ALERT: {job.company} - {job.title}\n{job.location} | Match estimate {job.fit_score}/10\n"
                    f"Posting age: {job.post_age} (index data; original unverified)\nSource: {url}\n"
                    f"Contacts researched: {len(contacts)}/2; emails: {sum(bool(c.email) for c in contacts)}\n"
                    f"Review details in the dashboard before approving.")
            try:
                send_telegram(text, [{'text': 'Approve this job', 'callback_data': f'approve:{job.id}'},
                                     {'text': 'Reject', 'callback_data': f'reject:{job.id}'}])
            except Exception as exc:
                errors.append(f'Telegram delivery: {type(exc).__name__}')
    return {'created': len(created), 'top_job_ids': [j.id for j in created[:cfg['shortlist_size']]], 'errors': errors,
            'note': 'Indexed search snippets are not proof a vacancy remains open. Verify before sending.'}

def discover_chatgpt(db: Session, *, prompt: str | None = None, notify: bool = True) -> dict:
    """Second discovery engine: independent model-driven, source-cited web search."""
    from .agentic_search import search_with_chatgpt
    cfg = settings()['search']
    if not cfg['enabled']:
        raise RuntimeError('Discovery is disabled in settings.yaml')
    candidates = search_with_chatgpt(prompt)
    created, errors = [], []
    for hit in candidates:
        url = canonical(hit['post_url'])
        if db.execute(select(Job).where(Job.post_url == url)).scalar_one_or_none():
            continue
        # Cross-platform dedupe: same company, title and location is probably the same vacancy.
        duplicate = db.execute(select(Job).where(
            Job.company.ilike(hit['company']),
            Job.title.ilike(hit['title']),
            Job.location.ilike(hit['location'])
        )).scalar_one_or_none()
        if duplicate:
            continue
        score, why, gaps = ranking(hit['company'], hit['title'], hit['location'],
                                   hit['description'], url)
        if hit.get('fit_notes'):
            why = (why + '; ' + hit['fit_notes'])[:1200]
        if hit.get('gaps'):
            gaps = ('Model-identified gaps (not independently verified): ' + hit['gaps'])[:900]
        job = Job(company=hit['company'], title=hit['title'], location=hit['location'],
                  arrangement=hit['arrangement'], experience=hit['experience'],
                  salary=hit['salary'], source='ChatGPT web search / ' + source_type(url),
                  post_url=url, apply_url=hit.get('apply_url', ''),
                  post_age=hit['post_age'], posting_date_kind='ai_reported_unverified',
                  author_name=hit['author_name'], author_url=hit['author_url'],
                  jd=hit['description'], fit_score=score, fit_why=why,
                  gaps=gaps, leadership=hit['leadership'],
                  verification='ai_cited_source_not_full_verification',
                  status='discovered')
        db.add(job)
        db.flush()
        created.append(job)
        log(db, 'job_discovered', job.id, engine='chatgpt_web_search',
            source_url=url, evidence='OpenAI web_search cited URL; vacancy details unverified')
        for c in hit['contacts'][:cfg['max_contacts_per_job']]:
            db.add(Contact(job_id=job.id, **c))
    created.sort(key=lambda j: j.fit_score, reverse=True)
    for job in created[:cfg['shortlist_size']]:
        try:
            research_contacts(db, job, limit=cfg['max_contacts_per_job'])
        except Exception as exc:
            errors.append(f'Contact lookup for {job.company}: {type(exc).__name__}')
    if notify and created and settings()['notifications']['telegram_enabled']:
        for job in created[:cfg['shortlist_size']]:
            try:
                send_telegram(
                    f"AI research lead (unverified): {job.company} - {job.title}\n"
                    f"{job.location} | {job.post_age}\nSource: {job.post_url}\n"
                    f"Review the original post and contacts in Atlas before approving."
                )
            except Exception as exc:
                errors.append(f'Telegram delivery: {type(exc).__name__}')
    return {'engine': 'chatgpt', 'created': len(created),
            'top_job_ids': [j.id for j in created[:cfg['shortlist_size']]],
            'errors': errors,
            'note': 'Source URL is cited by web search; job age, author, and current vacancy status still require verification.'}


def discover(db: Session, *, notify: bool = True, engine: str = 'api',
             prompt: str | None = None) -> dict:
    """Choose an independent engine; UI runs each as a separate request to avoid timeouts."""
    if engine == 'chatgpt':
        return discover_chatgpt(db, prompt=prompt, notify=notify)
    if engine == 'api':
        result = discover_api(db, notify=notify)
        result['engine'] = 'api'
        return result
    raise ValueError('Unknown search engine; use api or chatgpt')


def research_contacts(db: Session, job: Job, limit: int = 2):
    if not env('SERPER_API_KEY'):
        return []
    existing = db.execute(select(Contact).where(Contact.job_id == job.id)).scalars().all()
    if len(existing) >= limit:
        return existing
    candidates: list[dict] = []
    priorities = [
        (f'site:linkedin.com/in/ "{job.company}" "Talent Acquisition" analytics', 'Potential recruiter / TA'),
        (f'site:linkedin.com/in/ "{job.company}" "Director" "Analytics"', 'Potential analytics leader'),
    ]
    for query, category in priorities:
        for hit in search_public_index(query, count=5):
            url = canonical(hit.get('link', ''))
            if 'linkedin.com/in/' not in url or not url.startswith('https://'):
                continue
            raw = hit.get('title', '').split('|')[0].split(' - ')[0].strip()
            if len(raw.split()) not in (2, 3, 4) or any(x in raw.lower() for x in ['linkedin', 'hiring', 'manager']):
                continue
            if url in [c.linkedin_url for c in existing] + [c['linkedin_url'] for c in candidates]:
                continue
            candidates.append(dict(name=raw, linkedin_url=url, title=hit.get('snippet','')[:180], category=category,
                evidence_url=url, reason='Company and function appear in an indexed LinkedIn profile; exact job relationship not confirmed.',
                relationship='potential'))
            if len(candidates) + len(existing) >= limit:
                break
        if len(candidates) + len(existing) >= limit:
            break
    for c in candidates:
        record = Contact(job_id=job.id, **c)
        if env('ROCKETREACH_API_KEY'):
            result = rocketreach_lookup(record.linkedin_url)
            if result.get('email'):
                record.email = result['email']
                record.email_status = result.get('email_status', 'provider_returned_unverified')
        db.add(record)
        existing.append(record)
    return existing
