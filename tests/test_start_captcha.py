"""PATCH_START_CAPTCHA_V1 (Royal): «کاربر چه زیرمجموعه باشه چه نباشه، ربات را استارت کرد کپچا بیاد»."""
import time
import types

import pytest
import pytest_asyncio
from aiogram.types import Chat, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import start_captcha as SC

pytestmark = pytest.mark.asyncio
BOT, OWNER, U1, U2 = 3, 1, 200, 201


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
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="3:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.commit()

    async def gbr(b):
        async with mk() as s:
            return await s.get(models.Bot, BOT)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    yield types.SimpleNamespace(mk=mk)
    await eng.dispose()
    tenant._SETTING_CACHE.clear()


class Msg:
    def __init__(self, uid=U1, chat_type="private", is_bot=False):
        self.out = []
        self.from_user = types.SimpleNamespace(id=uid, is_bot=is_bot, full_name="u", username="u")
        self.chat = types.SimpleNamespace(id=uid, type=chat_type)

    async def answer(self, text, **kw): self.out.append((text, kw))


async def row(env):
    async with env.mk() as s:
        return await s.get(models.Bot, BOT)


async def setmode(m):
    await tenant.set_setting(BOT, SC.SETTING_KEY, m)


async def start(env, uid=U1, created=True, args=None, **kw):
    m = Msg(uid, **kw)
    blocked = await SC.gate(m, types.SimpleNamespace(), await row(env), created, args)
    return blocked, m


async def test_off_by_default(env):
    blocked, m = await start(env)
    assert blocked is False and m.out == []


async def test_new_mode_challenges_brand_new_users_with_or_without_a_referral(env):
    await setmode("new")
    for uid, args in ((U1, None), (U2, "ref_abc")):                              # «چه زیرمجموعه باشه چه نباشه»
        blocked, m = await start(env, uid, True, args)
        assert blocked is True and "تأییدِ هویت" in m.out[0][0]
        cbs = [b.callback_data for r in m.out[0][1]["reply_markup"].inline_keyboard for b in r]
        assert len(cbs) == 4 and all(c.startswith("scp:") for c in cbs)
    assert (await SC._get(BOT, U2))["args"] == "ref_abc"


async def test_new_mode_does_not_touch_existing_customers_but_all_mode_does_once(env):
    await setmode("new")
    blocked, m = await start(env, created=False)
    assert blocked is False and m.out == []
    await setmode("all")
    blocked, m = await start(env, created=False)
    assert blocked is True
    await SC._mark_verified(BOT, U1)
    blocked, m = await start(env, created=False)
    assert blocked is False                                                       # یک‌بار؛ بعد از تأیید دیگر نمی‌پرسد


async def test_owner_admins_groups_and_bots_are_exempt(env):
    await setmode("all")
    assert (await start(env, OWNER, True))[0] is False
    async with env.mk() as s:
        s.add(models.AdminSupport(bot_id=BOT, telegram_id=500, full_name="a"))
        await s.commit()
    assert (await start(env, 500, True))[0] is False
    assert (await start(env, U1, True, chat_type="group"))[0] is False
    assert (await start(env, U1, True, is_bot=True))[0] is False


async def test_second_start_does_not_bypass_the_captcha_and_keeps_the_invite_and_tries(env):
    await setmode("new")
    await start(env, U1, True, "ref_abc")
    rec = await SC._get(BOT, U1)
    rec["tries"] = 2
    await SC._put(BOT, U1, rec)
    blocked, m = await start(env, U1, False, None)                                # دوباره /start، این‌بار «تازه» نیست و پارامتر ندارد
    rec2 = await SC._get(BOT, U1)
    assert blocked is True and rec2["args"] == "ref_abc" and rec2["tries"] == 2 and rec2["new"] is True
    assert "فرصتِ باقی‌مانده: 1" in m.out[0][0]                                    # تلاش‌ها با /start ریست نمی‌شود (جلوگیریِ حدسِ پشتِ‌سرهم)


class Cb:
    def __init__(self, data, uid=U1):
        self.data = data
        self.from_user = User(id=uid, is_bot=False, first_name="u")
        self.edits, self.answers = [], []
        outer = self

        class M:
            chat = Chat(id=uid, type="private")

            async def edit_text(self, text, **k): outer.edits.append(text)
        self.message = M()

    async def answer(self, text=None, show_alert=False, **k): self.answers.append((text, show_alert))


async def answer(env, uid, value):
    rec = await SC._get(BOT, uid)
    cb = Cb(f"scp:{rec['t']}:{value}", uid)
    await SC.scp_answer(cb, types.SimpleNamespace(), types.SimpleNamespace())
    return cb, rec


