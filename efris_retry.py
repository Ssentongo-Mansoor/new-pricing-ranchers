"""Cron entry point: drain the EFRIS retry queue.

cPanel cron (every 5 minutes):
    cd /home/CPUSER/ranchers_app && \
    /home/CPUSER/virtualenv/ranchers_app/3.11/bin/python efris_retry.py
"""
from app import app
from services import efris

with app.app_context():
    ok, bad = efris.process_queue()
    if ok or bad:
        print(f"EFRIS queue: {ok} fiscalized, {bad} still pending/failed")
