"""Gemini tests are offline: never use a real key, quota or Gmail."""
import json
import pytest
from app import gemini


def test_gemini_connection_smoke_test_is_minimal(monkeypatch):
    prompts = []
    def fake(prompt, **kwargs):
        prompts.append(prompt)
        return 'ATLAS_OK'
    monkeypatch.setattr(gemini, 'gemini_text', fake)
    result = gemini.test_gemini_connection()
    assert result['connected'] is True
    assert len(prompts) == 1
    assert 'ATLAS_OK' in prompts[0]


def test_gemini_preview_selects_only_approved_career_fact_ids(monkeypatch):
    prompts = []
    def fake(prompt, **kwargs):
        prompts.append(prompt)
        assert kwargs['json_output'] is True
        return json.dumps({'fact_ids': ['dashboards', 'amex_sme', 'stakeholders']})
    monkeypatch.setattr(gemini, 'gemini_text', fake)
    result = gemini.build_email_preview('Visa', 'Analytics Manager',
        'SQL Tableau dashboards for payment clients', 'Jane Recruiter')
    assert result['mode'] == 'gemini'
    assert result['selected_fact_ids'] == ['dashboards', 'amex_sme', 'stakeholders']
    assert 'At American Express' in result['body']
    assert 'Hi Jane,' in result['body']
    assert '8+ years' not in result['body']
    assert result['gmail_draft_created'] is False
    assert result['email_sent'] is False
    assert result['resume_generated'] is False
    assert 'Jane Recruiter' not in prompts[0]  # no contact PII to model
    assert 'nitesh.bansiwal23@gmail.com' not in prompts[0]


def test_gemini_invalid_response_falls_back_without_inventing_facts(monkeypatch):
    monkeypatch.setattr(gemini, 'gemini_text',
        lambda prompt, **kwargs: '{"fact_ids":["made_up_experience"]}')
    result = gemini.build_email_preview('Example', 'Revenue Analytics Manager',
        'Pricing and sales reporting')
    assert result['mode'] == 'facts_only_fallback'
    assert result['selected_fact_ids'] == ['amex_sme', 'plivo_margin', 'dashboards']
    assert 'made_up_experience' not in result['body']
    assert '15% improvement' in result['body']
    assert result['warning']


def test_gemini_api_quota_error_is_visible_in_fallback(monkeypatch):
    def fail(prompt, **kwargs):
        raise gemini.GeminiError('Gemini quota or rate limit exceeded (HTTP 429)')
    monkeypatch.setattr(gemini, 'gemini_text', fail)
    result = gemini.build_email_preview('Example', 'Credit Risk Manager', 'Fraud risk')
    assert result['mode'] == 'facts_only_fallback'
    assert '429' in result['warning']
    assert result['email_sent'] is False


def test_gemini_validates_ids_before_use():
    for raw in ('{"fact_ids":["unknown","dashboards"]}',
                '{"fact_ids":["dashboards","dashboards"]}',
                '{"fact_ids":[]}', 'not json', '[]'):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            gemini._validated_ids(raw)


def test_gemini_api_response_and_no_key_leak(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {'candidates':[{'content':{'parts':[{'text':'ATLAS_OK'}]}}]}
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, *, headers, json):
            assert 'gemini-3.8-flash:generateContent' in url
            assert headers['x-goog-api-key'] == 'fake-key'
            assert 'fake-key' not in str(json)
            return Response()
    monkeypatch.setattr(gemini, 'env',
        lambda name, default='': {'GEMINI_API_KEY':'fake-key',
                                 'GEMINI_MODEL':'gemini-3.8-flash'}.get(name,default))
    monkeypatch.setattr(gemini.httpx, 'Client', Client)
    assert gemini.test_gemini_connection()['connected']
