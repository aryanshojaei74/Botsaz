"""
تست‌هایِ «ویرایشِ درجا برایِ همهٔ دکمه‌هایِ کاربر در منویِ شیشه‌ای» (PATCH_GLASS_EDIT_GENERIC_V1).

درخواستِ ادمین (فدایی): در پنل شیشه‌ای رویِ هر دکمه‌ای که کاربر می‌زند همان پیام ویرایش شود، نه پیامِ تازه.

تست‌ها یک Dispatcherِ *واقعیِ* aiogram با روترِ واقعیِ tenant می‌سازند، آپدیتِ callbackِ واقعی می‌دهند و
درخواست‌هایی را که به API تلگرام می‌رسد (SendMessage / EditMessageText / DeleteMessage) ثبت می‌کنند.
"""
from __future__ import annotations

import ast
import datetime
import json
import pathlib
import re
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import DeleteMessage, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import (CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           ReplyKeyboardMarkup, KeyboardButton, Update, User)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.shared import glass_generic as GG

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHAT = 555
BOT_TG = 8354597347


# ═════════════ زیرساخت: نشستِ ثبت‌کنندهٔ API ═════════════

class Capture(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list = []
        self.fail_edit: str | None = None       # اگر رشته بود، EditMessageText با این خطا شکست می‌خورد

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append(method)
        if name == "EditMessageText" and self.fail_edit:
            raise RuntimeError(self.fail_edit)
        if name in ("SendMessage", "EditMessageText"):
            return Message(message_id=getattr(method, "message_id", None) or (1000 + len(self.calls)),
                           date=datetime.datetime.now(), chat=Chat(id=method.chat_id, type="private"),
                           text=getattr(method, "text", "x")).as_(bot)
        return True

    def api_calls(self):
        """فراخوانی‌هایِ واقعیِ پیام (بدونِ AnswerCallbackQuery که هر هندلر برایِ بستنِ اسپینر می‌زند)."""
        return [m for m in self.calls if type(m).__name__ != "AnswerCallbackQuery"]

    def kinds(self):
        return [type(m).__name__ for m in self.api_calls()]


def kb(*cbs):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=c, callback_data=c)] for c in cbs])


def make_bot():
    cap = Capture()
    return Bot("123:TESTTOKEN", session=cap), cap


def update(data, *, chat_type="private", text="منو", message_id=42, uid=CHAT, photo=False):
    u = User(id=uid, is_bot=False, first_name="Ali")
    m = Message(message_id=message_id, date=datetime.datetime.now(), chat=Chat(id=uid, type=chat_type),
                text=None if photo else text, from_user=User(id=BOT_TG, is_bot=True, first_name="bot"),
                reply_markup=kb("a"))
    return Update(update_id=1, callback_query=CallbackQuery(id="1", from_user=u, chat_instance="c", data=data, message=m))


# ═════════════ Dispatcherِ آزمایشی با هندلرهایِ ساختگی + میان‌افزارِ واقعی ═════════════