async def test_correct_answer_verifies_and_replays_the_original_start(env, monkeypatch):
    await setmode("new")
    await start(env, U1, True, "ref_abc")
    seen = []

    async def fake_start(msg, command, bot, state):
        seen.append((msg.text, command.args, SC.SCAP_REPLAY.get(), msg.from_user.id))
    monkeypatch.setattr(tenant, "tenant_start", fake_start)
    rec = await SC._get(BOT, U1)
    cb, _ = await answer(env, U1, rec["ans"])
    assert any("تأیید شد" in e for e in cb.edits)
    assert await SC.is_verified(BOT, U1) and await SC._get(BOT, U1) is None
    assert seen == [("/start ref_abc", "ref_abc", "new", U1)]                     # همان دیپ‌لینک، با پرچمِ «کاربرِ تازه»
    assert SC.SCAP_REPLAY.get() is None                                            # پرچم بعدِ بازپخش آزاد شد
    assert (await start(env, U1, False))[0] is False                               # حالا /start عادی است


async def test_replay_skips_the_gate_so_the_user_is_not_asked_twice(env):
    await setmode("all")
    tok = SC.SCAP_REPLAY.set("old")
    try:
        assert (await start(env, U1, False))[0] is False
    finally:
        SC.SCAP_REPLAY.reset(tok)


async def test_wrong_answers_get_new_questions_then_a_ten_minute_block(env, monkeypatch):
    await setmode("new")
    await start(env, U1, True)
    replayed = []

    async def fake_start(*a, **k): replayed.append(1)
    monkeypatch.setattr(tenant, "tenant_start", fake_start)
    tokens = set()
    for i in range(2):
        rec = await SC._get(BOT, U1)
        tokens.add(rec["t"])
        cb, _ = await answer(env, U1, rec["ans"] + 100)
        assert "فرصتِ باقی‌مانده" in cb.edits[-1] or i == 2
    assert len(tokens) == 2                                                        # بعد از هر غلط سؤالِ تازه
    rec = await SC._get(BOT, U1)
    cb, _ = await answer(env, U1, rec["ans"] + 100)
    assert any("۱۰ دقیقه" in e for e in cb.edits) and replayed == [] and not await SC.is_verified(BOT, U1)
    blocked, m = await start(env, U1, False)
    assert blocked is True and "دقیقهٔ دیگر" in m.out[0][0]                         # محدودیتِ /start
    rec = await SC._get(BOT, U1)
    rec["until"] = time.time() - 1                                                  # محدودیت تمام شد ⇒ سؤالِ تازه
    await SC._put(BOT, U1, rec)
    blocked, m = await start(env, U1, False)
    assert blocked is True and "تأییدِ هویت" in m.out[0][0] and "فرصتِ باقی‌مانده: 3" in m.out[0][0]


async def test_expired_or_stale_questions_do_not_verify(env):
    await setmode("new")
    await start(env, U1, True, "ref_abc")
    rec = await SC._get(BOT, U1)
    stale = Cb(f"scp:zzzzzz:{rec['ans']}")
    await SC.scp_answer(stale, types.SimpleNamespace(), types.SimpleNamespace())
    assert stale.answers[0][1] and not await SC.is_verified(BOT, U1)                # توکنِ کهنه
    rec["exp"] = 1
    await SC._put(BOT, U1, rec)
    cb = Cb(f"scp:{rec['t']}:{rec['ans']}")
    await SC.scp_answer(cb, types.SimpleNamespace(), types.SimpleNamespace())
    assert any("مهلت" in e for e in cb.edits) and not await SC.is_verified(BOT, U1)
    blocked, m = await start(env, U1, False)                                         # /startِ بعدی: سؤالِ تازه، با لینکِ اولیه
    assert blocked is True and (await SC._get(BOT, U1))["args"] == "ref_abc"


async def test_another_user_cannot_solve_someone_elses_question(env):
    await setmode("new")
    await start(env, U1, True)
    rec = await SC._get(BOT, U1)
    cb = Cb(f"scp:{rec['t']}:{rec['ans']}", uid=U2)                                  # U2 چالشی ندارد
    await SC.scp_answer(cb, types.SimpleNamespace(), types.SimpleNamespace())
    assert not await SC.is_verified(BOT, U2) and not await SC.is_verified(BOT, U1)


async def test_gate_never_crashes_start_even_if_storage_fails(env, monkeypatch):
    await setmode("new")

    async def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(SC, "_get", boom)
    blocked, m = await start(env, U1, True)
    assert blocked is False                                                          # خطا ⇒ کاربر وارد می‌شود، /start خراب نمی‌شود


async def test_owner_menu_sets_the_mode_and_survives_reopen(env, monkeypatch):
    r = await row(env)

    async def ok(cb, bot): return r
    monkeypatch.setattr(tenant, "_owner_ok", ok)

    class M2:
        def __init__(self): self.texts = []

        async def edit_text(self, text, **k): self.texts.append(text)

        async def answer(self, text, **k): self.texts.append(text)

    class C2:
        def __init__(self, d):
            self.data, self.message, self.answers = d, M2(), []

        async def answer(self, text=None, **k): self.answers.append(text)
    for d, m in (("scpm:menu", "0"), ("scpm:new", "new"), ("scpm:all", "all"), ("scpm:0", "0")):
        c = C2(d)
        await SC.scp_menu(c, types.SimpleNamespace())
        assert await SC.mode(BOT) == m and "کپچا هنگامِ استارت" in c.message.texts[-1]
    await tenant.set_setting(BOT, SC.SETTING_KEY, "garbage")
    assert await SC.mode(BOT) == "0"                                                # مقدارِ خراب ⇒ خاموش


