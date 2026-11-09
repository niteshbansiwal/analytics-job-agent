"""Safe LinkedIn URL acceptance without rewriting user-provided profile URLs.

RocketReach decides whether a LinkedIn URL can be resolved. Atlas only checks
the HTTPS scheme and genuine LinkedIn host before passing the original string.
"""
from urllib.parse import urlsplit


def linkedin_profile_url(value: str) -> str:
    if not isinstance(value, str):
        return ''
    original = value.strip()
    if not original or len(original) > 1500 or any(ch.isspace() or ord(ch) < 32 for ch in original):
        return ''
    try:
        parsed = urlsplit(original)
        host = (parsed.hostname or '').lower().rstrip('.')
        if (parsed.scheme.lower() != 'https'
                or not (host == 'linkedin.com' or host.endswith('.linkedin.com'))
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)
                or not parsed.path or parsed.path == '/'):
            return ''
    except ValueError:
        return ''
    return original