@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    from tenant.shared import public_api as _pa         # Session را هنگامِ import می‌گیرد؛ اگر قبلاً import شده باشد کهنه است
    for mod in (config, tenant, _pa):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    # میان‌افزارِ ایموجیِ پرمیوم (سراسری رویِ نشست‌هایِ aiogram) با Sessionِ اصلیِ پروژه از دیتابیس می‌خواند؛ این‌جا نباید
    import premium_emoji_dynamic as ped

    async def no_premium(_token): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    async with maker() as s:
        b = models.Bot(owner_id=1, token="123:TESTTOKEN", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b)
        await s.flush()
        s.add(models.BotUser(bot_id=b.id, telegram_id=CHAT, full_name="Ali", balance=0))
        await s.commit()
        bot_id = b.id
    await tenant.set_setting(bot_id, "menu_mode", "inline")

    r = Router(name="glass_test")
    GG.install_glass_generic(r)
    log = {}

    @r.callback_query(F.data == "page")               # صفحه: متن + دکمهٔ اینلاین
    async def page(cb, bot):
        await cb.message.answer("صفحهٔ تازه", reply_markup=kb("x", "y"))
        await cb.answer()

    @r.callback_query(F.data == "plain")              # پیامِ بی‌دکمه (ack)
    async def plain(cb, bot):
        await cb.message.answer("✅ ثبت شد")

    @r.callback_query(F.data == "progress")           # «صبر کن» بی‌دکمه، بعد حذف، بعد صفحهٔ نتیجه
    async def progress(cb, bot):
        m = await cb.message.answer("⏳ صبر کن")
        await m.delete()
        await cb.message.answer("نتیجه", reply_markup=kb("r"))

    @r.callback_query(F.data == "send_then_delete")   # صفحهٔ تازه بفرست، قدیمی را پاک کن
    async def send_then_delete(cb, bot):
        await cb.message.answer("صفحهٔ تازه", reply_markup=kb("n"))
        await cb.message.delete()

    @r.callback_query(F.data == "send_then_strip")    # صفحهٔ تازه، بعد دکمه‌هایِ قدیمی را بردار
    async def send_then_strip(cb, bot):
        await cb.message.answer("صفحهٔ تازه", reply_markup=kb("n"))
        await cb.message.edit_reply_markup(reply_markup=None)

    @r.callback_query(F.data == "edit_first")         # خودش ویرایش می‌کند، بعد پیامِ دیگر
    async def edit_first(cb, bot):
        await cb.message.edit_text("ویرایشِ خودم", reply_markup=kb("e"))
        await cb.message.answer("پیامِ دوم", reply_markup=kb("f"))

    @r.callback_query(F.data == "delete_first")
    async def delete_first(cb, bot):
        await cb.message.delete()
        await cb.message.answer("جایگزین", reply_markup=kb("g"))

    @r.callback_query(F.data == "two_pages")
    async def two_pages(cb, bot):
        await cb.message.answer("صفحهٔ اول", reply_markup=kb("1"))
        await cb.message.answer("صفحهٔ دوم", reply_markup=kb("2"))

    @r.callback_query(F.data == "reply_kb")
    async def reply_kb(cb, bot):
        await cb.message.answer("شماره‌ات را بفرست", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="انصراف")]], resize_keyboard=True))

    @r.callback_query(F.data == "other_chat")
    async def other_chat(cb, bot):
        await bot.send_message(999, "اعلان به ادمین", reply_markup=kb("z"))
        await cb.answer()

    @r.callback_query(F.data == "progress_edit")      # پیامِ پیشرفتِ دارایِ دکمه که بعداً ویرایش می‌شود
    async def progress_edit(cb, bot):
        m = await cb.message.answer("در حالِ بررسی…", reply_markup=kb("cancel"))
        await m.edit_text("تمام شد ✅", reply_markup=kb("ok"))

    @r.callback_query(F.data == "mbtn:home")
    async def mbtn_home(cb, bot):
        await cb.message.answer("خانه", reply_markup=kb("h"))

    dp = Dispatcher()
    dp.include_router(r)
    bot, cap = make_bot()

    async def fake_get_bot_row(_bot):
        async with maker() as s:
            from sqlalchemy import select
            return await s.scalar(select(models.Bot).where(models.Bot.id == bot_id))
    monkeypatch.setattr(tenant, "get_bot_row", fake_get_bot_row)
    yield SimpleNamespace(dp=dp, bot=bot, cap=cap, bot_id=bot_id, tenant=tenant, router=r)
    tenant._SETTING_CACHE.clear()
    await engine.dispose()


async def press(env, data, **kw):
    env.cap.calls.clear()
    await env.dp.feed_update(env.bot, update(data, **kw))
    return env.cap


def only(cap, *names):
    return [m for m in cap.api_calls() if type(m).__name__ in names]


# ═════════════ ۱) رفتارِ اصلی ═════════════

