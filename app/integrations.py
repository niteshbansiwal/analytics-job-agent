from __future__ import annotations
import base64
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

def rocketreach_lookup(linkedin_url: str) -> dict:
    key = env('ROCKETREACH_API_KEY')
    if not key:
        return {'status': 'not_connected'}
    if 'linkedin.com/in/' not in linkedin_url:
        return {'status': 'no_profile'}
    with httpx.Client(timeout=35) as client:
        r = client.get('https://api.rocketreach.co/api/v2/person/lookup',
                       headers={'Api-Key': key, 'Accept': 'application/json'},
                       params={'linkedin_url': linkedin_url, 'lookup_type': 'standard', 'return_cached_emails': 'true'})
        if r.status_code in (401, 403, 429):
            return {'status': 'api_error', 'detail': f'RocketReach returned {r.status_code}'}
        r.raise_for_status()
        response = r.json()
    status = str(response.get('status') or '').lower()
    if status not in ('complete', 'success'):
        return {'status': status or 'pending', 'detail': 'Lookup not complete; no email claimed'}
    profile = response.get('person') or response.get('profile') or response
    collected = profile.get('emails') or profile.get('email') or []
    if isinstance(collected, (str, dict)):
        collected = [collected]
    for item in collected:
        if isinstance(item, str):
            return {'status': 'returned', 'email': item, 'email_status': 'provider_returned_unverified'}
        if isinstance(item, dict):
            mail = item.get('email') or item.get('address') or ''
            if mail:
                return {'status': 'returned', 'email': mail,
                        'email_status': str(item.get('status') or 'provider_returned_unverified')}
    return {'status': 'not_found'}
