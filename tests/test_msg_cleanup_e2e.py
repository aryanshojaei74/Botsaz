"""ایلیا: «پاک‌سازی عملیاتی انجام نمی‌دهد» — تستِ سرتاسریِ زنجیرهٔ واقعیِ aiogram (Dispatcher + Router + Bot + میان‌افزارِ نشست)."""
import datetime as _dt
import types

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User

import tenant
from tenant.shared import msg_cleanup as MC

pytestmark = pytest.mark.asyncio


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent, self.deleted, self._next = [], [], 100

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            self._next += 1
            self.sent.append((method.chat_id, method.text, self._next))
            return Message(message_id=self._next, date=_dt.datetime.utcnow(),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text)
        if isinstance(method, DeleteMessage):
            self.deleted.append((method.chat_id, method.message_id))
            return True
        return True


def _msg_update(uid, text, upd_id):
    m = Message(message_id=upd_id, date=_dt.datetime.utcnow(), chat=Chat(id=uid, type="private"),
                from_user=User(id=uid, is_bot=False, first_name="u"), text=text)
    return Update(update_id=upd_id, message=m)


def _cb_update(uid, data, upd_id, on_mid):
    m = Message(message_id=on_mid, date=_dt.datetime.utcnow(), chat=Chat(id=uid, type="private"),
                from_user=User(id=999, is_bot=True, first_name="b"), text="old")
    cq = CallbackQuery(id=str(upd_id), from_user=User(id=uid, is_bot=False, first_name="u"),
                       chat_instance="x", message=m, data=data)
    return Update(update_id=upd_id, callback_query=cq)


@pytest.fixture
def stack(monkeypatch):
    MC._TRACK.clear()
    store = {"msg_cleanup": "1"}
    row = types.SimpleNamespace(id=5, plan="pro", owner_id=77)

    async def get_bot_row(bot): return row

    async def gs(bid, key, default=""): return store.get(key, default)

    async def adm(bid, uid, owner): return uid in (77,)
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "is_owner_or_admin", adm)

    root, inner = Router(name="root"), Router(name="inner")       # مثلِ tenant_router + زیرروترها
    ik = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x")]])

    @inner.message(F.text == "menu")
    async def _menu(m: Message):
        await m.answer("🏠 منوی اصلی", reply_markup=ik)

    @inner.message(F.text == "wallet")
    async def _wallet(m: Message):
        await m.answer("👛 کیف پول", reply_markup=ik)

    @inner.message(F.text == "config")
    async def _config(m: Message):
        await m.answer("کانفیگ: vless://abc#x")

    @inner.callback_query(F.data == "back")
    async def _back(cb: CallbackQuery):
        await cb.message.answer("🏠 منوی اصلی", reply_markup=ik)

    root.include_router(inner)                                      # هندلرها در زیرروتر
    MC.install_on(root)                                             # میان‌افزار روی روترِ والد
    dp = Dispatcher()
    dp.include_router(root)
    sess = FakeSession()
    bot = Bot(token="1:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", session=sess)
    return dp, bot, sess, store


async def test_back_from_wallet_deletes_wallet_message_for_a_customer(stack):
    dp, bot, sess, _ = stack
    await dp.feed_update(bot, _msg_update(500, "menu", 1))
    await dp.feed_update(bot, _msg_update(500, "wallet", 2))
    assert sess.deleted == [(500, 101)]                             # منویِ اصلیِ قبلی پاک شد
    sess.deleted.clear()
    await dp.feed_update(bot, _cb_update(500, "back", 3, on_mid=102))
    assert sess.deleted == [(500, 102)]                             # کیف پول پاک؛ فقط «منوی اصلی» می‌ماند


async def test_config_messages_are_never_deleted(stack):
    dp, bot, sess, _ = stack
    await dp.feed_update(bot, _msg_update(500, "config", 1))
    await dp.feed_update(bot, _msg_update(500, "menu", 2))
    await dp.feed_update(bot, _msg_update(500, "wallet", 3))
    assert (500, 101) not in sess.deleted                           # پیامِ کانفیگ هرگز پاک نمی‌شود


async def test_owner_chat_is_untouched_by_default_and_cleaned_when_staff_toggle_is_on(stack):
    dp, bot, sess, store = stack
    await dp.feed_update(bot, _msg_update(77, "menu", 1))
    await dp.feed_update(bot, _msg_update(77, "wallet", 2))
    assert sess.deleted == []                                       # علتِ «کاری انجام نمی‌دهد»: مالک مستثنی بود
    store["msg_cleanup_staff"] = "1"
    MC._TRACK.clear()
    await dp.feed_update(bot, _msg_update(77, "menu", 3))
    await dp.feed_update(bot, _msg_update(77, "wallet", 4))
    assert len(sess.deleted) == 1


async def test_feature_off_does_nothing(stack):
    dp, bot, sess, store = stack
    store["msg_cleanup"] = "0"
    await dp.feed_update(bot, _msg_update(500, "menu", 1))
    await dp.feed_update(bot, _msg_update(500, "wallet", 2))
    assert sess.deleted == []


async def test_start_command_message_deleted_only_when_toggle_on(stack):
    dp, bot, sess, store = stack

    @dp.sub_routers[0].sub_routers[0].message(F.text == "/start")
    async def _st(m: Message):
        await m.answer("🏠 منوی اصلی")
    await dp.feed_update(bot, _msg_update(500, "/start", 11))
    assert (500, 11) not in sess.deleted                            # پیش‌فرض: پیامِ /start می‌ماند
    store["msg_cleanup_start"] = "1"
    MC._TRACK.clear()
    await dp.feed_update(bot, _msg_update(500, "/start", 12))
    assert (500, 12) in sess.deleted                                # روشن: پیامِ /start مشتری پاک شد
    await dp.feed_update(bot, _msg_update(500, "/start", 13))
    assert (500, 102) in sess.deleted or any(d[0] == 500 and d[1] > 100 for d in sess.deleted)   # منویِ قبلیِ ربات هم پاک شد
