"""PATCH_START_CLEAN_PREV_V1 (YOUxsell): /start جدید ⇒ پیامِ خوش‌آمدِ قبلی پاک شود (اختیاری)."""
from types import SimpleNamespace as NS

import pytest

import tenant


@pytest.fixture
def store(monkeypatch):
    st = {}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)

    async def ss(bot_id, key, val):
        st[key] = val
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    return st


class _Bot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat, mid):
        self.deleted.append((chat, mid))


def _msg(uid=7, bot=None):
    return NS(from_user=NS(id=uid), chat=NS(id=uid), bot=bot or _Bot())


@pytest.mark.asyncio
async def test_off_by_default_nothing_is_stored_or_deleted(store):
    m = _msg()
    await tenant._start_remember(m, 1, NS(message_id=50))
    await tenant._start_cleanup_prev(m, 1)
    assert store == {} and m.bot.deleted == []


@pytest.mark.asyncio
async def test_on_deletes_previous_welcome_once_and_remembers_the_new_one(store):
    store["start_clean_prev"] = "1"
    m1 = _msg()
    await tenant._start_cleanup_prev(m1, 1)                      # اولین /start: چیزی برای پاک‌کردن نیست
    assert m1.bot.deleted == []
    await tenant._start_remember(m1, 1, NS(message_id=50))
    assert store["last_welcome_msg:7"] == "50"
    m2 = _msg()
    await tenant._start_cleanup_prev(m2, 1)                      # /startِ دوم: پیامِ ۵۰ پاک می‌شود
    assert m2.bot.deleted == [(7, 50)] and store["last_welcome_msg:7"] == ""
    await tenant._start_remember(m2, 1, NS(message_id=61))
    m3 = _msg()
    await tenant._start_cleanup_prev(m3, 1)
    assert m3.bot.deleted == [(7, 61)]


@pytest.mark.asyncio
async def test_each_user_has_their_own_previous_message_and_delete_errors_are_harmless(store):
    store["start_clean_prev"] = "1"
    await tenant._start_remember(_msg(uid=1), 1, NS(message_id=10))
    await tenant._start_remember(_msg(uid=2), 1, NS(message_id=20))

    class Boom(_Bot):
        async def delete_message(self, chat, mid):
            raise RuntimeError("message to delete not found")
    m = _msg(uid=2, bot=Boom())
    await tenant._start_cleanup_prev(m, 1)                       # خطا نباید /start را بشکند
    assert store["last_welcome_msg:2"] == "" and store["last_welcome_msg:1"] == "10"
    await tenant._start_remember(_msg(uid=3), 1, None)           # پیامِ ناموفق ⇒ چیزی ذخیره نمی‌شود
    assert "last_welcome_msg:3" not in store


@pytest.mark.asyncio
async def test_toggle_handler_flips_the_setting(store, monkeypatch):
    async def owner(cb, bot):
        return NS(id=1)

    async def page(cb, bot, state):
        return None
    alerts = []

    class CB:
        data = "set:homemenutext:clean"

        async def answer(self, text="", **k):
            alerts.append(text)
    monkeypatch.setattr(tenant, "_owner_ok", owner)
    monkeypatch.setattr(tenant, "set_homemenutext_start", page)
    await tenant.set_start_clean_toggle(CB(), NS(), NS())
    assert store["start_clean_prev"] == "1" and "روشن" in alerts[-1]
    await tenant.set_start_clean_toggle(CB(), NS(), NS())
    assert store["start_clean_prev"] == "0" and "خاموش" in alerts[-1]
