"""Whale Support: کپچا برایِ زیرمجموعه‌گیری (قابلِ روشن/خاموش؛ پیش‌فرض خاموش)."""
import random
import types
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import extras
from tenant.shared import referral_captcha as RC

pytestmark = pytest.mark.asyncio


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import sys
    orig = config.Session
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "Session", None) is orig:
                monkeypatch.setattr(mod, "Session", mk)
        except Exception:
            pass
    _clear_setting_cache()
    async with mk() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="b", field="virtual_service", plan="pro", status="active"))
        inv = models.BotUser(bot_id=3, telegram_id=100, balance=Decimal(0))
        s.add(inv)
        await s.flush()
        s.add(models.ReferralLink(bot_id=3, user_id=inv.id, code="abc", reward_per_join=0))
        s.add(models.BotUser(bot_id=3, telegram_id=200, balance=Decimal(0)))
        s.add(models.BotUser(bot_id=3, telegram_id=201, balance=Decimal(0)))
        await s.commit()
    yield mk
    await eng.dispose()
    _clear_setting_cache()        # کشِ تنظیماتِ tenant بینِ تست‌ها نشت نکند (bot_id=3 در تست‌هایِ دیگرِ رفرال هم هست)


def _clear_setting_cache():
    for name in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        c = getattr(tenant, name, None)
        if hasattr(c, "clear"):
            c.clear()


async def _on(mk, v="1"):
    async with mk() as s:
        s.add(models.Setting(bot_id=3, key="referral_captcha", value=v))
        await s.commit()
    _clear_setting_cache()


async def _joins(mk):
    async with mk() as s:
        return (await s.execute(select(models.ReferralJoin.referred_telegram_id))).scalars().all()


async def _user(mk, tg):
    async with mk() as s:
        return await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == tg))


def test_question_has_four_unique_options_containing_the_answer():
    for seed in range(50):
        q, ans, opts = RC.make_question(random.Random(seed))
        assert len(set(opts)) == 4 and ans in opts and "+" in q


async def test_off_by_default_registers_the_join_immediately(env):
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)
    assert await _joins(env) == [200] and not any("تأییدِ دعوت" in t for _c, t, _k in bot.sent)


async def test_on_sends_challenge_and_does_not_register_until_answered_correctly(env):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)
    assert await _joins(env) == []                                        # هنوز ثبت نشده
    assert len(bot.sent) == 1 and "تأییدِ دعوت" in bot.sent[0][1]
    # دوباره با همان لینک: سؤالِ دوم نمی‌فرستد (چالشِ فعال دارد)
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)
    assert len(bot.sent) == 1 and await _joins(env) == []


class _CB:
    def __init__(self, uid, data):
        self.from_user = types.SimpleNamespace(id=uid)
        self.data = data
        self.alerts, self.edits = [], []
        outer = self

        class _M:
            async def edit_text(self, text, **k): outer.edits.append(text)
        self.message = _M()

    async def answer(self, text=None, show_alert=False, **k): self.alerts.append(text)


async def _answer(env, monkeypatch, uid, bot, value):
    st = await RC._get(3, uid)
    row = types.SimpleNamespace(id=3, plan="pro", owner_id=1)

    async def gbr(b): return row
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    cb = _CB(uid, f"rfc:{st['t']}:{value}")
    await RC.rfc_answer(cb, bot)
    return cb, st


async def test_correct_answer_registers_the_referral_once(env, monkeypatch):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)
    st = await RC._get(3, 200)
    cb, _ = await _answer(env, monkeypatch, 200, bot, st["ans"])
    assert await _joins(env) == [200] and any("تأیید شد" in e for e in cb.edits)
    assert await RC._get(3, 200) is None                                  # چالش پاک شد
    # پاسخِ دوباره/کهنه: چیزی دوباره ثبت نمی‌شود
    cb2 = _CB(200, f"rfc:{st['t']}:{st['ans']}")
    row = types.SimpleNamespace(id=3, plan="pro", owner_id=1)

    async def gbr(b): return row
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    await RC.rfc_answer(cb2, bot)
    assert await _joins(env) == [200] and any("معتبر نیست" in (a or "") for a in cb2.alerts)


async def test_three_wrong_answers_cancel_the_referral_but_user_stays(env, monkeypatch):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 201), "abc", bot)
    for _ in range(3):
        st = await RC._get(3, 201)
        if st is None:
            break
        cb, _ = await _answer(env, monkeypatch, 201, bot, st["ans"] + 100)     # قطعاً غلط
    assert await RC._get(3, 201) is None and await _joins(env) == []
    assert any("ثبتِ دعوت انجام نشد" in e for e in cb.edits)
    assert await _user(env, 201) is not None                                   # کاربر سرِ جایش است


async def test_expired_challenge_is_dropped(env, monkeypatch):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)
    st = await RC._get(3, 200)
    st["exp"] = 1
    await RC._put(3, 200, st)
    cb, _ = await _answer(env, monkeypatch, 200, bot, st["ans"])
    assert await _joins(env) == [] and any("مهلت" in e for e in cb.edits)


async def test_invalid_link_or_self_referral_bypasses_the_challenge(env):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "zzz", bot)          # لینکِ ناموجود
    inviter = await _user(env, 100)
    await extras.handle_referral_join(3, inviter, "abc", bot)                         # خودِ دعوت‌کننده
    assert bot.sent == []                                                             # کپچایی فرستاده نشد
    assert await _joins(env) == []


