from __future__ import annotations
import os
from pathlib import Path
from functools import lru_cache
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

@lru_cache(maxsize=1)
def settings() -> dict:
    with (ROOT / 'config' / 'settings.yaml').open(encoding='utf-8') as f:
        return yaml.safe_load(f)

def env(name: str, default: str = '') -> str:
    value = os.getenv(name, default)
    if value.lower().startswith('replace_with'):
        return ''
    return value

def demo_mode() -> bool:
    # Never implicitly enable demo on a hosted deployment.
    return env('DEMO_MODE', 'false' if os.getenv('VERCEL') else 'true').lower() == 'true'

def configured(name: str) -> bool:
    return bool(env(name))
