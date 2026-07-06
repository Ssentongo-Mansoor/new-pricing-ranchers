"""Passenger entry point for cPanel (Setup Python App).

cPanel's Passenger loads this file and looks for a WSGI callable named
``application``. Locally you keep running the app with start.command / flask;
this file is only used on the server, so it is safe to commit.

In cPanel > Setup Python App set:
  Application startup file : passenger_wsgi.py
  Application Entry point  : application
And add environment variables there (SECRET_KEY, and DATABASE_URL if you move
off SQLite). Do NOT hardcode secrets in this file.
"""
import os
import sys

# Make sure the app package is importable regardless of Passenger's cwd.
sys.path.insert(0, os.path.dirname(__file__))

from app import app as application  # noqa: E402  (module-level Flask app)
