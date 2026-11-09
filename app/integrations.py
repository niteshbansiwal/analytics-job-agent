from __future__ import annotations
import base64
import hashlib
import os
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import parseaddr
from urllib.parse import urlencode
from cryptography.fernet import Fernet
import httpx
from sqlalchemy.orm import Session
from .config import env, settings, demo_mode
from .db import Integration
from .linkedin_urls import linkedin_profile_url

GOOGLE_SCOPES = [
    'openid', 'email', 'profile',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.compose',
]

def encryption():
    secret = env('TOKEN_ENCRYPTION_KEY')
    if not secret:
        raise RuntimeError('TOKEN_ENCRYPTION_KEY must be configured for Gmail OAuth')
    return Fernet(secret.encode())

def google_oauth_url(state: str, *, gmail: bool = False):
    """Use least-privilege Google login; ask for Gmail access separately."""
    scopes = GOOGLE_SCOPES if gmail else ['openid', 'email', 'profile']
    params = dict(client_id=env('GOOGLE_CLIENT_ID'), response_type='code',
                  redirect_uri=env('APP_BASE_URL').rstrip('/') + '/api/auth/google/callback',
                  scope=' '.join(scopes), state=state,
                  access_type='offline' if gmail else 'online',
                  prompt='consent' if gmail else 'select_account',
                  include_granted_scopes='true')
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)

def google_code_exchange(code: str):
    with httpx.Client(timeout=25) as client:
        r = client.post('https://oauth2.googleapis.com/token', data={
            'code': code, 'client_id': env('GOOGLE_CLIENT_ID'),
            'client_secret': env('GOOGLE_CLIENT_SECRET'),
            'redirect_uri': env('APP_BASE_URL').rstrip('/') + '/api/auth/google/callback',
            'grant_type': 'authorization_code',
        })
        r.raise_for_status()
        return r.json()

def google_userinfo(access_token: str):
    with httpx.Client(timeout=20) as client:
        r = client.get('https://openidconnect.googleapis.com/v1/userinfo',
                       headers={'Authorization': f'Bearer {access_token}'})
        r.raise_for_status()
        return r.json()

def save_google_refresh(db: Session, owner_email: str, refresh_token: str):
    if not refresh_token:
        raise RuntimeError('Google did not provide an offline refresh token; reconnect with consent.')
    cipher = encryption().encrypt(refresh_token.encode()).decode()
    row = db.get(Integration, 'gmail')
    if not row:
        row = Integration(provider='gmail', owner_email=owner_email, refresh_ciphertext=cipher)
        db.add(row)
    else:
        row.owner_email = owner_email
        row.refresh_ciphertext = cipher

def google_access_token(db: Session):
    record = db.get(Integration, 'gmail')
    if not record:
        raise RuntimeError('Gmail not connected')
    refresh = encryption().decrypt(record.refresh_ciphertext.encode()).decode()
    with httpx.Client(timeout=25) as client:
        r = client.post('https://oauth2.googleapis.com/token', data={
            'client_id': env('GOOGLE_CLIENT_ID'), 'client_secret': env('GOOGLE_CLIENT_SECRET'),
            'refresh_token': refresh, 'grant_type': 'refresh_token'})
        r.raise_for_status()
        return r.json()['access_token'], record.owner_email

def gmail(db: Session, method: str, path: str, *, json=None, params=None):
    token, _ = google_access_token(db)
    with httpx.Client(timeout=30) as client:
        r = client.request(method, 'https://gmail.googleapis.com/gmail/v1/users/me/' + path.lstrip('/'),
                           headers={'Authorization': f'Bearer {token}'}, json=json, params=params)
    if r.status_code == 404:
        raise RuntimeError('Gmail draft/message not found; it may have been deleted manually.')
    r.raise_for_status()
    return r.json() if r.content else {}

def build_mime(to: str, sender: str, subject: str, body: str,
               pdf: bytes = b'', filename: str = '', in_reply_to: str = '') -> str:
    msg = EmailMessage()
    msg['To'] = to
    msg['From'] = sender
    msg['Subject'] = subject
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to
    msg.set_content(body)
    if pdf:
        msg.add_attachment(pdf, maintype='application', subtype='pdf',
                           filename=filename or 'Tailored_Resume.pdf')
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip('=')

def gmail_create_draft(db: Session, to: str, subject: str, body: str, pdf: bytes = b'',
                       filename: str = '', thread_id: str = '', reply_to: str = '') -> dict:
    owner = db.get(Integration, 'gmail').owner_email
    mime = build_mime(to, owner, subject, body, pdf, filename, reply_to)
    payload = {'message': {'raw': mime}}
    if thread_id:
        payload['message']['threadId'] = thread_id
    return gmail(db, 'POST', 'drafts', json=payload)

