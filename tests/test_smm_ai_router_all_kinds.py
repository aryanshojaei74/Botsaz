"""دکمه‌هایِ «راه‌اندازی و تنظیمات هوش مصنوعی» در تنظیماتِ گروه باید در هر نوعِ ربات هندلر داشته باشند
(باگِ گزارش‌شده: در سرویسِ vpn هیچ واکنشی نبود چون smm_ai_router فقط برایِ virtual_service ثبت می‌شد)."""
import os
import subprocess
import sys

import pytest

_CODE = r"""
import os, sys
sys.path.insert(0, os.getcwd())
import tenant
names = set()
def walk(r):
    for h in r.callback_query.handlers:
        names.add(getattr(h.callback, "__name__", ""))
    for sub in r.sub_routers:
        walk(sub)
walk(tenant.tenant_router)
need = {"smmai_menu", "smmai_setprovider_menu"}
print("OK" if need <= names else "MISSING:" + ",".join(sorted(need - names)))
"""


@pytest.mark.parametrize("kind", ["vpn", "digital_shop", "community_mgmt", "booking", "virtual_service", ""])
def test_ai_menu_handler_registered_for_every_kind(kind):
    env = dict(os.environ, TENANT_BOT_KIND=kind)
    out = subprocess.run([sys.executable, "-I", "-c", _CODE], env=env, capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), timeout=240)
    assert "OK" in out.stdout, (kind, out.stdout[-300:], out.stderr[-500:])
