#!/bin/bash
# Ranchers Finest Pricing — LOCAL test launcher (hardened build, July 2026).
# Double-click in Finder, or run:  bash start_local.command
# This is for local testing on your Mac only. Do NOT use these settings in production.
cd "$(dirname "$0")" || exit 1

echo "Preparing the Ranchers Finest pricing app (local test)..."
python3 -m pip install -q -r requirements.txt 2>/dev/null

# Local-only settings:
#  SECRET_KEY   - the app now refuses to start without one. This is a dev-only value.
#  COOKIE_INSECURE=1 - lets login work over plain http://localhost (production uses HTTPS).
export SECRET_KEY="local-dev-only-key-do-not-use-in-production-4471903857"
export COOKIE_INSECURE=1

echo ""
echo "============================================================"
echo "  Ranchers Finest Pricing (LOCAL TEST) is starting."
echo "  Open this in your browser:   http://127.0.0.1:8000"
echo "  Login: use your existing admin username and password."
echo "  To stop the app: come back here and press  Control + C"
echo "============================================================"
echo ""
python3 -m flask --app app run --port 8000
