"""Local/dev scheduler: external call to the same authenticated cron endpoint.

This process is separate from uvicorn, and must stay running. Do not run it in
parallel with a cloud scheduler. For an always-on service use a hosted scheduler.
"""
import os
import time
import httpx
from dotenv import load_dotenv
from app.config import ROOT

load_dotenv(ROOT / '.env')

url = os.environ.get('APP_BASE_URL', 'http://localhost:8000').rstrip('/')
secret = os.environ.get('CRON_SECRET', '')
if len(secret) < 24:
    raise SystemExit('Set a long CRON_SECRET in .env first')
print('Scheduler active; checking /api/cron/tick every 60 seconds. Press Ctrl+C to stop.')
while True:
    try:
        r = httpx.get(url + '/api/cron/tick', headers={'Authorization': 'Bearer ' + secret}, timeout=90)
        print(r.status_code, r.text[:500], flush=True)
    except Exception as exc:
        print('Tick failure:', type(exc).__name__, str(exc)[:200], flush=True)
    time.sleep(60)
