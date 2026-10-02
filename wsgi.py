"""
Production entry point.

    gunicorn wsgi:app

Flask's built-in server is single-threaded and explicitly not for production;
gunicorn is the WSGI server that actually runs the app. This module exists so
the start command is one short string rather than a module path with a colon in
it, and so database setup happens exactly once before the first request.

One worker, deliberately. The free tier has little memory, SQLite does not enjoy
concurrent writers, and the rate limiter in app/server.py counts requests
per-process, so a second worker would silently double the effective limit. All
three point the same way until this moves to Postgres.
"""

import os

from app.server import app, ensure_database, ensure_schema

# Runs at import, before gunicorn accepts a request. On a fresh container the
# database file does not exist yet; on a warm one this is a no-op.
if os.environ.get("RUNG_SKIP_DB_INIT") != "1":
    ensure_database()
else:
    # Seeding is skipped (Vercel: tools.init_db did it once), but new columns
    # still have to exist before the first query selects them, or a deploy
    # that adds one breaks every page until someone reruns the seeder.
    ensure_schema()

__all__ = ["app"]
