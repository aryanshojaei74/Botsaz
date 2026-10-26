"""فدایی: (۱) دکمهٔ «درخواستِ لغو» برایِ همهٔ سفارش‌هایِ ثبت‌شده رویِ پنل (پرچمِ cancelِ ارائه‌دهنده پنهانش نکند)،
(۲) صفحهٔ محصول دکمهٔ «بازگشت به لیست» با مقصدِ دقیقِ همان لیست/صفحه دارد."""
from types import SimpleNamespace

import pytest

import models
from tenant.bots.virtual_service import smm, smm_refill
from tests.test_smm_partial import _callbacks, _make, db, notified  # noqa: F401


@pytest.mark.asyncio
async def test_cancel_button_shown_even_if_provider_flag_says_no(db, notified, monkeypatch):
    bot_id, oid = await _make(db, status="sent")
    _, kb = await smm._smm_myorder_detail_content(bot_id, 555, oid, 1)
    assert any(c.startswith("smmu:reqcancel:") for c in _callbacks(kb))
    # پرچمِ cancel=false در کشِ ارائه‌دهنده دیگر دکمه را پنهان نمی‌کند
    monkeypatch.setattr(smm_refill, "cached_cancel", lambda *a, **k: False)
    _, kb = await smm._smm_myorder_detail_content(bot_id, 555, oid, 1)
    assert any(c.startswith("smmu:reqcancel:") for c in _callbacks(kb))
    b2, o2 = await _make(db, status="awaiting_admin", qty=60)
    _, kb = await smm._smm_myorder_detail_content(b2, 555, o2, 1)
    assert any(c.startswith("smmu:reqcancel:") for c in _callbacks(kb))
    for st in ("completed", "refunded", "canceled", "failed"):
        b3, o3 = await _make(db, status=st, qty=70)
        _, kb = await smm._smm_myorder_detail_content(b3, 555, o3, 1)
        assert not any(c.startswith("smmu:reqcancel:") for c in _callbacks(kb)), st
    b4, o4 = await _make(db, status="sent", qty=80, panel_oid=None)                     # سفارشِ دستی: چیزی برایِ ارسال به API نیست
    _, kb = await smm._smm_myorder_detail_content(b4, 555, o4, 1)
    assert not any(c.startswith("smmu:reqcancel:") for c in _callbacks(kb))


@pytest.mark.asyncio
async def test_pick_service_passes_exact_list_back_and_page_has_button(db, notified, monkeypatch):
    got = {}

    async def fake_page(cb, bot, vid, state, breadcrumb=None, back_cb=None):
        got.update(vid=vid, back=back_cb)
        return True

    async def fake_row(bot):
        return SimpleNamespace(id=1)
    import tenant
    monkeypatch.setattr(smm, "send_smm_product_page", fake_page)
    monkeypatch.setattr(tenant, "get_bot_row", fake_row)

    class CB:
        def __init__(self, data):
            self.data = data

        async def answer(self, *a, **k):
            pass
    await smm.smmu_pick_service(CB("smmu:svc:42:telegram:members:3"), None, None)
    assert got == {"vid": 42, "back": "smmu:type:telegram:members:3"}
    await smm.smmu_pick_service(CB("smmu:svc:42"), None, None)                            # دکمه‌هایِ قدیمی بدونِ پسوند
    assert got == {"vid": 42, "back": None}


@pytest.mark.asyncio
async def test_product_page_back_button_row(db, notified, monkeypatch):
    import tenant
    async with db() as s:
        bot = models.Bot(owner_id=1, token="9:T", username="shop9", field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        svc = models.SmmService(bot_id=bot.id, service_id="12", name="ممبر", min_qty=10, max_qty=1000, sell_rate=1000,
                                is_active=True)
        s.add(svc)
        await s.commit()
        bid, sid = bot.id, svc.id

    async def fake_row(b):
        return SimpleNamespace(id=bid, plan="pro", field="virtual_service")
    monkeypatch.setattr(tenant, "get_bot_row", fake_row)
    shown = {}

    class Msg:
        async def edit_text(self, text, **kw):
            shown["kb"] = kw.get("reply_markup")

        async def answer(self, text, **kw):
            shown["kb"] = kw.get("reply_markup")

    class CB(SimpleNamespace):
        async def answer(self, *a, **k):
            pass
    from aiogram.types import CallbackQuery
    cb = CB(message=Msg())
    monkeypatch.setattr(smm, "CallbackQuery", CB)                                         # isinstance(msg_or_cb, CallbackQuery)

    class St:
        async def set_state(self, *a, **k): pass
        async def update_data(self, *a, **k): pass
        async def clear(self): pass
    ok = await smm.send_smm_product_page(cb, None, sid, St(), back_cb="smmu:type:telegram:members:3")
    assert ok
    rows = [[(b.text, b.callback_data) for b in r] for r in shown["kb"].inline_keyboard]
    assert rows[-1] == [("⬅️ بازگشت به لیست", "smmu:type:telegram:members:3"), ("🏠 منوی اصلی", "mbtn:home")]
    ok = await smm.send_smm_product_page(cb, None, sid, St())
    assert [(b.text, b.callback_data) for b in shown["kb"].inline_keyboard[-1]] == [("🏠 منوی اصلی", "mbtn:home")]
