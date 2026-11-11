"""Controlled email composition and fail-closed checks at provider boundaries."""
import hashlib
import html
import json
import re
from urllib.parse import urlsplit


class EmailValidationError(ValueError):
    pass


DEFAULTS = {
    'subjects': [
        "{{first_name}}, interested in {{company}}'s {{job_title}} role",
        "{{first_name}}, reaching out about {{company}}'s {{job_title}} opening",
        "{{first_name}}, a quick note about {{company}}'s {{job_title}} role",
        "{{first_name}}, about the {{job_title}} opportunity at {{company}}",
    ],
    'subject_mode': 'rotate', 'subject_index': 0,
    'body_template': ('Hi {{first_name}},\n\n'
        "I'm writing about the {{job_title}} opening at {{company}} ({{job_link}}). I'd love to be considered.\n\n"
        'A bit about me:\n{{achievements}}\n\n'
        '{{value_proposition}}\n\n'
        "If you're still hiring, would you be open to a quick conversation?\n\n"
        'Nitesh Bansiwal\nResume attached'),
    'followup_template': ('Hi {{first_name}},\n\n'
        "I'm following up on my note about the {{job_title}} opportunity at {{company}}. "
        "I believe my background in hands-on analytics and stakeholder delivery could be valuable to your team.\n\n"
        "If you're still hiring, would you be open to a quick conversation?\n\n"
        'Nitesh Bansiwal'),
    'resume_id': 'analytics',
    'max_repairs': 2,
    'allowed_fact_ids': ['amex_sme','amex_genai','amex_call_compliance','amex_nl_sql','plivo_margin','plivo_fraud','dashboards','stakeholders','zs_forecasting','experience'],
}
TOKENS = {'first_name', 'company', 'job_title', 'job_link', 'value_proposition', 'achievements'}
SUBJECT_TOKENS = {'first_name', 'company', 'job_title'}
FOLLOWUP_TOKENS = {'first_name', 'company', 'job_title'}
# Match common template syntaxes and bare machine-style variable identifiers.
VARIABLES = re.compile(
    r'\{\{[^{}\n]{1,160}\}\}|\$\{[^{}\n]{1,160}\}|<%[\s\S]{1,160}?%>'
    r'|\[(?:[A-Za-z][A-Za-z_ .-]{0,100})\]|\{[A-Za-z][A-Za-z_ .-]{0,100}\}'
    r'|<(?:first[_ -]?name|company|job[_ -]?(?:title|url|link)|recipient[^>]*)>'
    r'|\b(?:FIRST_?NAME|CONTACT_?(?:FIRST_?)?NAME|RECIPIENT_?(?:FIRST_?)?NAME|COMPANY_?NAME|JOB_?TITLE|JOB_?URL|JOB_?LINK'
    r'|first_name|contact_name|recipient_name|company_name|job_title|job_url|job_link)\b'
    r'|\b(?:INSERT|REPLACE|YOUR)_(?:NAME|COMPANY|LINK|URL|ROLE)\b', re.I)


def assert_no_variables(subject, body):
    if not isinstance(subject, str) or not isinstance(body, str):
        raise EmailValidationError('Subject and body must be text')
    match = VARIABLES.search(html.unescape(subject + '\n' + body))
    if match:
        raise EmailValidationError('Unresolved email variable: ' + match.group()[:80])
    if '\n' in subject or '\r' in subject or any(ord(c) < 32 for c in subject):
        raise EmailValidationError('Subject must be one line')
    if '\\n' in subject + body or '\\r' in subject + body:
        raise EmailValidationError('Email contains literal escaped newlines')
    # The HTML renderer legitimately uses "link here" as an HTTPS anchor label.
    # Treat only unlinked occurrences as placeholders, not a real clickable link.
    body_without_verified_link_labels = re.sub(
        r'<a\s+href="https://[^"]+"[^>]*>link here</a>', '', body, flags=re.I)
    if re.search(r'(?i)\b(?:link here|insert link|job link here)\b', body_without_verified_link_labels):
        raise EmailValidationError('Email contains an unfilled link label')


def first_name(name):
    name = (name or '').strip()
    name = re.sub(r'(?i)^(?:mr|mrs|ms|dr|prof)\.?\s+', '', name)
    first = name.split()[0] if name else ''
    if name.casefold() in ('hiring team', 'hiring manager', 'unknown', 'recruiter') or not re.fullmatch(r"[^\W\d_][\w.'’\-]{1,39}", first):
        raise EmailValidationError('A real hiring contact first name is required')
    assert_no_variables(first, '')
    return first


def job_url(value):
    value = (value or '').strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or re.search(r'[\s<>"\x00-\x1f]', value):
            raise ValueError()
    except ValueError:
        raise EmailValidationError('A valid original HTTPS job posting URL is required')
    return value


