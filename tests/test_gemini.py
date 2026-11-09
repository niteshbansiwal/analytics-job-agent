"""Gemini tests are offline: never use a real key, quota or Gmail."""
import json
import pytest
from app import gemini

JD = ('Own SQL-driven analytics, reporting and stakeholder decision support '
      'for payment clients, including dashboard strategy, commercial insights, '
      'automation, and collaboration with product teams.')

def drafted(*, subject='Analytics Manager at Visa | Relevant analytics impact',
            body=None, ids=None):
    if body is None:
        body = ('Hi [FIRST_NAME],\n\n'
                'Your Visa analytics manager role emphasizes SQL-based decision '
                'support and payments reporting. At American Express, I own '
                'analytics projects end to end, from scoping to field rollout, '
                'while leading a team of two analysts. At Plivo, I built '
                'dashboards with QuickSight, Power BI and Tableau. '
                'I would value a short conversation about the decisions your '
                'team is trying to improve.\n\nRegards,\nNitesh Bansiwal')
    return json.dumps({'subject': subject, 'body': body,
                       'fact_ids': ids or ['dashboards', 'stakeholders']})


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


def test_model_authors_complete_email_from_saved_prompt_and_jd(monkeypatch):
    prompts = []
    def fake(prompt, **kwargs):
        prompts.append(prompt)
        assert kwargs['json_output'] is True
        return drafted()
    monkeypatch.setattr(gemini, 'gemini_text', fake)
    instruction = 'Use punchy bullet points and show how I create business value.'
    result = gemini.build_email_preview('Visa', 'Analytics Manager', JD,
                                        'Jane Recruiter', instruction)
    assert result['mode'] == 'ai_email'
    assert result['prompt_source'] == 'custom'
    assert result['prompt_characters'] == len(instruction)
    assert len(result['prompt_fingerprint']) == 12
    assert instruction in prompts[0]
    assert JD in prompts[0]
    assert 'Your Visa analytics manager role' in result['body']
    assert 'Hi Jane,' in result['body']
    assert '[FIRST_NAME]' not in result['body']
    assert 'Jane Recruiter' not in prompts[0]  # contact name stays private
    assert result['selected_fact_ids'] == ['dashboards', 'stakeholders']
    assert result['gmail_draft_created'] is False
    assert result['email_sent'] is False
    assert result['resume_generated'] is False


def test_model_email_not_overwritten_by_fixed_fact_bullets(monkeypatch):
    body = ('Hi [FIRST_NAME],\n\nThe Visa Analytics Manager position '
            'requires decisions built on clear commercial analytics. '
            'I have owned field-facing analytics delivery at American Express '
            'and developed reporting at Plivo. I would like to compare '
            'approaches to payments decision support with your team.\n\n'
            'Thanks,\nNitesh Bansiwal')
    monkeypatch.setattr(gemini, 'gemini_text',
                        lambda *args, **kwargs: drafted(body=body))
    result = gemini.build_email_preview('Visa', 'Analytics Manager', JD, 'Yashaswini')
    assert 'Hi Yashaswini,' in result['body']
    assert 'Thanks,\nNitesh Bansiwal' in result['body']
    assert result['body'].count('At Plivo, I built dashboards') == 0


def test_invalid_response_fails_closed_not_generic_fallback(monkeypatch):
    monkeypatch.setattr(gemini, 'gemini_text',
                        lambda *args, **kwargs: '{"fact_ids":["made_up_experience"]}')
    with pytest.raises(gemini.GeminiError):
        gemini.build_email_preview('Visa', 'Analytics Manager', JD)


def test_model_unsupported_numeric_claim_is_blocked(monkeypatch):
    body = ('Hi [FIRST_NAME],\n\nThe Visa Analytics Manager role '
            'needs SQL reporting and business decision support. '
            'I managed 300 analysts for 15 years and generated '
            'enormous revenue for the business. I would welcome '
            'a discussion about the analytics challenges you face. '
            'Thank you for your consideration.\n\nRegards,\nNitesh Bansiwal')
    monkeypatch.setattr(gemini, 'gemini_text',
                        lambda *args, **kwargs: drafted(body=body))
    with pytest.raises(gemini.GeminiError, match='unsupported numerical'):
        gemini.build_email_preview('Visa', 'Analytics Manager', JD)


def test_timeout_and_quota_errors_do_not_send_or_fallback(monkeypatch):
    calls = []
    def fake(prompt, **kwargs):
        calls.append(1)
        raise gemini.GeminiError('Gemini quota or rate limit exceeded (HTTP 429)')
    monkeypatch.setattr(gemini, 'gemini_text', fake)
    with pytest.raises(gemini.GeminiError, match='429'):
        gemini.build_email_preview('Visa', 'Analytics Manager', JD)
    assert len(calls) == 1


def test_missing_detailed_jd_blocks_generic_email(monkeypatch):
    monkeypatch.setattr(gemini, 'gemini_text',
                        lambda *args, **kwargs: drafted())
    with pytest.raises(gemini.GeminiError, match='Detailed job description'):
        gemini.build_email_preview('Visa', 'Analytics Manager', 'SQL')


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
            assert 'gemini-3.5-flash-lite:generateContent' in url
            assert headers['x-goog-api-key'] == 'fake-key'
            assert 'fake-key' not in str(json)
            assert json['generationConfig']['thinkingConfig']['thinkingLevel'] == 'MINIMAL'
            return Response()
    monkeypatch.setattr(gemini, 'env',
        lambda name, default='': {'GEMINI_API_KEY':'fake-key',
                                 'GEMINI_MODEL':'gemini-3.5-flash-lite'}.get(name,default))
    monkeypatch.setattr(gemini.httpx, 'Client', Client)
    assert gemini.test_gemini_connection()['connected']


def test_email_model_uses_minimal_thinking_and_legacy_uses_low(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {'candidates': [{'content': {'parts': [
                {'text': '{"fact_ids":["dashboards","amex_sme"]}'}
            ]}}]}
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, *, headers, json):
            calls.append((url, json))
            return Response()
    monkeypatch.setattr(gemini, 'env',
        lambda name, default='': {'GEMINI_API_KEY': 'fake-key',
                                 'GEMINI_MODEL': 'gemini-3.5-flash-lite'}.get(name, default))
    monkeypatch.setattr(gemini.httpx, 'Client', Client)
    for model in ('gemini-3.5-flash-lite', 'gemini-3.8-flash'):
        result = gemini.gemini_text('Select facts', json_output=True, model=model)
        assert gemini._validated_ids(result) == ['dashboards', 'amex_sme']
    assert len(calls) == 2
    for (url, payload), model in zip(calls, ('gemini-3.5-flash-lite', 'gemini-3.8-flash')):
        assert model + ':generateContent' in url
        assert payload['generationConfig']['thinkingConfig'] == {
            'thinkingLevel': 'MINIMAL' if model == gemini.EMAIL_MODEL else 'LOW'}
        assert payload['generationConfig']['responseMimeType'] == 'application/json'