@pytest.mark.asyncio
async def test_a_page_is_edited_in_place_instead_of_sent(env):
    cap = await press(env, "page")
    assert cap.kinds() == ["EditMessageText"]
    assert only(cap, "SendMessage") == []
    e = only(cap, "EditMessageText")[0]
    assert (e.chat_id, e.message_id, e.text) == (CHAT, 42, "صفحهٔ تازه")
    assert [b.callback_data for r in e.reply_markup.inline_keyboard for b in r] == ["x", "y"]


@pytest.mark.asyncio
async def test_messages_without_a_keyboard_are_sent_normally(env):
    cap = await press(env, "plain")
    assert only(cap, "EditMessageText") == [] and len(only(cap, "SendMessage")) == 1


@pytest.mark.asyncio
async def test_progress_message_then_delete_then_result_page(env):
    """الگوی «⏳ صبر کن» بی‌دکمه ⇒ ارسالِ معمولی و حذفِ خودش؛ فقط *نتیجهٔ دارایِ دکمه* اگر اولین بود تبدیل می‌شد."""
    cap = await press(env, "progress")
    assert cap.kinds()[:2] == ["SendMessage", "DeleteMessage"]
    assert only(cap, "EditMessageText") == []          # اولین ارسال بی‌دکمه بود ⇒ تمامِ تبدیل غیرفعال


@pytest.mark.asyncio
async def test_send_then_delete_keeps_the_new_page(env):
    """الگوی «صفحهٔ تازه بفرست، قدیمی را پاک کن»: صفحهٔ تازه همان پیام است؛ حذف نباید انجام شود."""
    cap = await press(env, "send_then_delete")
    assert len(only(cap, "EditMessageText")) == 1
    assert only(cap, "DeleteMessage") == []


@pytest.mark.asyncio
async def test_send_then_strip_buttons_keeps_the_new_keyboard(env):
    cap = await press(env, "send_then_strip")
    assert len(only(cap, "EditMessageText")) == 1
    assert only(cap, "EditMessageReplyMarkup") == []


@pytest.mark.asyncio
async def test_handler_that_edits_first_is_left_alone(env):
    cap = await press(env, "edit_first")
    assert cap.kinds() == ["EditMessageText", "SendMessage"]     # ویرایشِ خودش + پیامِ دومِ واقعاً تازه


@pytest.mark.asyncio
async def test_handler_that_deletes_first_is_left_alone(env):
    cap = await press(env, "delete_first")
    assert cap.kinds() == ["DeleteMessage", "SendMessage"]


@pytest.mark.asyncio
async def test_only_the_first_page_is_converted(env):
    cap = await press(env, "two_pages")
    assert cap.kinds() == ["EditMessageText", "SendMessage"]


@pytest.mark.asyncio
async def test_reply_keyboard_prompts_are_never_converted(env):
    cap = await press(env, "reply_kb")
    assert cap.kinds() == ["SendMessage"]


@pytest.mark.asyncio
async def test_messages_to_other_chats_are_untouched(env):
    cap = await press(env, "other_chat")
    assert [(type(m).__name__, m.chat_id) for m in cap.api_calls()] == [("SendMessage", 999)]


@pytest.mark.asyncio
async def test_later_edit_of_the_converted_page_still_applies(env):
    """پیامِ پیشرفت که بعداً ویرایش می‌شود همان صفحه است؛ آخرین متن باید بماند."""
    cap = await press(env, "progress_edit")
    texts = [m.text for m in cap.api_calls() if type(m).__name__ == "EditMessageText"]
    assert texts == ["در حالِ بررسی…", "تمام شد ✅"]
    assert only(cap, "SendMessage") == []


# ═════════════ ۲) شرط‌هایِ فعال‌شدن ═════════════

@pytest.mark.asyncio
async def test_not_active_in_reply_keyboard_mode(env):
    await env.tenant.set_setting(env.bot_id, "menu_mode", "reply")
    cap = await press(env, "page")
    assert cap.kinds() == ["SendMessage"]


@pytest.mark.asyncio
async def test_not_active_in_groups_or_on_media_messages(env):
    assert (await press(env, "page", chat_type="supergroup")).kinds() == ["SendMessage"]
    assert (await press(env, "page", photo=True)).kinds() == ["SendMessage"]