def gmail_update_draft(db: Session, draft_id: str, to: str, subject: str,
                       body: str, pdf: bytes, filename: str) -> dict:
    """Explicitly replace an existing unsent Gmail draft; never sends it."""
    owner = db.get(Integration, 'gmail').owner_email
    mime = build_mime(to, owner, subject, body, pdf, filename)
    return gmail(db, 'PUT', f'drafts/{draft_id}', json={
        'id': draft_id, 'message': {'raw': mime}})


def gmail_get_draft(db: Session, draft_id: str) -> dict:
    return gmail(db, 'GET', f'drafts/{draft_id}', params={'format': 'raw'})

def inspect_gmail_draft(draft: dict) -> dict:
    raw = draft.get('message', {}).get('raw', '')
    if not raw:
        raise RuntimeError('Draft is empty or could not be read')
    decoded = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    msg = BytesParser(policy=email_policy).parsebytes(decoded)
    recipients = [parseaddr(part)[1] for part in str(msg.get('To', '')).split(',')]
    if not recipients or not all('@' in address and '.' in address.split('@')[-1] for address in recipients):
        raise RuntimeError('Draft lacks a valid To recipient')
    body = msg.get_body(preferencelist=('plain',))
    return {
        'to': ', '.join(recipients), 'subject': str(msg.get('Subject', '')),
        'body': body.get_content() if body else '',
        'attachment_count': sum(1 for p in msg.walk() if p.get_content_disposition() == 'attachment'),
        'attachment_filenames': [p.get_filename() or '' for p in msg.walk()
                                 if p.get_content_disposition() == 'attachment'],
        'attachment_sha256': [hashlib.sha256(p.get_payload(decode=True) or b'').hexdigest()
                              for p in msg.walk() if p.get_content_disposition() == 'attachment'],
        'thread_id': draft.get('message', {}).get('threadId', ''),
    }

def gmail_send_existing_draft(db: Session, draft_id: str) -> dict:
    # No MIME override: Gmail sends the *latest manually edited* contents.
    return gmail(db, 'POST', 'drafts/send', json={'id': draft_id})

def gmail_delete_draft(db: Session, draft_id: str):
    return gmail(db, 'DELETE', f'drafts/{draft_id}')

def gmail_get_thread(db: Session, thread_id: str):
    return gmail(db, 'GET', f'threads/{thread_id}', params={'format': 'full'})

def gmail_get_message(db: Session, message_id: str):
    return gmail(db, 'GET', f'messages/{message_id}', params={'format': 'metadata'})

def message_header(message: dict, name: str) -> str:
    for h in message.get('payload', {}).get('headers', []):
        if h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''

