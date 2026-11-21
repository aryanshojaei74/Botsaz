"""PATCH_PRICE_GROUP_STATUS_V1 — قیمت‌یاب در «تنظیمات گروه»؛ ربات گروه‌هایش را تشخیص می‌دهد و وضعیت را نشان می‌دهد."""
import asyncio
import json

import pytest

from tenant.shared import price_lookup as pl


class _Chat:
    def __init__(self, cid, title, typ="supergroup"):
        self.id, self.title, self.type = cid, title, typ


class _Me:
    def __init__(self, reads):
        self.id, self.username, self.can_read_all_group_messages = 99, "shopbot", reads


class _Member:
    def __init__(self, st):
        self.status = st


class _Bot:
    id = 99

    def __init__(self, reads=False, statuses=None):
        self.reads, self.statuses = reads, statuses or {}

    async def me(self):
        return _Me(self.reads)

    async def get_chat_member(self, cid, uid):
        st = self.statuses.get(cid)
        if st is None:
            raise RuntimeError("chat not found")
        return _Member(st)


@pytest.fixture
def store(monkeypatch):
    import tenant
    d = {}

    async def gs(bid, k, dflt=""):
        return d.get(k, dflt)

    async def ss(bid, k, v):
        d[k] = v

    class _Row:
        id = 1

    async def row(bot):
        return _Row()

    async def bought(bid):
        return True
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    monkeypatch.setattr(tenant, "get_bot_row", row)
    monkeypatch.setattr(pl, "purchased", bought)
    pl._SEEN_GROUPS.clear()
    return d


class _Msg:
    def __init__(self, chat):
        self.chat = chat


@pytest.mark.asyncio
async def test_remember_group_once_and_cap(store):
    bot = _Bot()
    pl.remember_group(_Msg(_Chat(-1001, "گروه ارز")), bot)
    pl.remember_group(_Msg(_Chat(-1001, "گروه ارز")), bot)        # بار دوم کاری نمی‌کند
    pl.remember_group(_Msg(_Chat(5, "pv", "private")), bot)        # پیوی ذخیره نمی‌شود
    await asyncio.sleep(0.05)
    assert json.loads(store[pl.GROUPS_KEY]) == {"-1001": "گروه ارز"}
    for i in range(12):
        await pl._save_group(1, -2000 - i, f"g{i}")
    assert len(json.loads(store[pl.GROUPS_KEY])) == pl.MAX_GROUPS


@pytest.mark.asyncio
async def test_status_admin_member_and_gone(store):
    store[pl.GROUPS_KEY] = json.dumps({"-1": "الف", "-2": "ب", "-3": "گروه‌قدیمی"}, ensure_ascii=False)
    bot = _Bot(reads=False, statuses={-1: "administrator", -2: "member"})
    text, uname = await pl.group_status(1, bot)
    assert uname == "shopbot" and "Group Privacy روشن" in text
    assert "الف — ✅ ادمین" in text and "ب — ⚠️ عضو ولی ادمین نیست" in text and "گروه‌قدیمی" not in text
    assert json.loads(store[pl.GROUPS_KEY]) == {"-1": "الف", "-2": "ب"}       # گروهی که ربات در آن نیست حذف شد
    text, _ = await pl.group_status(1, _Bot(reads=True, statuses={-1: "administrator", -2: "member"}))
    assert "ب — ✅ عضو؛ جواب می‌دهد" in text and "Group Privacy خاموش" in text


@pytest.mark.asyncio
async def test_menu_has_add_to_group_link(store):
    store[pl.ON_KEY] = "1"
    text, kb = await pl._menu(1, _Bot(reads=True))
    urls = [b.url for r in kb.inline_keyboard for b in r if b.url]
    assert "https://t.me/shopbot?startgroup=pq&admin=delete_messages" in urls
    assert "هنوز گروهی دیده نشده" in text


def test_group_settings_entry_and_hook():
    import inspect
    import tenant
    from tenant.shared import group_wallet
    assert 'callback_data="pq:menu"' in inspect.getsource(group_wallet._group_settings_content)
    assert "_pq_rg(msg, bot)" in inspect.getsource(tenant)