def validate_settings(config):
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise EmailValidationError('Unknown email template setting')
    value = {**DEFAULTS, **config}
    subjects = value['subjects']
    if not isinstance(subjects, list) or not 1 <= len(subjects) <= 4 or any(not isinstance(s,str) or not 8 <= len(s.strip()) <= 180 for s in subjects):
        raise EmailValidationError('Save between one and four subject templates, 8–180 characters each')
    body = value['body_template']
    if not isinstance(body,str) or not 100 <= len(body) <= 4000:
        raise EmailValidationError('Body template must contain 100–4000 characters')
    followup = value['followup_template']
    if not isinstance(followup, str) or not 80 <= len(followup) <= 2500:
        raise EmailValidationError('Follow-up template must contain 80–2500 characters')
    for text, allowed in [(s, SUBJECT_TOKENS) for s in subjects] + [(body,TOKENS), (followup,FOLLOWUP_TOKENS)]:
        found = re.findall(r'\{\{([a-z_]+)\}\}',text)
        if set(found) - allowed:
            raise EmailValidationError('Unknown template variable')
        assert_no_variables('', re.sub(r'\{\{([a-z_]+)\}\}', '',text))
        if text in subjects and ('\n' in text or '\r' in text):
            raise EmailValidationError('Subject templates must be one line')
    if not body.startswith('Hi {{first_name}},'):
        raise EmailValidationError('Body must start with Hi {{first_name}},')
    for token in ('first_name','job_link','value_proposition','achievements'):
        if body.count('{{'+token+'}}') != 1:
            raise EmailValidationError('Body requires exactly one {{'+token+'}}')
    if 'Nitesh Bansiwal' not in body:
        raise EmailValidationError('Keep your name in the email signature')
    if not followup.startswith('Hi {{first_name}},') or followup.count('{{first_name}}') != 1 or 'Nitesh Bansiwal' not in followup:
        raise EmailValidationError('Follow-up must greet the first name and retain your signature')
    if value['subject_mode'] not in ('rotate','fixed') or type(value['subject_index']) is not int or not 0 <= value['subject_index'] < len(subjects):
        raise EmailValidationError('Choose a valid subject line and rotation mode')
    if value['resume_id'] not in ('analytics','data_science'):
        raise EmailValidationError('Select one of the two uploaded resume versions')
    if type(value['max_repairs']) is not int or not 0 <= value['max_repairs'] <= 2:
        raise EmailValidationError('Repair attempts must be 0, 1 or 2')
    allowed = value['allowed_fact_ids']
    if not isinstance(allowed,list) or len(allowed)<2 or any(k not in DEFAULTS['allowed_fact_ids'] for k in allowed) or len(set(allowed))!=len(allowed):
        raise EmailValidationError('Enable at least two distinct verified achievements')
    value['subjects'] = [s.strip() for s in subjects]
    return value


def load_settings(db):
    from .db import get_kv
    raw = get_kv(db, 'email_template_controls', '')
    return validate_settings(json.loads(raw) if raw else {})


def compose(config, company, title, contact_name, source_url, value, fact_ids, facts):
    config = validate_settings(config)
    name, url = first_name(contact_name), job_url(source_url)
    values = {'first_name':name,'company':company,'job_title':title,'job_link':url,
              'value_proposition':value,'achievements':'\n'.join(f'{i}. {facts[k]}' for i,k in enumerate(fact_ids, 1))}
    index = (int(hashlib.sha256((url+'|'+contact_name).encode()).hexdigest()[:8],16) % len(config['subjects'])
             if config['subject_mode']=='rotate' else config['subject_index'])
    def render(text):
        return re.sub(r'\{\{([a-z_]+)\}\}',lambda m:values[m[1]],text).strip()
    subject, body = render(config['subjects'][index]),render(config['body_template'])
    if len(subject)>198: subject = subject[:197].rstrip()+'…'
    validate_final(subject,body,contact_name,url)
    return subject,body,index


def compose_followup(config, company, title, contact_name):
    config = validate_settings(config)
    values = {'first_name': first_name(contact_name), 'company': company, 'job_title': title}
    body = re.sub(r'\{\{([a-z_]+)\}\}', lambda m: values[m[1]], config['followup_template']).strip()
    assert_no_variables('Re: previous application', body)
    return body


def require_v2_format(body):
    """Hold scheduled drafts that do not match the owner's saved V2 structure."""
    heading = body.find('A bit about me:\n')
    first = re.search(r'(?m)^1\. \S', body)
    second = re.search(r'(?m)^2\. \S', body)
    interest = re.search(r"(?m)^I'm interested because\b", body)
    if (heading < 0 or not first or not second or not interest
            or not heading < first.start() < second.start() < interest.start()
            or len(re.findall(r'(?m)^\d+\. ', body)) != 2):
        raise EmailValidationError('Email does not follow the saved V2 template: two numbered bullets must precede the interest paragraph')
    if "If you're still hiring, would you be open to a quick conversation?" not in body:
        raise EmailValidationError('Email closing does not match the saved V2 template')


def validate_final(subject, body, contact_name, source_url, *, followup=False):
    assert_no_variables(subject,body)
    if not 3 <= len(subject) <= 200 or not 30 <= len(body) <= 8000:
        raise EmailValidationError('Email subject or body length is invalid')
    name = first_name(contact_name)
    if not followup and not re.match(r'^Hi\s+'+re.escape(name)+r',\s*\n', body):
        raise EmailValidationError('Greeting does not match the selected hiring contact')
    greeting = re.match(r'(?i)^(?:hi|hello|dear)\s+([\w.’\'-]+)\b',subject) or re.match(r'^([\w.’\'-]+),',subject)
    if greeting and greeting[1].casefold() != name.casefold():
        raise EmailValidationError('Subject addresses a different contact')
    if not followup and job_url(source_url) not in body:
        raise EmailValidationError('Original job posting URL is missing from the body')


def html_body(body):
    """Only code creates anchors; model/user HTML is escaped, never executed."""
    parts, end = [], 0
    for match in re.finditer(r'https?://[^\s<>]+',body):
        url = match.group().rstrip(').,;')
        parts.append(html.escape(body[end:match.start()]))
        parts.append('<a href="'+html.escape(url,quote=True)+'">'+html.escape('link here' if body[max(0,match.start()-1):match.start()] == '(' else ('View job posting' if not end else url))+'</a>')
        end = match.start()+len(url)
    parts.append(html.escape(body[end:]))
    return '<html><body><div style="white-space:pre-wrap;font-family:Arial,sans-serif">'+''.join(parts)+'</div></body></html>'
