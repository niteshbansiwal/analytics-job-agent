"""Vercel FastAPI framework entrypoint.

Keep the ASGI app at a Vercel auto-detected root path. The actual
application and routes remain in app/main.py.
"""
from app.main import app
