"""Template lint — every POST form must carry a CSRF token.

QA audit 5 Jul 2026 H1: eight production forms shipped without csrf_token and
every submission returned 400 with CSRF enforced globally. This test fails the
build when any template POST form lacks either a csrf_token hidden input or a
csrf_token() call inside the form body.

Run (no database needed):

    python3 tests/test_templates_csrf.py
"""
import os
import re
import sys

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "templates")

# A <form ...> opening tag whose method is post, then everything up to the
# matching </form>. DOTALL because forms span lines. Non-greedy body so nested
# text stops at the first close tag — forms are not nested in HTML.
FORM_RE = re.compile(r"<form\b[^>]*\bmethod\s*=\s*['\"]post['\"][^>]*>(.*?)</form>",
                     re.IGNORECASE | re.DOTALL)
TOKEN_RE = re.compile(r"csrf_token", re.IGNORECASE)

PASS = FAIL = 0
failures = []

for root, _dirs, files in os.walk(BASE):
    for fn in files:
        if not fn.endswith(".html"):
            continue
        path = os.path.join(root, fn)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for m in FORM_RE.finditer(src):
            rel = os.path.relpath(path, BASE)
            line = src.count("\n", 0, m.start()) + 1
            if TOKEN_RE.search(m.group(1)):
                PASS += 1
            else:
                FAIL += 1
                failures.append(f"{rel}:{line}")

print(f"POST forms with a CSRF token: {PASS}")
if failures:
    print(f"POST forms WITHOUT a CSRF token: {FAIL}")
    for f in failures:
        print(f"  MISSING: {f}")
    sys.exit(1)
print("All POST forms carry a CSRF token.")