def test_owner_toggle_is_in_the_referral_menu_and_wired():
    from pathlib import Path
    src = Path("tenant/__init__.py").read_text(encoding="utf-8")
    kb = tenant._ref_reward_kb(True, "join", "pro", True)
    assert "refrw:captcha" in [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "include_router(referral_captcha_router)" in src
    assert src.count("_ref_reward_kb(_en, _md, row.plan, _cap)") >= 3          # همهٔ مسیرهایِ بازسازیِ منو وضعیتِ کپچا را نشان می‌دهند


# ───────────── Whale Support (۱۹ نوامبر): «اول کپچا رو میاره بعد منو اصلی؛ باید اول کپچا حل بشه بعد منو بیاد» ─────────────
async def test_join_returns_true_only_when_a_challenge_was_sent(env):
    bot = _Bot()
    assert not await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)      # کپچا خاموش ⇒ منو همان لحظه
    async with env() as s:
        for j in (await s.execute(select(models.ReferralJoin))).scalars().all():
            await s.delete(j)
        await s.commit()
    await _on(env)
    assert await extras.handle_referral_join(3, await _user(env, 201), "abc", bot) is True  # چالش فرستاده شد ⇒ منو نباید بیاید
    assert await extras.handle_referral_join(3, await _user(env, 201), "abc", bot) is True  # چالشِ فعال هم همین


def test_captcha_keyboard_never_gets_the_main_menu_button():
    """دکمهٔ سراسریِ «🏠 منوی اصلی» به صفحه‌هایِ شیشه‌ای اضافه می‌شود؛ برایِ کپچا نباید (وگرنه با یک دکمه کپچا دور زده می‌شد)."""
    kb = RC._kb("abc123", [9, 10, 14, 11])
    out = tenant._nav_inject(kb, True)
    assert out is kb and "mbtn:home" not in [b.callback_data for r in out.inline_keyboard for b in r]
    other = tenant._nav_inject(tenant.InlineKeyboardMarkup(inline_keyboard=[[tenant.InlineKeyboardButton(text="x", callback_data="foo:1")]]), True)
    assert "mbtn:home" in [b.callback_data for r in other.inline_keyboard for b in r]        # صفحه‌هایِ دیگر مثلِ قبل


async def _capture_menu(monkeypatch):
    sent = []

    async def fake(bot, row, cb): sent.append(cb.from_user.id)
    monkeypatch.setattr(tenant, "send_welcome_menu_after_captcha", fake)
    return sent


async def test_menu_is_sent_after_a_correct_answer(env, monkeypatch):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)
    sent = await _capture_menu(monkeypatch)
    st = await RC._get(3, 200)
    await _answer(env, monkeypatch, 200, bot, st["ans"])
    assert sent == [200]


async def test_menu_is_sent_after_three_wrong_answers_but_not_after_a_single_wrong_one(env, monkeypatch):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 201), "abc", bot)
    sent = await _capture_menu(monkeypatch)
    st = await RC._get(3, 201)
    await _answer(env, monkeypatch, 201, bot, st["ans"] + 100)
    assert sent == []                                                         # غلطِ اول: هنوز فقط کپچا
    st = await RC._get(3, 201)
    await _answer(env, monkeypatch, 201, bot, st["ans"] + 100)
    assert sent == []
    st = await RC._get(3, 201)
    await _answer(env, monkeypatch, 201, bot, st["ans"] + 100)
    assert sent == [201]                                                      # سومی: دعوت رد شد ولی کاربر بی‌منو نمی‌ماند


async def test_menu_is_sent_when_the_challenge_expired(env, monkeypatch):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)
    st = await RC._get(3, 200)
    st["exp"] = 1
    await RC._put(3, 200, st)
    sent = await _capture_menu(monkeypatch)
    await _answer(env, monkeypatch, 200, bot, st["ans"])
    assert sent == [200]


async def test_a_menu_failure_never_breaks_the_captcha_flow(env, monkeypatch):
    await _on(env)
    bot = _Bot()
    await extras.handle_referral_join(3, await _user(env, 200), "abc", bot)

    async def boom(bot, row, cb): raise RuntimeError("telegram down")
    monkeypatch.setattr(tenant, "send_welcome_menu_after_captcha", boom)
    st = await RC._get(3, 200)
    cb, _ = await _answer(env, monkeypatch, 200, bot, st["ans"])
    assert await _joins(env) == [200] and any("تأیید شد" in e for e in cb.edits)           # دعوت ثبت شد؛ خطا فقط لاگ شد


async def test_phone_verify_resume_reports_a_pending_challenge(env):
    await _on(env)
    bot = _Bot()

    class St:
        async def get_data(self): return {"pending_ref_code": "abc"}

        async def update_data(self, **k): pass
    u = await _user(env, 200)
    row = types.SimpleNamespace(id=3)
    assert await tenant._phone_verify_resume_pending(bot, row, u, St(), 200) is True
    assert await _joins(env) == []


def test_start_flow_returns_before_the_welcome_menu_when_challenged():
    """tenant_start: بعد از handle_referral_join اگر چالش فرستاده شد، خوش‌آمد/منو/استیکر نیاید (قبل از «استیکر/خوش‌آمد»)."""
    from pathlib import Path
    src = Path("tenant/__init__.py").read_text(encoding="utf-8")
    i = src.index("_rf_challenged = bool(await handle_referral_join(row.id, u, ref_code, bot))")
    j = src.index("if _rf_challenged:\n        return", i)
    k = src.index("welcome = await get_setting(row.id, \"welcome_text\"", i)
    bonus = src.index("await grant_welcome_bonus(bot, row, u)", i)
    assert i < bonus < j < k                                                   # هدیهٔ خوش‌آمد حتماً داده می‌شود، بعد برگشت، قبل از ساختِ خوش‌آمد
