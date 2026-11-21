"""پنل مدیریتِ سروش: کدِ دسترسی/نشست/قفل، منوها، کارهایِ حساس با step-up، پیامِ همگانی."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_rubika as R
import platform_sadmin as P
import platform_sadmin_actions as A

NOW = dt.datetime(2026, 10, 22, 12, 0)
CODE = "my-secret-code-77"


class FakeIO:
    def __init__(self):
        self.sent, self.deleted = [], []

    async def send(self, chat, text, rows=None):
        self.sent.append((chat, text, rows))

    async def delete(self, chat, message_id):
        self.deleted.append((chat, message_id))

    async def download(self, file_id):
        return b"\xff\xd8photo"

    @property
    def last(self):
        return self.sent[-1][1]

    @property
    def last_rows(self):
        return self.sent[-1][2]


class FakeTG:
    def __init__(self):
        self.msgs, self.copies, self.photos = [], [], []
        self._mid = 100

    async def send_message(self, chat, text, **kw):
        self.msgs.append((chat, text))
        self._mid += 1
        return SimpleNamespace(message_id=self._mid)

    async def send_photo(self, chat, f, **kw):
        self.photos.append((chat, kw))
        self._mid += 1
        return SimpleNamespace(message_id=self._mid)

    async def send_video(self, chat, f, **kw):
        return await self.send_photo(chat, f, **kw)

    send_voice = send_audio = send_document = send_video

    async def copy_message(self, chat_id, from_chat_id, message_id, reply_markup=None):
        self.copies.append((chat_id, message_id, reply_markup))


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (R, A, config):
        monkeypatch.setattr(mod, "Session", maker)
    monkeypatch.setattr(P, "PLATFORM_ADMIN_ID", 42)
    async def inv(token):
        return None
    monkeypatch.setattr(A, "_invalidate", inv)
    async with maker() as s:
        s.add_all([
            models.Bot(id=1, owner_id=900, token="1:A", username="alpha_bot", field="vpn_sales", plan="growth",
                       status="active", plan_expires_at=NOW + dt.timedelta(days=3), created_at=NOW),
            models.Bot(id=2, owner_id=901, token="2:B", username="beta_bot", field="virtual_service", plan="pro",
                       status="active", plan_expires_at=NOW + dt.timedelta(days=60), created_at=NOW),
            models.Bot(id=3, owner_id=902, token="3:C", username="gamma_bot", field="vpn_sales", plan="free",
                       status="paused", archived_at=NOW - dt.timedelta(days=2), archive_reason="nonpayment",
                       plan_expires_at=NOW - dt.timedelta(days=2), created_at=NOW),
            models.PlatformUser(telegram_id=900, full_name="Ali", username="ali", balance=100_000, last_active_at=NOW),
            models.PlatformUser(telegram_id=901, full_name="Sara", username="sara", balance=5_000, last_active_at=NOW),
            models.PlatformUser(telegram_id=902, full_name="Reza", balance=0, is_banned=True),
            models.PlatformUser(telegram_id=903, full_name="Nima", balance=0, last_active_at=NOW - dt.timedelta(days=90)),
        ])
        await s.commit()
    await R._set(P.K_EN, "1")
    await R._set(P.K_TOKEN, "tok")
    await R._set(P.K_HASH, P.hash_code(CODE))
    io, tg = FakeIO(), FakeTG()
    clock = {"now": NOW}
    console = P.Console(io, tg, now_fn=lambda: clock["now"])
    yield SimpleNamespace(maker=maker, io=io, tg=tg, c=console, clock=clock)
    await engine.dispose()


def msg(text="", uid="7", chat="7", kind="text", **kw):
    m = {"chat_id": chat, "user_id": uid, "first_name": "Admin", "last_name": "", "username": "adm", "text": text,
         "kind": kind, "message_id": 55, "file_id": "", "file_name": "", "file_type": ""}
    m.update(kw)
    return m


async def say(env, text="", **kw):
    await env.c.handle(msg(text, **kw))


async def login(env):
    await say(env, CODE)
    assert env.tg.msgs and "ورود موفق" in env.tg.msgs[-1][1]


# ─────────────── کد و نشست ───────────────

def test_hash_roundtrip_and_no_plaintext():
    h = P.hash_code("abc12345")
    assert "abc12345" not in h and P.check_hash("abc12345", h) and not P.check_hash("abc12346", h)
    assert not P.check_hash("x", "garbage")


@pytest.mark.asyncio
async def test_unauthenticated_sees_only_prompt(env):
    await say(env, "/start")
    assert "کدِ دسترسی" in env.io.last and env.io.last_rows == []
    assert not env.tg.msgs


@pytest.mark.asyncio
async def test_wrong_code_counts_and_message_deleted(env):
    await say(env, "wrong-code-1")
    assert "کدِ نادرست (1/5)" in env.io.last and env.io.deleted == [("7", 55)]
    await say(env, "📊 آمار")                                  # بدونِ نشست، آمار نمی‌دهد
    assert "آمار" not in env.io.last or "نادرست" in env.io.last


@pytest.mark.asyncio
async def test_login_success_notifies_telegram_and_shows_menu(env):
    await login(env)
    assert env.io.deleted[0] == ("7", 55)                       # کد از چتِ سروش پاک شد
    assert "پنلِ مدیریت" in env.io.last and [P.BTN_STATS, P.BTN_BOTS] in env.io.last_rows
    assert env.tg.msgs[-1][0] == 42 and "@adm" in env.tg.msgs[-1][1]


@pytest.mark.asyncio
async def test_lockout_after_five_failures_blocks_even_correct_code(env):
    for i in range(5):
        await say(env, f"bad-{i}")
    assert "قفل" in env.io.last and "هشدار" in env.tg.msgs[-1][1] or "قفل" in env.tg.msgs[-1][1]
    await say(env, CODE)
    assert "قفل" in env.io.last and not any("ورود موفق" in t for _, t in env.tg.msgs)
    env.clock["now"] = NOW + dt.timedelta(minutes=16)
    await say(env, CODE)
    assert any("ورود موفق" in t for _, t in env.tg.msgs)


@pytest.mark.asyncio
async def test_otp_is_single_use_and_expires(env):
    await R._set(P.K_HASH, "")
    await P._jset(P.K_OTP, {"h": P.hash_code("12345678"), "x": P._iso(NOW + dt.timedelta(minutes=5))})
    await say(env, "12345678")
    assert any("کدِ یک‌بارمصرف" in t for _, t in env.tg.msgs)
    await P.end_session("7")
    await say(env, "12345678")                                  # دوباره کار نمی‌کند
    assert "نادرست" in env.io.last
    await P._jset(P.K_OTP, {"h": P.hash_code("87654321"), "x": P._iso(NOW - dt.timedelta(minutes=1))})
    await say(env, "87654321")
    assert "نادرست" in env.io.last


@pytest.mark.asyncio
async def test_session_slides_expires_and_epoch_kill(env):
    await login(env)
    env.clock["now"] = NOW + dt.timedelta(minutes=20)
    await say(env, P.BTN_STATS)
    assert "آمار" in env.io.last                                # نشست تمدید شد
    env.clock["now"] = NOW + dt.timedelta(minutes=20 + 31)
    await say(env, P.BTN_STATS)
    assert "کدِ دسترسی" in env.io.last                          # منقضی
    env.clock["now"] = NOW
    await login(env)
    await R._set(P.K_EPOCH, "1")                                # «قطعِ همهٔ نشست‌ها» از تلگرام
    await say(env, P.BTN_STATS)
    assert "کدِ دسترسی" in env.io.last


@pytest.mark.asyncio
async def test_session_bound_to_soroush_user(env):
    await login(env)
    await say(env, P.BTN_STATS, uid="999")                      # همان چت ولی کاربرِ دیگر
    assert "کدِ دسترسی" in env.io.last


@pytest.mark.asyncio
async def test_disabled_or_no_code(env):
    await R._set(P.K_EN, "0")
    await say(env, CODE)
    assert "فعال نیست" in env.io.last and not env.tg.msgs


# ─────────────── آمار و ربات‌ها ───────────────

@pytest.mark.asyncio
async def test_stats_text(env):
    await login(env)
    await say(env, P.BTN_STATS)
    t = env.io.last
    assert "ربات‌ها: 3" in t and "بایگانی: 1" in t and "کاربرانِ بات‌ساز: 4" in t and "بن: 1" in t
    assert "105,000" in t                                       # مجموعِ کیف‌پول‌ها


@pytest.mark.asyncio
async def test_bot_search_card_lock_unlock_days(env):
    await login(env)
    await say(env, P.BTN_BOTS)
    await say(env, "alpha")
    assert "alpha_bot" in env.io.last and "آخرین فعالیت" in env.io.last and P.BTN_DELETE in sum(env.io.last_rows, [])
    await say(env, P.BTN_LOCK)
    await say(env, "تخلف")
    async with env.maker() as s:
        b = await s.get(models.Bot, 1)
    assert b.status == "locked" and b.lock_reason == "تخلف"
    assert any(c == 900 and "قفل شد" in t for c, t in env.tg.msgs)           # مالک خبر گرفت
    await say(env, P.BTN_UNLOCK)
    async with env.maker() as s:
        assert (await s.get(models.Bot, 1)).status == "active"
    await say(env, P.BTN_ADDDAYS)
    await say(env, "۵")
    async with env.maker() as s:
        assert (await s.get(models.Bot, 1)).plan_expires_at == NOW + dt.timedelta(days=8)


@pytest.mark.asyncio
async def test_bot_presets_and_hash_open(env):
    await login(env)
    await say(env, P.BTN_BOTS)
    await say(env, P.BTN_ARCH)
    assert "gamma_bot" in env.io.last and "alpha_bot" not in env.io.last
    await say(env, P.BTN_EXP)
    assert "alpha_bot" in env.io.last and "beta_bot" not in env.io.last
    await say(env, "#2")
    assert "beta_bot" in env.io.last


@pytest.mark.asyncio
async def test_delete_bot_requires_stepup_code(env, monkeypatch):
    purged = []

    async def purge(bot_id):
        async with env.maker() as s:
            b = await s.get(models.Bot, bot_id)
            tok = b.token
            await s.delete(b)
            await s.commit()
        purged.append(bot_id)
        return tok

    class TB:
        async def __aenter__(self):
            return SimpleNamespace(delete_webhook=lambda: asyncio.sleep(0))

        async def __aexit__(self, *a):
            return False

    import bot_utils
    monkeypatch.setattr(config, "purge_bot", purge)
    monkeypatch.setattr(bot_utils, "_temp_bot", lambda token: TB())
    await login(env)
    await say(env, P.BTN_BOTS)
    await say(env, "#2")
    await say(env, P.BTN_DELETE)
    await say(env, "دلیل تست")
    assert "تأییدِ نهایی" in env.io.last and not purged
    await say(env, "wrong-code-xx")                              # کدِ اشتباه ⇒ حذف نمی‌شود
    assert "نادرست" in env.io.last and not purged
    await say(env, CODE)
    assert purged == [2] and "برایِ همیشه حذف شد" in env.io.last
    assert any(c == 901 and "حذف شد" in t and "دلیل تست" in t for c, t in env.tg.msgs)
    assert any("حذفِ ربات #2" in t for c, t in env.tg.msgs if c == 42)       # ممیزی به تلگرامِ مدیر


@pytest.mark.asyncio
async def test_partner_bot_not_deletable_from_soroush(env):
    async with env.maker() as s:
        b = await s.get(models.Bot, 2)
        b.plan = "partner"
        await s.commit()
    ok, msg_ = await A.delete_bot(2, "x", None)
    assert not ok and "همکار" in msg_


# ─────────────── کاربران و کیف پول ───────────────

@pytest.mark.asyncio
async def test_user_search_card_and_ban(env):
    await login(env)
    await say(env, P.BTN_USERS)
    await say(env, "ali")
    assert "Ali" in env.io.last and "100,000" in env.io.last and "alpha_bot" in env.io.last
    await say(env, P.BTN_BAN)
    async with env.maker() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 900))
    assert u.is_banned
    await say(env, P.BTN_UNBAN)
    async with env.maker() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 900))
    assert not u.is_banned


@pytest.mark.asyncio
async def test_wallet_adjust_flow_with_stepup_and_user_notice(env):
    await login(env)
    await say(env, P.BTN_WALLET)
    await say(env, "900")
    assert "مبلغ را بفرست" in env.io.last
    await say(env, "۵۰٬۰۰۰ جبرانِ قطعی")
    assert "تأییدِ نهایی" in env.io.last
    await say(env, CODE)
    async with env.maker() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 900))
        tx = (await s.scalars(select(models.PlatformTxn))).all()
    assert int(u.balance) == 150_000 and len(tx) == 1 and tx[0].kind == "admin" and tx[0].amount == 50_000
    assert any(c == 900 and "50,000" in t and "جبرانِ قطعی" in t for c, t in env.tg.msgs)
    assert "موجودی 150,000" in env.io.last


@pytest.mark.asyncio
async def test_wallet_deduct_cannot_go_negative(env):
    ok, m_ = await A.adjust_wallet(901, -999_999, "x", None)
    assert not ok and "کافی نیست" in m_
    ok, m_ = await A.adjust_wallet(901, -5_000, "x", None)
    assert ok and "0 تومان" in m_


# ─────────────── پیامِ همگانی ───────────────

@pytest.mark.asyncio
async def test_audience_ids(env):
    assert await A.audience_ids("all", NOW) == [900, 901, 903]                # بن‌شده‌ها نه
    assert await A.audience_ids("owners", NOW) == [900, 901]                  # 902 بن است
    assert await A.audience_ids("owners_active", NOW) == [900, 901]
    assert await A.audience_ids("expiring", NOW) == [900]
    assert await A.audience_ids("recent30", NOW) == [900, 901]
    assert await A.audience_ids("plan:pro", NOW) == [901]
    assert await A.audience_ids("nope", NOW) == []


def test_render_text_and_button():
    assert A.render_text("سلام **علی** و __تو__ <b>x</b>") == "سلام <b>علی</b> و <i>تو</i> &lt;b&gt;x&lt;/b&gt;"
    assert A.render_text("[سایت](https://a.com?x=1&y=2)") == '<a href="https://a.com?x=1&amp;y=2">سایت</a>'
    assert A.render_text("html:<b>خام</b>") == "<b>خام</b>"
    assert A.parse_button("ورود | https://t.me/x") == ("ورود", "https://t.me/x")
    assert A.parse_button("بدون لینک") is None and A.parse_button("x | javascript:alert(1)") is None


@pytest.mark.asyncio
async def test_broadcast_full_flow(env):
    await login(env)
    await say(env, P.BTN_BC)
    assert "همهٔ کاربرانِ بات‌ساز: 3" in env.io.last
    await say(env, "مالکانِ رباتِ فعال")
    await say(env, "سلام **دوستان**")
    assert env.tg.msgs[-1][1] == "سلام <b>دوستان</b>" and "پیش‌نمایش" in env.io.last
    await say(env, "ورود | https://t.me/x")
    assert "2 نفر" in env.io.last and P.BTN_SEND in sum(env.io.last_rows, [])
    await say(env, P.BTN_TEST)
    assert env.tg.copies[-1][0] == 42
    await say(env, P.BTN_SEND)
    assert "تأییدِ نهایی" in env.io.last
    await say(env, CODE)
    assert "ارسال شروع شد" in env.io.last
    await asyncio.gather(*env.c.bg)
    sent_to = [c for c, _, _ in env.tg.copies if c != 42]
    assert sent_to == [900, 901]
    assert env.tg.copies[-1][2].inline_keyboard[0][0].url == "https://t.me/x"
    assert "تمام شد" in env.io.sent[-1][1] and "موفق: 2" in env.io.sent[-1][1]


@pytest.mark.asyncio
async def test_broadcast_media_is_downloaded_and_sent_to_telegram(env):
    await login(env)
    await say(env, P.BTN_BC)
    await say(env, "همهٔ کاربرانِ بات‌ساز")
    await say(env, "کپشن **تست**", kind="file", file_id="F1", file_name="a.jpg", file_type="photo")
    assert env.tg.photos and env.tg.photos[0][1]["caption"] == "کپشن <b>تست</b>"
    assert "پیش‌نمایش" in env.io.last


@pytest.mark.asyncio
async def test_broadcast_stop_and_resume(env):
    job = {"id": 1, "ids": [1, 2, 3, 4], "idx": 0, "sent": 0, "failed": 0, "src_chat": 42, "src_msg": 9,
           "button": None, "status": "sending", "chat": "7", "aud": "x"}
    flag = {"n": 0}

    async def stop():
        flag["n"] += 1
        return flag["n"] > 2                                    # بعد از ۲ نفر توقف

    async def save(j):
        await P._jset(P.K_BC, j)

    async def nosleep(_):
        return None

    out = await A.run_broadcast(job, env.tg, save, stop, sleep=nosleep)
    assert out["status"] == "cancelled" and out["sent"] == 2 and out["idx"] == 2
    # ازسرگیری از همان‌جا (مثلاً بعد از ری‌استارت)
    saved = await P._jget(P.K_BC)
    assert saved["idx"] == 2 and saved["status"] == "cancelled"
    saved["status"] = "sending"
    await P._jset(P.K_BC, saved)
    await R._set(P.K_BC_STOP, "")
    await env.c.resume_job()
    await asyncio.gather(*env.c.bg)
    assert [c for c, _, _ in env.tg.copies][-2:] == [3, 4]


@pytest.mark.asyncio
async def test_broadcast_retry_after_and_failures(env):
    from aiogram.exceptions import TelegramRetryAfter

    class Flaky:
        def __init__(self):
            self.calls = []
            self.limited = False

        async def copy_message(self, chat_id, from_chat_id, message_id, reply_markup=None):
            self.calls.append(chat_id)
            if chat_id == 2 and not self.limited:
                self.limited = True
                raise TelegramRetryAfter(method=None, message="x", retry_after=1)
            if chat_id == 3:
                raise RuntimeError("blocked")

    slept = []

    async def sl(s):
        slept.append(s)

    async def save(j):
        return None

    async def stop():
        return False

    job = {"ids": [1, 2, 3], "idx": 0, "sent": 0, "failed": 0, "src_chat": 42, "src_msg": 9, "button": None,
           "status": "sending"}
    out = await A.run_broadcast(job, Flaky(), save, stop, sleep=sl)
    assert out["sent"] == 2 and out["failed"] == 1 and 1 in slept and out["status"] == "done"


# ─────────────── سیستم و تلگرام ───────────────

@pytest.mark.asyncio
async def test_system_page(env):
    await login(env)
    await say(env, P.BTN_SYS)
    assert "آخرین بک‌آپِ محلی" in env.io.last and "تیکتِ باز" in env.io.last and "login-static" in env.io.last


@pytest.mark.asyncio
async def test_logout_ends_session(env):
    await login(env)
    await say(env, P.BTN_LOGOUT)
    await say(env, P.BTN_STATS)
    assert "کدِ دسترسی" in env.io.last


@pytest.mark.asyncio
async def test_telegram_side_code_and_token_rules(env, monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    out, deleted = [], []

    async def answer(t, **kw):
        out.append(t)

    async def delete():
        deleted.append(1)

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))

    def tm(text):
        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=42), answer=answer, delete=delete)

    await P.sd_code_in(tm("short"), state)
    assert "حداقل" in out[-1] and deleted
    await P.sd_code_in(tm("a-very-long-code-1"), state)
    assert P.check_hash("a-very-long-code-1", await R._get(P.K_HASH)) and "a-very-long-code-1" not in await R._get(P.K_HASH)
    await R._set("splus_token", "support-token-123")
    await P.sd_token_in(tm("support-token-123"), state)
    assert "همان توکنِ رباتِ پشتیبانی" in out[-1]

    async def api(token, method, body=None, timeout=25, form=None):
        return {"ok": True, "result": {"id": 1}}

    import platform_splus
    monkeypatch.setattr(platform_splus, "api_call", api)
    await P.sd_token_in(tm("panel-token-12345"), state)
    assert await R._get(P.K_TOKEN) == "panel-token-12345"


@pytest.mark.asyncio
async def test_telegram_callbacks_otp_ttl_kill(env):
    out = []

    async def cb_answer(*a, **k):
        pass

    async def edit_text(t, **kw):
        out.append(t)

    async def answer(t, **kw):
        out.append(t)

    def cb(data):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=42), answer=cb_answer,
                               message=SimpleNamespace(edit_text=edit_text, answer=answer))

    await P.sd_cb(cb("psdm:otp"), None)
    import re
    code = re.search(r"<code>(\d{8})</code>", " ".join(out)).group(1)
    assert await P.verify_code(code, NOW) == "otp" and await P.verify_code(code, NOW) is None
    await P.sd_cb(cb("psdm:ttl"), None)
    assert await P._ttl_min() == 60
    await P.sd_cb(cb("psdm:kill"), None)
    assert await R._get(P.K_EPOCH) == "1"
    await P.sd_cb(cb("psdm:rand"), None)
    rand = re.search(r"<code>([\w-]+)</code>", out[-2] if "کدِ جدید" in out[-2] else " ".join(out[-3:])).group(1)
    assert await P.verify_code(rand, NOW) == "static"


@pytest.mark.asyncio
async def test_change_bot_plan_and_restore_from_archive(env, monkeypatch):
    import bot_utils
    hooks = []

    async def wh(token):
        hooks.append(token)

    monkeypatch.setattr(bot_utils, "set_tenant_webhook", wh)
    await login(env)
    await say(env, P.BTN_BOTS)
    await say(env, "#3")                                         # بایگانی‌شده
    await say(env, P.BTN_PLAN)
    await say(env, "حرفه‌ای")
    async with env.maker() as s:
        b = await s.get(models.Bot, 3)
    assert b.plan == "pro" and b.archived_at is None and b.status == "active" and hooks == ["3:C"]
    assert any(c == 902 and "تغییر کرد" in t for c, t in env.tg.msgs)
    ok, _ = await A.set_bot_plan(3, "partner", None)
    assert not ok