@pytest.mark.asyncio
async def test_main_button_prefix_is_left_to_its_own_path(env):
    cap = await press(env, "mbtn:home")
    assert cap.kinds() == ["SendMessage"]            # این میان‌افزار دخالت نمی‌کند (مسیرِ mbtn قاعدهٔ خودش را دارد)


@pytest.mark.asyncio
async def test_per_bot_and_global_kill_switches(env, monkeypatch):
    await env.tenant.set_setting(env.bot_id, "glass_edit_generic", "0")
    assert (await press(env, "page")).kinds() == ["SendMessage"]
    await env.tenant.set_setting(env.bot_id, "glass_edit_generic", "1")
    env.tenant._SETTING_CACHE.clear()
    assert (await press(env, "page")).kinds()[0] == "EditMessageText"
    monkeypatch.setenv("GLASS_EDIT_GENERIC", "0")
    assert (await press(env, "page")).kinds() == ["SendMessage"]


@pytest.mark.asyncio
async def test_skip_listed_handlers_are_untouched(env, monkeypatch):
    monkeypatch.setattr(GG, "SKIP_HANDLERS", frozenset({GG.handler_key(SimpleNamespace(callback=env.router.callback_query.handlers[0].callback))}))
    cap = await press(env, "page")
    assert cap.kinds() == ["SendMessage"]


# ═════════════ ۳) خطاها هرگز رفتارِ قبلی را نمی‌شکنند ═════════════

@pytest.mark.asyncio
async def test_edit_failure_falls_back_to_a_normal_send_without_deleting(env):
    env.cap.fail_edit = "Bad Request: message can't be edited"
    cap = await press(env, "page")
    assert cap.kinds() == ["EditMessageText", "SendMessage"]        # تلاش برایِ ویرایش، بعد ارسالِ معمولی
    assert only(cap, "DeleteMessage") == []                          # پیامِ قبلی حذف نشد


@pytest.mark.asyncio
async def test_message_not_modified_counts_as_success(env):
    env.cap.fail_edit = "Bad Request: message is not modified"
    cap = await press(env, "page")
    assert only(cap, "SendMessage") == []


@pytest.mark.asyncio
async def test_a_crash_in_the_begin_step_runs_the_handler_normally(env, monkeypatch):
    import tenant

    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(tenant, "get_bot_row", boom)
    cap = await press(env, "page")
    assert cap.kinds() == ["SendMessage"]


@pytest.mark.asyncio
async def test_context_is_cleared_between_callbacks(env):
    await press(env, "page")
    assert env.tenant._GLASS_EDIT.get() is None
    cap = await press(env, "plain")
    assert cap.kinds() == ["SendMessage"]


def test_install_is_idempotent():
    r = Router(name="x")
    GG.install_glass_generic(r)
    GG.install_glass_generic(r)
    assert len(r.callback_query.middleware._middlewares) == 1


# ═════════════ ۴) هندلرهایِ واقعیِ کاربر ═════════════

class FakeFsm:
    async def clear(self): ...
    async def set_state(self, *a, **k): ...
    async def get_data(self): return {}
    async def update_data(self, **k): ...


@pytest.mark.asyncio
async def test_real_wallet_gateway_pages_are_edited_in_place(env, monkeypatch):
    """زرین‌پال/زیبال/حساب‌پی: صفحهٔ «مبلغ را انتخاب کن» قبلاً هر بار پیامِ تازه بود."""
    import tenant
    from tenant.gateways import zarinpal_wallet as zw
    row = await tenant.get_bot_row(env.bot)

    async def configured(_): return True
    monkeypatch.setattr(zw, "_configured", configured)
    async def one_of(*a, **k): return row
    # هندلرِ واقعی را روی همان Dispatcherِ آزمایشی ثبت می‌کنیم (میان‌افزارِ واقعی رویِ روتر نصب است)
    env.router.callback_query.register(zw.zarinpal_start, F.data == "w:zarinpal")
    cap = await press(env, "w:zarinpal")
    assert only(cap, "SendMessage") == [], cap.kinds()
    e = only(cap, "EditMessageText")[0]
    assert e.message_id == 42 and "مبلغ شارژ" in e.text
    cbs = [b.callback_data for r in e.reply_markup.inline_keyboard for b in r]
    assert any(c.startswith("wzp:amt:") for c in cbs) and "wzp:custom" in cbs


