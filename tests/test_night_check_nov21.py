"""بررسی لاگ شب ۳۰ آبان: KeyError psel_panel، تایم‌اوت‌های پرسروصدا، نرخ هم‌زمان در قیمت‌یاب."""
import pytest


@pytest.mark.asyncio
async def test_psel_price_step_with_lost_data_answers_instead_of_crash(monkeypatch):
    from tenant.bots.panel_seller import admin as A

    class _Row:
        id = 1

    async def guard(msg, bot):
        return _Row()
    monkeypatch.setattr(A, "_guard", guard)

    class _St:
        cleared = False

        async def get_data(self):
            return {"psel_title": "x"}                       # بقیهٔ کلیدها گم شده

        async def clear(self):
            self.cleared = True

    class _M:
        text = "50000"
        out = []

        async def answer(self, text, **kw):
            self.out.append(text)
    m, st = _M(), _St()
    await A.pk_price(m, None, st)
    assert st.cleared and "از اول" in m.out[-1]


def test_timeouts_are_quiet():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent           # خواندن فایل؛ وارد کردن main_platform روترها را دوباره وصل می‌کند
    assert "except (asyncio.TimeoutError, TelegramNetworkError)" in (root / "tenant/bots/giveaway/loop.py").read_text()
    assert "ری‌اکشن /start: تایم‌اوتِ تلگرام" in (root / "tenant/__init__.py").read_text()
    assert "حذفِ وب‌هوک لازم نیست" in (root / "main_platform.py").read_text()


@pytest.mark.asyncio
async def test_rate_fetched_in_parallel_with_coin(monkeypatch):
    import asyncio
    from tenant.shared import coin_data as cd
    from tenant.shared import price_lookup as pl
    from tenant.shared.coin_data import Coin
    order = []

    async def rate():
        order.append("rate-start")
        await asyncio.sleep(0.05)
        order.append("rate-end")
        return 270_000.0, "x"

    async def lookup(*a, **k):
        order.append("coin-start")
        await asyncio.sleep(0.05)
        order.append("coin-end")
        return Coin(name="Gram", symbol="TON", price=1.0)
    monkeypatch.setattr(pl, "live_rate", rate)
    monkeypatch.setattr(cd, "lookup", lookup)
    text, img = await pl.answer_for(1, ("asset", 1.0, "ton"))
    assert "270,000 تومان" in text and img is None
    assert order.index("coin-start") < order.index("rate-end")             # هم‌زمان، نه پشت سر هم
