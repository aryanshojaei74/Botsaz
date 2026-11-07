"""
تست‌هایِ بازیابیِ متنِ دکمه‌هایِ کیبوردِ پایینِ صفحه (PATCH_REPLY_LABEL_RESTORE_V1).

باگِ گزارش‌شده (ادمین سهیل): در حالتِ منویِ کیبوردی با ایموجیِ پرمیوم، دکمه‌ها (مثلاً «بازگشت به خانه»)
جواب نمی‌دادند. این تست‌ها جریانِ واقعی را با aiogram کامل می‌رانند:
  ارسالِ کیبورد از طریقِ Botِ واقعی (میان‌افزارِ خروجی ایموجی را به آیکون تبدیل می‌کند) ⇒
  رسیدنِ آپدیتِ «متنِ بدونِ ایموجی» ⇒ Dispatcherِ واقعی ⇒ هندلری که «🏠 بازگشت به خانه» می‌خواهد.
"""
from __future__ import annotations

import datetime
import json

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import (Chat, KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove, Update, User)

import premium_emoji_dynamic as ped
import reply_label_registry as rlr

TOKEN = "123456:TESTTOKEN"
CHAT = 777
HOME_ID = "5368324170671202286"


class FakeRedis:
    """جایگزینِ ساده‌ی redis.asyncio با همان چند متدی که ماژول لازم دارد."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.calls: list[str] = []
        self.fail = False

    async def set(self, key, value, ex=None):
        self.calls.append("set")
        if self.fail:
            raise ConnectionError("down")
        self.store[key] = value
        self.ttl[key] = ex

    async def get(self, key):
        self.calls.append("get")
        if self.fail:
            raise ConnectionError("down")
        return self.store.get(key)

    async def delete(self, key):
        self.calls.append("delete")
        if self.fail:
            raise ConnectionError("down")
        self.store.pop(key, None)


class CaptureSession(BaseSession):
    """جلسهٔ آزمایشی: درخواست‌هایِ *بعد از* میان‌افزارها را ذخیره می‌کند (به تلگرام نمی‌رود)."""

    def __init__(self):
        super().__init__()
        self.sent: list = []

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        return Message(message_id=len(self.sent), date=datetime.datetime.now(),
                       chat=Chat(id=getattr(method, "chat_id", CHAT) or CHAT, type="private"),
                       text=getattr(method, "text", "x"))


@pytest_asyncio.fixture
async def env(monkeypatch):
    # نگاشتِ پرمیوم: فقط 🏠 و 🎛 آیکون می‌شوند (⬅️ نه) — دقیقاً وضعیتِ اسکرین‌شاتِ سهیل
    async def provider(token):
        return {"🏠": (HOME_ID, "🏠"), "🎛": ("5000000000000000001", "🎛")} if token == TOKEN else None
    monkeypatch.setattr(ped, "_PROVIDERS", [provider])
    fake = FakeRedis()
    monkeypatch.setattr(rlr, "_redis", fake)
    monkeypatch.setattr(rlr, "_redis_init_tried", True)
    rlr._local.clear()
    session = CaptureSession()
    bot = Bot(TOKEN, session=session)
    yield bot, session, fake
    rlr._local.clear()


def _kb(*labels):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=l)] for l in labels], resize_keyboard=True)


async def _send_kb(bot, *labels, chat=CHAT):
    return await bot.send_message(chat, "منو", reply_markup=_kb(*labels))


def _update(text, chat=CHAT, chat_type="private", uid=1):
    msg = Message(message_id=10, date=datetime.datetime.now(),
                  chat=Chat(id=chat, type=chat_type),
                  from_user=User(id=uid, is_bot=False, first_name="Ali"), text=text)
    return Update(update_id=1, message=msg)


def _dispatcher(hits):
    """Dispatcher شبیهِ tenant: هندلرها با تطابقِ دقیقِ متنِ *با ایموجی*."""
    dp = Dispatcher()
    r = Router()

    @r.message(F.text == "🏠 بازگشت به خانه")
    async def home(m):
        hits.append(("home", m.text))

    @r.message(F.text == "🏠 تنظیم و مدیریت دکمه‌های صفحه اصلی")
    async def homebtns(m):
        hits.append(("homebtns", m.text))

    @r.message(F.text == "⬅️ بازگشت")
    async def back(m):
        hits.append(("back", m.text))

    @r.message(F.text)
    async def other(m):
        hits.append(("other", m.text))

    dp.include_router(r)
    return dp


# ───────────────────────── خروجی: ایموجی واقعاً به آیکون تبدیل می‌شود ─────────────────────────

@pytest.mark.asyncio
async def test_outgoing_keyboard_loses_the_emoji_from_button_text(env):
    """این همان ریشهٔ باگ است: متنِ دکمه‌ای که آیکون شد دیگر «🏠» ندارد."""
    bot, session, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه", "⬅️ بازگشت")
    btns = [b for row in session.sent[-1].reply_markup.keyboard for b in row]
    assert btns[0].text == "بازگشت به خانه" and btns[0].icon_custom_emoji_id == HOME_ID
    assert btns[1].text == "⬅️ بازگشت" and not btns[1].icon_custom_emoji_id


# ───────────────────────── باگ: بدونِ میان‌افزارِ ورودی هندلر نمی‌رسد ─────────────────────────

@pytest.mark.asyncio
async def test_bug_reproduced_without_the_inbound_middleware(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    hits = []
    dp = _dispatcher(hits)                                 # بدونِ install_reply_label_restore
    await dp.feed_update(bot, _update("بازگشت به خانه"))
    assert hits == [("other", "بازگشت به خانه")]           # هندلرِ «🏠 بازگشت به خانه» هرگز اجرا نشد


# ───────────────────────── رفع ─────────────────────────

@pytest.mark.asyncio
async def test_fix_routes_the_emoji_less_tap_to_the_original_handler(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه", "⬅️ بازگشت")
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("بازگشت به خانه"))
    assert hits == [("home", "🏠 بازگشت به خانه")]


@pytest.mark.asyncio
async def test_the_second_reported_button_settings_management(env):
    """«دکمه‌ها» در منویِ تنظیمات: «🏠 تنظیم و مدیریت دکمه‌های صفحه اصلی» هم با 🏠 شروع می‌شود."""
    bot, _, _ = env
    await _send_kb(bot, "🏠 تنظیم و مدیریت دکمه‌های صفحه اصلی", "🎛 دکمه‌ها و تنظیمات")
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("تنظیم و مدیریت دکمه‌های صفحه اصلی"))
    assert hits == [("homebtns", "🏠 تنظیم و مدیریت دکمه‌های صفحه اصلی")]


@pytest.mark.asyncio
async def test_buttons_that_kept_their_emoji_are_untouched(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه", "⬅️ بازگشت")
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("⬅️ بازگشت"))
    assert hits == [("back", "⬅️ بازگشت")]


@pytest.mark.asyncio
async def test_free_text_not_in_the_registry_is_untouched(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("سلام، یک سؤال دارم"))
    assert hits == [("other", "سلام، یک سؤال دارم")]


# ───────────────────────── محدوده و ایمنی ─────────────────────────

@pytest.mark.asyncio
async def test_registry_is_per_chat(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه", chat=CHAT)
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("بازگشت به خانه", chat=999))       # چتِ دیگر: کیبوردی نگرفته
    assert hits == [("other", "بازگشت به خانه")]


@pytest.mark.asyncio
async def test_group_chats_are_never_touched(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("بازگشت به خانه", chat_type="supergroup"))
    assert hits == [("other", "بازگشت به خانه")]


@pytest.mark.asyncio
async def test_latest_keyboard_replaces_the_previous_one(env):
    """کاربر فقط کیبوردِ آخر را می‌بیند؛ یادداشتِ کیبوردِ قبلی نباید متنِ دکمهٔ سادهٔ جدید را خراب کند."""
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه")                 # یادداشت: «بازگشت به خانه» ← با 🏠
    await _send_kb(bot, "بازگشت به خانه")                     # کیبوردِ جدید: دکمهٔ ساده و هم‌متن
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("بازگشت به خانه"))
    assert hits == [("other", "بازگشت به خانه")]              # ابهام = دست‌نزدن


@pytest.mark.asyncio
async def test_plain_button_with_same_text_in_the_same_keyboard_is_not_restored(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه", "بازگشت به خانه")
    assert await rlr.restore(TOKEN, CHAT, "بازگشت به خانه") is None


@pytest.mark.asyncio
async def test_two_different_labels_collapsing_to_one_text_are_ambiguous(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت", "🎛 بازگشت")
    assert await rlr.restore(TOKEN, CHAT, "بازگشت") is None


@pytest.mark.asyncio
async def test_removing_the_keyboard_forgets_the_labels(env):
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    assert await rlr.restore(TOKEN, CHAT, "بازگشت به خانه") == "🏠 بازگشت به خانه"
    await bot.send_message(CHAT, "بستن", reply_markup=ReplyKeyboardRemove())
    assert await rlr.restore(TOKEN, CHAT, "بازگشت به خانه") is None


@pytest.mark.asyncio
async def test_inline_keyboards_do_not_touch_the_registry(env):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    bot, _, _ = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    await bot.send_message(CHAT, "x", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 بازگشت به خانه", callback_data="mbtn:home")]]))
    assert await rlr.restore(TOKEN, CHAT, "بازگشت به خانه") == "🏠 بازگشت به خانه"


@pytest.mark.asyncio
async def test_bot_without_a_premium_map_is_completely_untouched(env, monkeypatch):
    bot, _, fake = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    async def none_provider(token):
        return None
    monkeypatch.setattr(ped, "_PROVIDERS", [none_provider])
    fake.calls.clear()
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("بازگشت به خانه"))
    assert hits == [("other", "بازگشت به خانه")]
    assert fake.calls == []                                   # حتی یک رفت‌وبرگشتِ Redis هم نشد


@pytest.mark.asyncio
async def test_install_is_idempotent(env):
    bot, _, _ = env
    dp = _dispatcher([])
    rlr.install_reply_label_restore(dp)
    rlr.install_reply_label_restore(dp)
    assert len(dp.message.outer_middleware) == 1


# ───────────────────────── Redis: چندین ورکر، ری‌استارت، خرابی ─────────────────────────

@pytest.mark.asyncio
async def test_registry_survives_a_process_restart_via_redis(env):
    bot, _, fake = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    assert any("بازگشت به خانه" in v for v in fake.store.values())
    rlr._local.clear()                                        # «ری‌استارت» یا ورکرِ دیگر: حافظهٔ پروسه خالی
    assert await rlr.restore(TOKEN, CHAT, "بازگشت به خانه") == "🏠 بازگشت به خانه"


@pytest.mark.asyncio
async def test_redis_entry_has_a_ttl_and_a_hashed_token_key(env):
    bot, _, fake = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    (key,) = fake.store.keys()
    assert TOKEN not in key and key.startswith("rl:") and key.endswith(f":{CHAT}")
    assert fake.ttl[key] == rlr._TTL_S


@pytest.mark.asyncio
async def test_identical_keyboard_resend_does_not_hit_redis_again(env):
    bot, _, fake = env
    await _send_kb(bot, "🏠 بازگشت به خانه")
    n = fake.calls.count("set")
    await _send_kb(bot, "🏠 بازگشت به خانه")
    await _send_kb(bot, "🏠 بازگشت به خانه")
    assert fake.calls.count("set") == n


@pytest.mark.asyncio
async def test_redis_down_falls_back_to_process_memory_and_never_raises(env):
    bot, _, fake = env
    fake.fail = True
    await _send_kb(bot, "🏠 بازگشت به خانه")                 # نوشتن خطا می‌دهد ولی ارسال سالم می‌ماند
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("بازگشت به خانه"))
    assert hits == [("home", "🏠 بازگشت به خانه")]


@pytest.mark.asyncio
async def test_a_failure_inside_the_middleware_never_drops_the_message(env, monkeypatch):
    bot, _, _ = env
    async def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(rlr, "restore", boom)
    hits = []
    dp = _dispatcher(hits)
    rlr.install_reply_label_restore(dp)
    await dp.feed_update(bot, _update("بازگشت به خانه"))
    assert hits == [("other", "بازگشت به خانه")]


@pytest.mark.asyncio
async def test_slow_cache_refresh_picks_up_a_keyboard_sent_by_another_worker(env, monkeypatch):
    bot, _, fake = env
    key = rlr._key(TOKEN, CHAT)
    fake.store[key] = json.dumps({"بازگشت به خانه": "🏠 بازگشت به خانه"}, ensure_ascii=False)
    assert await rlr.restore(TOKEN, CHAT, "بازگشت به خانه") == "🏠 بازگشت به خانه"
    fake.store[key] = json.dumps({"چیز دیگر": "🎛 چیز دیگر"}, ensure_ascii=False)
    # داخلِ پنجرهٔ کش هنوز نسخهٔ قبلی؛ بعد از آن نسخهٔ تازه
    monkeypatch.setattr(rlr, "_LOCAL_READ_TTL_S", 0.0)
    assert await rlr.restore(TOKEN, CHAT, "چیز دیگر") == "🎛 چیز دیگر"


# ───────────────────────── پیوندِ واقعی در main_tenant ─────────────────────────

def test_main_tenant_installs_the_middleware_on_the_tenant_dispatcher():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "main_tenant.py").read_text(encoding="utf-8")
    assert "install_reply_label_restore(tenant_dp)" in src
    # باید قبل از ثبتِ روترها باشد (outer middleware روی dp.message)
    assert src.index("install_reply_label_restore(tenant_dp)") < src.index("tenant_dp.include_router(tenant_router)")