def send_telegram(text: str, buttons: list[dict] | None = None):
    token, chat_id = env('TELEGRAM_BOT_TOKEN'), env('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return {'sent': False, 'reason': 'Telegram not connected'}
    data = {'chat_id': chat_id, 'text': text[:3900], 'disable_web_page_preview': True}
    if buttons:
        data['reply_markup'] = {'inline_keyboard': [[b] for b in buttons]}
    with httpx.Client(timeout=18) as client:
        r = client.post(f'https://api.telegram.org/bot{token}/sendMessage', json=data)
        r.raise_for_status()
        return {'sent': True, 'message': r.json()}

def answer_telegram_callback(callback_id: str, message: str):
    token = env('TELEGRAM_BOT_TOKEN')
    if not token:
        return
    with httpx.Client(timeout=18) as client:
        client.post(f'https://api.telegram.org/bot{token}/answerCallbackQuery',
                    json={'callback_query_id': callback_id, 'text': message})

def _rocketreach_email_result(payload: dict | list, requested_linkedin: str = '') -> dict:
    """Parse a classic RocketReach lookup/checkStatus response without declaring premature misses.

    A completed lookup can initially have no work email. Preserve its profile ID
    so subsequent checkStatus calls can re-read the SAME lookup.
    """
    from urllib.parse import urlsplit
    import re

    envelope = payload if isinstance(payload, dict) else {}
    if isinstance(payload, list):
        person = payload[0] if payload else {}
    elif isinstance(payload, dict):
        person = payload.get('person') or payload.get('profile')
        if not isinstance(person, dict):
            for field in ('people', 'profiles', 'results', 'persons', 'data'):
                collection = payload.get(field)
                if isinstance(collection, list) and collection:
                    person = collection[0]
                    break
                if isinstance(collection, dict):
                    person = collection.get('person') or collection.get('profile') or collection
                    break
        if not isinstance(person, dict):
            person = payload
    else:
        person = {}
    if not isinstance(person, dict):
        return {'status': 'api_error', 'detail': 'Unexpected provider response format'}

    person_id = str(person.get('id') or person.get('person_id') or envelope.get('id') or '')
    state = str(person.get('status') or envelope.get('status') or '').lower().strip()

    def slug(url):
        if not url:
            return ''
        parsed = urlsplit(url if '://' in str(url) else 'https://' + str(url))
        host = (parsed.hostname or '').lower()
        match = re.search(r'^/in/([^/?#]+)', parsed.path, re.I)
        return match.group(1).lower() if (host == 'linkedin.com' or host.endswith('.linkedin.com')) and match else ''

    supplied_slug = slug(requested_linkedin)
    returned_slug = slug(person.get('linkedin_url') or person.get('linkedin_ext_url') or '')
    if supplied_slug and returned_slug and supplied_slug != returned_slug:
        return {'status': 'identity_mismatch', 'provider_id': person_id,
                'detail': 'Provider profile does not match requested LinkedIn identity'}

    if state in ('progress', 'searching', 'queued', 'pending', 'not queued', 'not_queued'):
        return {'status': 'pending', 'provider_id': person_id,
                'detail': 'Provider is still enriching this profile'}
    if state not in ('complete', 'success'):
        return {'status': 'api_error', 'provider_id': person_id,
                'detail': 'Unexpected provider lookup state'}

    candidates = []
    for field in ('current_work_email', 'work_email', 'current_email'):
        if person.get(field):
            candidates.append((person[field], 'rocketreach_work_email', False))
    for field in ('emails', 'work_emails', 'email_addresses', 'email'):
        entries = person.get(field) or []
        if isinstance(entries, (str, dict)):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, str):
                candidates.append((item, 'rocketreach_unverified', False))
            elif isinstance(item, dict):
                kind = str(item.get('type') or item.get('category') or '').lower()
                if kind in ('personal', 'private', 'home'):
                    continue
                validity = str(item.get('smtp_valid') or item.get('validity') or item.get('status') or '').lower()
                label = 'rocketreach_smtp_valid' if validity in ('valid', 'true') else 'rocketreach_unverified'
                candidates.append((item.get('email') or item.get('address') or item.get('value') or '', label, False))

    consumer = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com',
                'live.com', 'aol.com', 'rediffmail.com', 'proton.me', 'protonmail.com'}
    for email, label, _ in candidates:
        if isinstance(email, dict):
            email = email.get('email') or email.get('address') or ''
        email = str(email).strip().lower()
        if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) and email.split('@')[-1] not in consumer:
            return {'status': 'returned', 'provider_id': person_id,
                    'email': email, 'email_status': label}

    # Do NOT call this a final "not found" yet: provider can reveal email later.
    return {'status': 'awaiting_email' if person_id else 'not_found',
            'provider_id': person_id,
            'detail': ('Lookup completed but no professional email is available yet'
                       if person_id else 'No professional email or provider ID was returned')}


def _rocketreach_request(path: str, *, params: dict) -> dict | list:
    key = env('ROCKETREACH_API_KEY')
    if not key:
        return {'status': 'not_connected'}
    with httpx.Client(timeout=25) as client:
        response = client.get('https://api.rocketreach.co/api/v2/' + path,
                              headers={'Api-Key': key, 'Accept': 'application/json'}, params=params)
    if response.status_code in (401, 403, 429):
        return {'status': 'api_error', 'detail': f'RocketReach HTTP {response.status_code}'}
    if response.status_code == 404:
        return {'status': 'not_found'}
    response.raise_for_status()
    return response.json()


def rocketreach_lookup(linkedin_url: str) -> dict:
    if not linkedin_profile_url(linkedin_url):
        return {'status': 'no_profile', 'detail': 'Provide a safe HTTPS LinkedIn URL'}
    # Send the original URL unchanged; RocketReach determines resolvability.
    response = _rocketreach_request('person/lookup',
        params={'linkedin_url': linkedin_url, 'lookup_type': 'standard',
                'return_cached_emails': 'true'})
    if isinstance(response, dict) and response.get('status') in ('not_connected', 'api_error', 'not_found'):
        return response
    return _rocketreach_email_result(response, linkedin_url)


def rocketreach_check_status(person_id: str, linkedin_url: str) -> dict:
    if not str(person_id).isdigit():
        return {'status': 'api_error', 'detail': 'Missing provider lookup ID'}
    response = _rocketreach_request('person/checkStatus', params={'ids': str(person_id)})
    if isinstance(response, dict) and response.get('status') in ('not_connected', 'api_error', 'not_found'):
        return response
    return _rocketreach_email_result(response, linkedin_url)