def test_wiring_menu_registered_in_both_menu_modes_and_nav_exempt_and_referral_not_asked_twice():
    from pathlib import Path
    src = Path("tenant/__init__.py").read_text(encoding="utf-8")
    assert '("🧩 کپچا هنگامِ استارت", "scpm:menu")' in src                           # دکمه در تنظیمات (حالتِ شیشه‌ای)
    assert '"🧩 کپچا هنگامِ استارت": _scap_menu_entry' in src                        # و دیسپچرِ حالتِ کیبوردی (باگِ همیشگی)
    assert "include_router(start_captcha_router)" in src
    assert "_captcha_passed=(_scap.SCAP_REPLAY.get() is not None)" in src
    kb = SC._kb("abc123", [1, 2, 3, 4])
    out = tenant._nav_inject(kb, True)
    assert "mbtn:home" not in [b.callback_data for r in out.inline_keyboard for b in r]        # دکمهٔ منوی اصلی زیرِ کپچا نمی‌آید
    i = src.index("if await _scap.gate(msg, bot, row, was_created")
    assert i < src.index("ref_code = None", i) and i < src.index("await grant_welcome_bonus(bot, row, u)", i)   # قبل از دعوت و هدیه


# ───────────── سرتاسری: tenant_startِ واقعی ─────────────
async def test_real_start_shows_only_the_captcha_then_the_welcome_after_solving(env, monkeypatch):
    """/startِ واقعی (DB واقعی): بارِ اول فقط کپچا (بدونِ خوش‌آمد/منو)؛ بعد از جوابِ درست، همان /start بازپخش و خوش‌آمد فرستاده می‌شود."""
    from aiogram.filters import CommandObject
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    await setmode("new")
    await tenant.set_setting(BOT, "welcome_text", "WELCOME-TEXT-XYZ")
    await tenant.set_setting(BOT, "menu_mode", "inline")
    sent = []

    class Bot_:
        token = "3:T"
        id = 3

        async def send_message(self, chat_id, text, **kw):
            sent.append(text)
            return types.SimpleNamespace(message_id=9)
    bot = Bot_()

    class RealishMsg:
        def __init__(self, text="/start"):
            self.text, self.message_id = text, 5
            self.chat = types.SimpleNamespace(id=555, type="private")
            self.from_user = types.SimpleNamespace(id=555, full_name="newbie", username="nb", is_bot=False)

        async def answer(self, text="", **kw):
            sent.append(text)
            return types.SimpleNamespace(message_id=len(sent) + 20, chat=self.chat)
        reply = answer

        async def answer_sticker(self, *a, **k): return types.SimpleNamespace(message_id=1)

        async def react(self, *a, **k): return None

        async def answer_photo(self, *a, **k): sent.append("photo"); return types.SimpleNamespace(message_id=1)

        async def delete(self): pass
    st = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=3, chat_id=555, user_id=555))
    await tenant.tenant_start(RealishMsg(), CommandObject(prefix="/", command="start", args=None), bot, st)
    assert any("تأییدِ هویت" in t for t in sent), sent
    assert not any("WELCOME-TEXT-XYZ" in t for t in sent), sent                        # خوش‌آمد/منو هنوز نیامده
    rec = await SC._get(BOT, 555)
    assert rec and rec["new"] is True
    sent.clear()

    class RealCb:
        data = f"scp:{rec['t']}:{rec['ans']}"
        from_user = User(id=555, is_bot=False, first_name="newbie")

        class message:
            chat = Chat(id=555, type="private")

            @staticmethod
            async def edit_text(text, **k): sent.append("EDIT:" + text)

            @staticmethod
            async def answer(text="", **kw):
                sent.append(text)
                return types.SimpleNamespace(message_id=30)

        @staticmethod
        async def answer(*a, **k): return None
    # tenant_startِ بازپخش‌شده روی یک Messageِ واقعیِ aiogram کار می‌کند؛ پاسخ‌هایش را از طریقِ bot.send_message/Message.answer می‌گیریم
    from aiogram.types import Message as AMsg

    async def fake_answer(self, text="", **kw):
        sent.append(text)
        return types.SimpleNamespace(message_id=len(sent) + 40, chat=self.chat)
    monkeypatch.setattr(AMsg, "answer", fake_answer)
    await SC.scp_answer(RealCb, bot, st)
    assert await SC.is_verified(BOT, 555) and await SC._get(BOT, 555) is None
    assert any("WELCOME-TEXT-XYZ" in t for t in sent), sent                              # بعد از حل: خوش‌آمد
