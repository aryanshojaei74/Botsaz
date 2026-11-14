"""PATCH_VS_GRACE_V1 — «ارفاق خرید»: پله‌ها، اعمال، متغیرِ {{grace}}، خاموش/روشن."""
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import vs_grace as G
from tenant.shared.text_vars import render_text

BOT, TG = 9, 777


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    tenant._SETTING_CACHE.clear()
    G._PENDING.clear()
    yield mk
    await eng.dispose()


async def _cfg(on=True, tiers=None):
    import json
    await tenant.set_setting(BOT, G.KEY_ON, "1" if on else "0")
    await tenant.set_setting(BOT, G.KEY_TIERS, json.dumps(tiers if tiers is not None else
                             [{"min": 100000, "max": 1000}, {"min": 200000, "max": 2000}]))


def test_pick_tier_uses_highest_reached():
    t = [{"min": 100000, "max": 1000}, {"min": 200000, "max": 2000}]
    assert G.pick_tier(t, 50000) is None
    assert G.pick_tier(t, 100000)["max"] == 1000
    assert G.pick_tier(t, 250000)["max"] == 2000


@pytest.mark.asyncio
async def test_apply_covers_only_inside_tier_and_only_when_on(db):
    await _cfg(on=False)
    assert await G.apply(BOT, TG, 109300, 110700) == (110700, False)          # خاموش
    await _cfg(on=True)
    price, ok = await G.apply(BOT, TG, 109300, 110700)                          # کمبود ۱٬۴۰۰ > ۱٬۰۰۰ ⇒ نه
    assert (price, ok) == (110700, False) and not G.peek_block(BOT, TG)
    price, ok = await G.apply(BOT, TG, 110000, 110700)                          # کمبود ۷۰۰ ⇒ بله؛ قیمت = موجودی
    assert ok and price == 110000
    blk = G.peek_block(BOT, TG)
    assert "<blockquote expandable>" in blk and "110,700" in blk and "700" in blk
    assert await G.apply(BOT, TG, 50000, 60000) == (60000, False)               # زیرِ حداقلِ پله
    assert await G.apply(BOT, TG, 0, 100500) == (100500, False)                 # موجودیِ صفر ارفاق نمی‌گیرد
    assert await G.apply(BOT, TG, 200000, 150000) == (150000, False)            # کمبود ندارد


@pytest.mark.asyncio
async def test_grace_variable_renders_block_or_nothing(db):
    from types import SimpleNamespace
    await _cfg()
    order = SimpleNamespace(id=1, tracking_code="123", service_name="x", quantity=1, price_toman=1,
                            link="", service_id="", status="", created_at=None, user_telegram_id=TG)
    tpl = "ثبت شد ✅\n{{grace}}\nکد: {{tracking_code}}"
    out = await render_text(tpl, bot_id=BOT, order=order)
    assert "blockquote" not in out and "{{grace}}" not in out                   # اعمال نشده ⇒ انگار نه انگار
    await G.apply(BOT, TG, 110000, 110700)
    out = await render_text(tpl, bot_id=BOT, order=order)
    assert "<blockquote expandable>" in out and "کد: 123" in out
    # hook: متنِ پیش‌فرضِ ثبت ⇒ بلوک به انتها
    dflt = "✅ سفارش شما با موفقیت ثبت شد! 🎉\n\n🛍 x"
    from tenant.shared.vs_msg_override import rule_keys, _PLACED
    assert rule_keys(dflt) == _PLACED
    assert "blockquote" in G.decorate(BOT, TG, dflt, is_default_placed=True)
    assert G.decorate(BOT, TG + 1, dflt, is_default_placed=True) == dflt        # کاربرِ دیگر: بدونِ تغییر


@pytest.mark.asyncio
async def test_ai_order_lookup_by_code_only_for_own_orders(db):
    import datetime as dt
    from tenant.bots.virtual_service import smm_ai as AI
    async with db() as s:
        s.add(models.Bot(id=BOT, owner_id=1, token="t:9", field="virtual_service", plan="pro"))
        s.add(models.SmmOrder(bot_id=BOT, user_telegram_id=TG, smm_service_pk=1, service_id="1", service_name="ممبر",
                              link="x", quantity=100, price_toman=5000, status="completed",
                              created_at=dt.datetime(2026, 11, 5, 9, 0), tracking_code="1515"))
        s.add(models.SmmOrder(bot_id=BOT, user_telegram_id=TG + 1, smm_service_pk=1, service_id="1", service_name="لایک",
                              link="y", quantity=10, price_toman=900, status="pending",
                              created_at=dt.datetime(2026, 11, 5, 9, 0), tracking_code="2020"))
        await s.commit()
    mine = await AI._order_lookup_context(BOT, TG, "اکسا سفارشمو با کد پیگیری ۱۵۱۵ پیگیری کن")
    assert "ممبر" in mine and "1515" in mine and "ساعتِ ثبت" in mine
    other = await AI._order_lookup_context(BOT, TG, "کد 2020")                  # سفارشِ کاربرِ دیگر
    assert "لایک" not in other and "پیدا نشد" in other
    assert await AI._order_lookup_context(BOT, TG, "سلام") == ""


def test_custom_text_is_the_whole_quote_no_fixed_header_added():
    # SINA: متنِ تنظیم‌شده نباید داخلِ یک قالبِ ثابت بنشیند (تیتر/خط‌ها دوبار می‌آمد)
    txt = "🌟 ارفاق ویژه برای شما\n💰 مبلغ فاکتور: {invoice} تومان\n⭕️ اختلاف: {diff} تومان\n✅ ممنون از همراهی شما"
    blk = G.build_block(txt, 110700, 110000, 700)
    assert blk.startswith("<blockquote expandable>") and blk.endswith("</blockquote>")
    assert blk.count("ارفاق") == 1 and "110,700" in blk and "700 تومان" in blk and "👛" not in blk
    assert "{invoice}" not in blk and "{{" not in blk
    dflt = G.build_block("", 110700, 110000, 700)
    assert "ارفاقِ ویژه" in dflt and "110,000" in dflt


@pytest.mark.asyncio
async def test_apply_precheck_does_not_record(db):
    await _cfg()
    price, ok = await G.apply(BOT, TG, 110000, 110700, record=False)
    assert ok and price == 110000 and not G.peek_block(BOT, TG)
