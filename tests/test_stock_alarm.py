"""آلارمِ تمام‌شدنِ دستهٔ انبار + بررسیِ باگ + دکمهٔ آموزش در تحویلِ تست (MidnightSoul)."""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.shared.stock_alarm as SA


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, SA, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=77, token="1:T", username="x", field="vpn_sales", plan="pro", status="active")
        s.add(b); await s.flush()
        bid = b.id
        for i, (cat, st) in enumerate([("دسته ۱", "in_stock"), ("دسته ۱", "in_stock"), ("دسته ۲", "in_stock")]):
            s.add(models.Config(bot_id=bid, code=i + 1, volume_gb=10, days=30, payload=f"vless://{i}", status=st, category=cat))
        await s.commit()
    yield maker, bid
    await engine.dispose()


async def _sell(maker, bid, cat, n=99):
    from sqlalchemy import select
    async with maker() as s:
        rows = (await s.scalars(select(models.Config).where(
            models.Config.bot_id == bid, models.Config.category == cat, models.Config.status == "in_stock"))).all()
        for r in rows[:n]:
            r.status = "sold"
        await s.commit()


@pytest.mark.asyncio
async def test_first_run_is_silent_then_alert_once_then_rearm(env):
    maker, bid = env
    sent = []

    async def send(t):
        sent.append(t)
    assert await SA.check_bot(bid, send) == [] and not sent          # اولین اجرا: فقط ثبت
    await _sell(maker, bid, "دسته ۲")
    assert await SA.check_bot(bid, send) == ["دسته ۲"]
    assert len(sent) == 1 and "دسته ۲" in sent[0]
    assert await SA.check_bot(bid, send) == [] and len(sent) == 1      # تکراری نمی‌شود
    async with maker() as s:                                           # شارژ دوباره
        s.add(models.Config(bot_id=bid, code=50, volume_gb=1, days=1, payload="p", category="دسته ۲"))
        await s.commit()
    assert await SA.check_bot(bid, send) == []
    await _sell(maker, bid, "دسته ۲")
    assert await SA.check_bot(bid, send) == ["دسته ۲"] and len(sent) == 2


@pytest.mark.asyncio
async def test_partial_sale_does_not_alert_and_toggle_off_silences(env):
    maker, bid = env
    sent = []

    async def send(t):
        sent.append(t)
    await SA.check_bot(bid, send)
    await _sell(maker, bid, "دسته ۱", n=1)
    assert await SA.check_bot(bid, send) == []
    await tenant.set_setting(bid, SA.ENABLED_KEY, "0")
    await _sell(maker, bid, "دسته ۱")
    assert await SA.check_bot(bid, send) == [] and not sent


@pytest.mark.asyncio
async def test_diagnose_and_safe_fix(env):
    maker, bid = env
    async with maker() as s:
        c = await s.get(models.Config, 1)
        c.owner_user_id = None
        s.add(models.MainButton(bot_id=bid, row=0, col=0, label="تست", action="free_trial",
                                config=json.dumps({"trial_mode": "manual", "trial_claim_limit": "abc",
                                                   "trial_category": "دسته ۹"})))
        await s.commit()
    info = await SA.diagnose(bid)
    txt = " ".join(t for t, _ in info["issues"])
    assert "عددِ معتبر نیست" in txt and "دستهٔ «دسته ۹»" in txt
    done = await SA.apply_fixes(bid)
    assert any("روی ۱" in d for d in done)
    info2 = await SA.diagnose(bid)
    assert not any("عددِ معتبر نیست" in t for t, _ in info2["issues"])
    assert any("دسته ۹" in t for t, _ in info2["issues"])          # موردِ غیرِ بی‌خطر دست‌نخورده می‌ماند
    text, kb = SA._render(info2, True)
    assert "دسته ۱" in text and kb.inline_keyboard


@pytest.mark.asyncio
async def test_trial_stock_markup_has_guide_button(env, monkeypatch):
    from aiogram.types import InlineKeyboardButton
    seen = {}

    async def fake_guide(bot_id, source="manual", category=None):
        seen.update(source=source, category=category)
        return [InlineKeyboardButton(text="📖 آموزش اتصال", callback_data="cfgguide:show:manual")]
    async def only_manual(bot_id, source="manual", category=None):       # آموزشِ اختصاصیِ تست تنظیم نشده
        return None if source == "trial" else await fake_guide(bot_id, source, category)
    monkeypatch.setattr(tenant, "_cfg_guide_row", only_manual)
    kb = await tenant._trial_stock_markup(1, 10, "دسته ۱")
    flat = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "cfgguide:show:manual" in flat and seen == {"source": "manual", "category": "دسته ۱"}   # بازگشت به آموزشِ روشِ فروش
    await tenant._trial_stock_markup(1, 0, None)
    assert seen["source"] == "unlimited"


@pytest.mark.asyncio
async def test_trial_has_its_own_guide_source_with_priority_and_no_legacy_leak(env, monkeypatch):
    """MidnightSoul: برای سرویس‌هایِ تست آموزش/لینکِ جدا بشود؛ نبودش ⇒ همان آموزشِ روشِ فروش."""
    maker, bid = env
    import tenant.bots.vpn.vpn_admin as VA
    monkeypatch.setattr(VA, "Session", maker)
    assert tenant.TRIAL_GUIDE_SOURCE == "trial" and ("trial", "🎁 سرویس‌های تست رایگان") in tenant._CFG_GUIDE_SOURCES
    await tenant.set_setting(bid, "cfg_guide_enabled", "1")                 # آموزشِ عمومیِ قدیمی
    await tenant.set_setting(bid, "cfg_guide_text", "قدیمی")
    assert await tenant._cfgguide_get(bid, "trial") == (False, "")           # به تست نشت نمی‌کند
    assert await tenant._cfg_guide_row(bid, "trial") is None
    kb = await tenant._trial_stock_markup(bid, 10, None)                     # فقط آموزشِ «دستی» (قدیمی) می‌ماند
    assert kb is None or all("trial" not in (b.callback_data or "") for r in kb.inline_keyboard for b in r)
    await tenant.set_setting(bid, "cfg_guide_enabled_trial", "1")
    await tenant.set_setting(bid, "cfg_guide_text_trial", '<a href="https://t.me/x/1">آموزش اندروید</a>')
    row = await tenant._cfg_guide_row(bid, "trial")
    assert row and row[0].callback_data == "cfgguide:show:trial"
    kb = await tenant._trial_stock_markup(bid, 10, "دسته ۱")
    assert "cfgguide:show:trial" in [b.callback_data for r in kb.inline_keyboard for b in r]
    from aiogram.types import InlineKeyboardMarkup
    body, picker = await tenant._cfgguide_picker_content(bid)
    cbs = [b.callback_data for r in picker.inline_keyboard for b in r]
    assert "tadm:cfgguide:pick:trial" in cbs and any("✅" in b.text and "تست" in b.text for r in picker.inline_keyboard for b in r)
    text, kb2 = await tenant._cfgguide_panel(bid, "trial")
    assert "سرویس‌های تست" in text and "آموزش اندروید" in text
    await tenant.set_setting(bid, "cfg_guide_enabled_trial", "0")            # غیرفعال ⇒ دوباره آموزشِ روشِ فروش
    assert await tenant._cfg_guide_row(bid, "trial") is None
