"""دسترسیِ پولیِ API: انقضا، رایگان/پولی بودن، و «باز برایِ همه»."""
import asyncio
import datetime as dt
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenant.shared import public_api as pa  # noqa: E402


def _u(access=False, exp=None, bot_id=1):
    return types.SimpleNamespace(api_access=access, api_expires_at=exp, bot_id=bot_id)


def _patch(monkeypatch, price=0, open_all=False):
    async def _price(_b): return price
    async def _open(_b): return open_all
    monkeypatch.setattr(pa, "api_price", _price)
    monkeypatch.setattr(pa, "api_open_to_all", _open)


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_manual_grant_is_permanent(monkeypatch):
    _patch(monkeypatch)
    assert run(pa.user_api_allowed(_u(access=True)))


def test_no_access_by_default(monkeypatch):
    _patch(monkeypatch)
    assert not run(pa.user_api_allowed(_u()))
    assert not run(pa.user_api_allowed(None))


def test_monthly_access_expires(monkeypatch):
    _patch(monkeypatch, price=50000)
    now = dt.datetime.utcnow()
    assert run(pa.user_api_allowed(_u(True, now + dt.timedelta(days=3))))
    assert not run(pa.user_api_allowed(_u(True, now - dt.timedelta(seconds=1))))


def test_open_to_all_only_when_free(monkeypatch):
    _patch(monkeypatch, price=0, open_all=True)
    assert run(pa.user_api_allowed(_u()))
    _patch(monkeypatch, price=50000, open_all=True)   # پولی شد ⇒ «باز برایِ همه» کارساز نیست
    assert not run(pa.user_api_allowed(_u()))
    # ولی خریدارِ معتبر همچنان دسترسی دارد
    assert run(pa.user_api_allowed(_u(True, None)))
