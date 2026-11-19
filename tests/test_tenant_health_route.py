"""PATCH_TENANT_HEALTH_ROUTE_V1: main_tenant باید GET /health را واقعاً ثبت کند (دکوراتور افتاده بود ⇒ ۴۰۴)؛ apply.sh هم نباید به ۲۰۰ وابسته باشد."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_tenant_health_is_registered():
    src = open(os.path.join(ROOT, "main_tenant.py"), encoding="utf-8").read()
    assert re.search(r'@app\.get\("/health"\)[^\n]*\nasync def health\(\)', src)


def test_apply_script_health_check_accepts_any_http_answer():
    src = open(os.path.join(ROOT, "deploy", "apply.sh"), encoding="utf-8").read()
    line = next(l for l in src.splitlines() if "curl" in l and "127.0.0.1:$1/health" in l)
    assert " -f" not in line and "-fs" not in line