@pytest.mark.asyncio
async def test_real_profile_api_page_is_edited_in_place(env, monkeypatch):
    import tenant
    from tenant.gateways import custom_domain

    async def base(_bid): return "https://shop.example"
    monkeypatch.setattr(custom_domain, "get_bot_webhook_base", base)
    await tenant.set_setting(env.bot_id, "api_open_to_all", "1")
    env.router.callback_query.register(tenant.pubapi_key_show, F.data == "pubapi:key")
    cap = await press(env, "pubapi:key")
    assert only(cap, "SendMessage") == [], cap.kinds()
    assert "EditMessageText" in cap.kinds()


# ═════════════ ۵) فهرستِ ایمنی همیشه تازه است ═════════════

def _risky_handlers_by_ast():
    CB = {"cb", "call", "query", "callback", "c"}

    def is_cbmsg(n, name):
        return isinstance(n, ast.Attribute) and n.attr == "message" and isinstance(n.value, ast.Name) and n.value.id == name
    out = set()
    for p in (ROOT / "tenant").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            t = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        mod = str(p.relative_to(ROOT))[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        for fn in ast.walk(t):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            if not any("callback_query" in ast.unparse(d) for d in fn.decorator_list):
                continue
            cb = next((a.arg for a in fn.args.args if a.arg in CB), None)
            if not cb:
                continue
            ev = []
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and is_cbmsg(n.func.value, cb):
                    a = n.func.attr
                    if a in ("answer", "answer_photo", "answer_document"):
                        ev.append((n.lineno, "send"))
                    elif a == "delete":
                        ev.append((n.lineno, "del"))
                    elif a in ("edit_text", "edit_caption", "edit_reply_markup"):
                        ev.append((n.lineno, "edit"))
            ev.sort()
            kinds = [k for _, k in ev]
            if "send" in kinds and any(k in ("del", "edit") for k in kinds[kinds.index("send") + 1:]):
                out.add(f"{mod}.{fn.name}")
    return out


def test_skip_list_covers_every_handler_that_touches_the_old_message_after_sending():
    """اگر این تست شکست خورد: هندلرِ جدیدی بعد از ارسالِ صفحهٔ تازه پیامِ قبلی را ویرایش/حذف می‌کند.
    نامش را به SKIP_HANDLERS (tenant/shared/glass_generic.py) اضافه کن، یا الگو را عوض کن."""
    missing = _risky_handlers_by_ast() - set(GG.SKIP_HANDLERS)
    assert not missing, sorted(missing)


def test_skip_list_has_no_stale_entries():
    stale = set(GG.SKIP_HANDLERS) - _risky_handlers_by_ast()
    assert not stale, sorted(stale)


def test_skip_list_names_match_real_runtime_names():
    """کلیدِ تابع در زمانِ اجرا (__module__.__qualname__) باید با نامِ استخراج‌شده از سورس یکی باشد."""
    import importlib
    for key in sorted(GG.SKIP_HANDLERS):
        mod, _, fn = key.rpartition(".")
        m = importlib.import_module(mod)
        obj = getattr(m, fn, None)
        assert obj is not None, key
        assert GG.handler_key(SimpleNamespace(callback=obj)) == key, key


def test_main_tenant_installs_the_middleware_before_including_the_router():
    src = (ROOT / "main_tenant.py").read_text(encoding="utf-8")
    assert "install_glass_generic(tenant_router)" in src
    assert src.index("install_glass_generic(tenant_router)") < src.index("tenant_dp.include_router(tenant_router)")
