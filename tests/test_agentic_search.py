"""Offline validation of ChatGPT web-search source grounding. No network calls."""
import json
import os
os.environ['DEMO_MODE'] = 'true'

import pytest
from app.agentic_search import master_prompt, validate_jobs


def payload(url, include_source=True):
    job = {
        'company': 'Example Payments', 'title': 'Analytics Manager',
        'original_post_url': url, 'location': 'Gurugram',
        'posting_age': '2 days ago', 'poster_name': '',
        'contacts': [],
    }
    source = {'type': 'web_search_call', 'action': {
        'sources': [{'url': url}] if include_source else []
    }}
    msg = {'type': 'message', 'content': [{
        'type': 'output_text', 'text': json.dumps({'jobs': [job]}), 'annotations': []
    }]}
    return {'output': [source, msg]}


def test_prompt_is_exact_full_research_brief():
    text = master_prompt()
    assert len(text) > 10000
    assert 'LINKEDIN HIRING-POST HUNT' in text
    assert '45 days' in text


def test_source_backed_vacancy_is_retained():
    url = 'https://www.linkedin.com/posts/recruiter_analytics-manager-activity-123'
    results = validate_jobs(payload(url))
    assert len(results) == 1
    assert results[0]['company'] == 'Example Payments'
    assert results[0]['post_url'].startswith('https://linkedin.com/posts/')


def test_model_invented_url_is_rejected():
    url = 'https://www.linkedin.com/posts/unverified_analytics-activity-123'
    with pytest.raises(ValueError, match='no inspectable source'):
        validate_jobs(payload(url, include_source=False))


def test_web_search_is_required():
    p = payload('https://www.linkedin.com/posts/recruiter_analytics-activity-123')
    p['output'] = [x for x in p['output'] if x['type'] != 'web_search_call']
    with pytest.raises(ValueError, match='did not perform'):
        validate_jobs(p)


def test_unsourced_contact_is_not_retained():
    url = 'https://www.linkedin.com/posts/recruiter_analytics-activity-123'
    p = payload(url)
    raw = json.loads(p['output'][1]['content'][0]['text'])
    raw['jobs'][0]['contacts'] = [{
        'name': 'Invented Person', 'linkedin_profile_url': 'https://linkedin.com/in/invented',
    }]
    p['output'][1]['content'][0]['text'] = json.dumps(raw)
    result = validate_jobs(p)
    assert result[0]['contacts'] == []
