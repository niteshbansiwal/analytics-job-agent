"""Independent OpenAI Responses API web-research engine.

This does not reuse a ChatGPT conversation or its Plus subscription. It invokes
a model with the hosted web_search tool, then keeps only source-backed URLs.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit
import httpx

from .config import ROOT, env


def master_prompt() -> str:
    return (ROOT / 'config' / 'job_search_prompt.md').read_text(encoding='utf-8')


def canonical_url(value: str) -> str:
    if not isinstance(value, str):
        return ''
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() != 'https' or not parts.hostname:
            return ''
        host = parts.hostname.lower().removeprefix('www.')
        if host in {'localhost', 'example.com', 'example.org'}:
            return ''
        return urlunsplit(('https', host, parts.path.rstrip('/'), '', ''))
    except (ValueError, AttributeError):
        return ''


def cited_urls(payload: dict) -> set[str]:
    """Extract actual tool-source and citation URLs, not model-written links."""
    urls: set[str] = set()
    for item in payload.get('output', []):
        if item.get('type') == 'web_search_call':
            for source in (item.get('action') or {}).get('sources') or []:
                if isinstance(source, dict):
                    url = canonical_url(source.get('url', ''))
                    if url:
                        urls.add(url)
        if item.get('type') == 'message':
            for content in item.get('content', []):
                for annotation in content.get('annotations') or []:
                    if annotation.get('type') == 'url_citation':
                        url = canonical_url(annotation.get('url', ''))
                        if url:
                            urls.add(url)
    return urls


def _response_text(payload: dict) -> str:
    chunks = []
    for item in payload.get('output', []):
        if item.get('type') == 'message':
            chunks.extend(str(c.get('text') or '') for c in item.get('content', []) if c.get('type') == 'output_text')
    return '\n'.join(chunks).strip()


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r'^\x60{3}(?:json)?\s*|\s*\x60{3}$', '', text.strip(), flags=re.I)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        begin, end = cleaned.find('{'), cleaned.rfind('}')
        if begin < 0 or end <= begin:
            raise ValueError('AI research did not return structured job evidence')
        data = json.loads(cleaned[begin:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
        raise ValueError('AI research returned no structured jobs list')
    return data


def validate_jobs(payload: dict) -> list[dict]:
    """Never store a vacancy or a contact based solely on model-generated URLs."""
    if not any(x.get('type') == 'web_search_call' for x in payload.get('output', [])):
        raise ValueError('AI model did not perform a web search')
    sources = cited_urls(payload)
    if not sources:
        raise ValueError('Web research returned no inspectable source URLs')
    parsed = _parse_json(_response_text(payload))
    results = []
    seen = set()
    for raw in parsed['jobs'][:15]:
        if not isinstance(raw, dict):
            continue
        url = canonical_url(raw.get('original_post_url', ''))
        if not url or url not in sources or url in seen:
            continue
        title = str(raw.get('title') or '').strip()[:250]
        company = str(raw.get('company') or '').strip()[:200]
        if not title or not company:
            continue
        lower = title.lower()
        if not any(t in lower for t in ('manager', 'head of', 'analytics lead', 'insights lead')):
            continue
        if not any(t in lower for t in ('analytic', 'insight', 'intelligence', 'decision science', 'data strategy', 'risk', 'fraud')):
            continue
        contacts = []
        for c in (raw.get('contacts') or [])[:2]:
            if not isinstance(c, dict):
                continue
            profile = canonical_url(c.get('linkedin_profile_url', ''))
            if not profile or profile not in sources or 'linkedin.com/in/' not in profile:
                continue
            name = str(c.get('name') or '').strip()[:200]
            if not name:
                continue
            contacts.append({
                'name': name, 'title': str(c.get('title') or '')[:250],
                'category': str(c.get('category') or 'Potential hiring-side contact')[:80],
                'linkedin_url': profile,
                'evidence_url': profile,
                'reason': str(c.get('why_relevant') or 'Profile is cited; exact vacancy relationship not independently confirmed.')[:500],
                'relationship': 'potential',
            })
        seen.add(url)
        results.append({
            'company': company, 'title': title,
            'post_url': url,
            'apply_url': canonical_url(raw.get('apply_url', '')) if canonical_url(raw.get('apply_url', '')) in sources else '',
            'location': str(raw.get('location') or 'Not stated')[:250],
            'arrangement': str(raw.get('arrangement') or 'Not stated')[:80],
            'experience': str(raw.get('experience') or 'Not stated')[:80],
            'salary': str(raw.get('salary') or 'Not stated')[:80],
            'post_age': str(raw.get('posting_age') or 'Not stated')[:100],
            'author_name': str(raw.get('poster_name') or '')[:200],
            'author_url': canonical_url(raw.get('poster_profile_url', '')) if canonical_url(raw.get('poster_profile_url', '')) in sources else '',
            'description': str(raw.get('evidence_summary') or '')[:2500],
            'fit_notes': str(raw.get('fit_notes') or '')[:600],
            'gaps': str(raw.get('gaps') or '')[:600],
            'leadership': str(raw.get('managerial_vs_ic') or 'Unverified')[:100],
            'contacts': contacts,
        })
    return results


def search_with_chatgpt(prompt: str | None = None) -> list[dict]:
    """Run a fresh independent agentic web search using the saved user's prompt."""
    key = env('OPENAI_API_KEY')
    if not key:
        raise RuntimeError('OPENAI_API_KEY is required for ChatGPT-powered web research')
    instructions = (prompt or master_prompt()).strip()
    if not 100 <= len(instructions) <= 24000:
        raise ValueError('Search prompt must be between 100 and 24,000 characters')
    directive = """Perform actual WEB SEARCHES now, independently of any other job-search API.
Use the user's instructions below as the research brief. Prefer LinkedIn authored
hiring posts, especially from the last 48 hours, and allow jobs up to 45 days old.
Do not invent or infer posting dates, authors, LinkedIn URLs or hiring relationships.
If you cannot access a post, do not claim to have verified it. Search again for
a source you can cite. You may return fewer than five jobs.

Output one JSON object ONLY, of the form:
{"jobs":[{"company":"","title":"","location":"","arrangement":"",
"experience":"","salary":"","original_post_url":"","apply_url":"",
"posting_age":"","poster_name":"","poster_profile_url":"",
"evidence_summary":"","fit_notes":"","gaps":"","managerial_vs_ic":"",
"contacts":[{"name":"","title":"","category":"","linkedin_profile_url":"",
"why_relevant":""}]}]}
Return up to eight genuine job leads. Each original_post_url MUST be an exact
URL of a page you accessed or cited during this web search; cite that source
in your response annotations. Each contact LinkedIn URL must also be a cited
source. If no source-backed jobs can be found, return {"jobs":[]}.
Only a source-backed URL can be accepted into the dashboard.

USER'S EXACT RESEARCH BRIEF:
"""
    with httpx.Client(timeout=55) as client:
        response = client.post(
            'https://api.openai.com/v1/responses',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={
                'model': env('OPENAI_SEARCH_MODEL', 'gpt-6.1-sol'),
                'reasoning': {'effort': 'low'},
                'tools': [{'type': 'web_search'}],
                'tool_choice': 'required',
                'include': ['web_search_call.action.sources'],
                'input': directive + instructions,
                'max_output_tokens': 6500,
                'store': False,
            },
        )
        response.raise_for_status()
        return validate_jobs(response.json())
