"""ایلیا: پاک‌سازیِ خودکارِ پیام‌هایِ قبلیِ همان روز (پیش‌فرض خاموش، پرو)؛ پیام‌هایِ خودِ ربات/رسانه/لینک هرگز پاک نشوند."""
import datetime as _dt
import types

import pytest
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage, SendPhoto
from aiogram.types import (Chat, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

import tenant
from plans import Feature, has_feature
from tenant.shared import msg_cleanup as MC

pytestmark = pytest.mark.asyncio


def _msg(mid, chat=1):
    return Message(message_id=mid, date=_dt.datetime.utcnow(), chat=Chat(id=chat, type="private"))


class _Bot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, mid):
        self.deleted.append((chat_id, mid))


def _mk_next(mid):
    async def make_request(bot, method):
        return _msg(mid)
    return make_request


@pytest.fixture(autouse=True)
def _clean():
    MC._TRACK.clear()
    yield
    MC._TRACK.clear()


async def _step(bot, bot_id, chat, sends):
    """یک آپدیتِ مشتری: sends = [(method, message_id_returned)]."""
    ctx = MC.begin(bot_id, chat)
    tok = MC._CTX.set(ctx)
    try:
        for method, mid in sends:
            await MC.session_mw(_mk_next(mid), bot, method)
    finally:
        MC._CTX.reset(tok)
    return await MC.finish(bot, ctx)


async def test_previous_menu_deleted_and_only_new_page_stays():
    bot = _Bot()
    ik = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x")]])
    n = await _step(bot, 5, 1, [(SendMessage(chat_id=1, text="🏠 منوی اصلی", reply_markup=ik), 10)])
    assert n == 0
    n = await _step(bot, 5, 1, [(SendMessage(chat_id=1, text="👛 کیف پول", reply_markup=ik), 11)])
    assert bot.deleted == [(1, 10)] and n == 1               # منویِ اصلیِ قبلی پاک؛ کیف پول مانده
    bot.deleted.clear()
    await _step(bot, 5, 1, [(SendMessage(chat_id=1, text="🏠 منوی اصلی", reply_markup=ik), 12)])
    assert bot.deleted == [(1, 11)]                          # «بازگشت»: کیف پول پاک، فقط منوی اصلی


async def test_edited_page_is_kept_and_others_removed():
    bot = _Bot()
    await _step(bot, 5, 1, [(SendMessage(chat_id=1, text="a"), 10), (SendMessage(chat_id=1, text="b"), 11)])
    await _step(bot, 5, 1, [(EditMessageText(chat_id=1, message_id=11, text="b2"), 11)])
    assert bot.deleted == [(1, 10)]                          # ۱۱ ویرایش شد ⇒ می‌ماند؛ ۱۰ پاک


async def test_protected_messages_are_never_tracked_or_deleted():
    bot = _Bot()
    rk = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="m")]])
    await _step(bot, 5, 1, [
        (SendMessage(chat_id=1, text="کانفیگ: vless://abc"), 20),          # لینک/کانفیگ
        (SendMessage(chat_id=1, text="منو", reply_markup=rk), 21),          # کیبوردِ پایین
        (SendPhoto(chat_id=1, photo="x"), 22),                              # رسانه
        (SendMessage(chat_id=1, text="ok"), 23)])
    await _step(bot, 5, 1, [(SendMessage(chat_id=1, text="next"), 24)])
    assert bot.deleted == [(1, 23)]                                         # فقط پیامِ معمولی


async def test_messages_outside_user_update_or_other_chat_are_not_tracked():
    bot = _Bot()
    # بدونِ context (پیامِ پس‌زمینه: پیامِ اختصاصی/امتیازدهی/اخطار) هیچ ثبتی نمی‌شود
    await MC.session_mw(_mk_next(30), bot, SendMessage(chat_id=1, text="پیامِ اختصاصیِ ربات"))
    assert MC._TRACK == {}
    # پیام به چتِ دیگر (مثلاً اعلان به ادمین) داخلِ همان آپدیت ثبت نمی‌شود
    await _step(bot, 5, 1, [(SendMessage(chat_id=999, text="اعلانِ ادمین"), 31), (SendMessage(chat_id=1, text="x"), 32)])
    assert MC._TRACK[(5, 1)]["ids"] == [32]


async def test_silent_update_does_not_wipe_the_screen_and_old_day_is_ignored():
    bot = _Bot()
    await _step(bot, 5, 1, [(SendMessage(chat_id=1, text="page"), 40)])
    assert await _step(bot, 5, 1, []) == 0 and bot.deleted == []            # آپدیتِ بی‌پیام (فقط answer) چیزی پاک نمی‌کند
    MC._TRACK[(5, 1)]["day"] = "2000-01-01"                                  # روزِ قبل ⇒ دست نمی‌خورد
    await _step(bot, 5, 1, [(SendMessage(chat_id=1, text="new"), 41)])
    assert bot.deleted == []


async def test_gate_is_pro_only_default_off_customer_private_only(monkeypatch):
    assert has_feature("pro", Feature.MSG_CLEANUP) and not has_feature("growth", Feature.MSG_CLEANUP)
    row = types.SimpleNamespace(id=5, plan="pro", owner_id=77)
    store = {}

    async def get_bot_row(bot): return row

    async def gs(bid, key, default=""): return store.get(key, default)

    async def adm(bid, uid, owner): return uid in (77, 78)
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "is_owner_or_admin", adm)
    priv = types.SimpleNamespace(chat=types.SimpleNamespace(type="private", id=1), from_user=types.SimpleNamespace(id=500))
    assert await MC.enabled_for(object(), priv) is None                      # پیش‌فرض خاموش
    store["msg_cleanup"] = "1"
    assert await MC.enabled_for(object(), priv) == 5
    owner = types.SimpleNamespace(chat=priv.chat, from_user=types.SimpleNamespace(id=77))
    assert await MC.enabled_for(object(), owner) is None                     # مالک/ادمین پیش‌فرض نه
    store["msg_cleanup_staff"] = "1"                                         # با گزینهٔ «چتِ مالک/ادمین هم» (تستِ خودِ مالک)
    assert await MC.enabled_for(object(), owner) == 5
    store["msg_cleanup_staff"] = "0"
    group = types.SimpleNamespace(chat=types.SimpleNamespace(type="supergroup", id=-1), from_user=priv.from_user)
    assert await MC.enabled_for(object(), group) is None                     # گروه نه
    row.plan = "growth"
    assert await MC.enabled_for(object(), priv) is None                      # پلنِ پایین‌تر نه


def test_middleware_is_installed_on_the_parent_router():
    from pathlib import Path
    src = Path("tenant/__init__.py").read_text(encoding="utf-8")
    assert "_mc_install_on(tenant_router)" in src and '"mcl:menu"' in src


async def test_menu_shows_staff_toggle_and_explains_owner_testing(monkeypatch):
    async def gs(bid, key, default=""):
        return {"msg_cleanup": "1"}.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    text, kb = await MC._menu(5, "pro")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "mcl:staff" in cbs and "مالک" in text and "تست" in text and "از لحظهٔ روشن‌شدن" in text
