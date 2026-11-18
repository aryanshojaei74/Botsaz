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
