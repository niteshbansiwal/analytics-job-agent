"""Offline model transport and controlled email contracts."""
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

def test_resume_numbers_and_url_digits():
    assert {'900', '98', '450000', '10000'} <= gemini._approved_numbers()
    assert gemini._draft_numbers('Saved $450K across 10,000+ queries') <= gemini._approved_numbers()
    assert not gemini._draft_numbers('https://linkedin.com/jobs/view/987654321/ user123@example.com')
    assert '123456789' in gemini._draft_numbers('Saved 123456789 dollars')




@pytest.mark.parametrize('code', [429, 500, 502, 503, 504])
def test_gemini_transient_http_errors_are_retryable(monkeypatch, code):
    class Response:
        status_code = code
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['timeout'].read == 45.0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(gemini, 'env', lambda name, default='': 'fake-key'
                        if name == 'GEMINI_API_KEY' else 'gemini-3.5-flash-lite')
    monkeypatch.setattr(gemini.httpx, 'Client', Client)
    with pytest.raises(gemini.GeminiError) as exc:
        gemini.gemini_text('Test', json_output=True)
    assert exc.value.retryable is True
    assert str(code) in str(exc.value)


def test_gemini_invalid_configuration_is_not_retryable(monkeypatch):
    class Response:
        status_code = 400
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(gemini, 'env', lambda name, default='': 'fake-key'
                        if name == 'GEMINI_API_KEY' else 'gemini-3.5-flash-lite')
    monkeypatch.setattr(gemini.httpx, 'Client', Client)
    with pytest.raises(gemini.GeminiError) as exc:
        gemini.gemini_text('Test', json_output=True)
    assert exc.value.retryable is False
