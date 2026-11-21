"""PATCH_BTN_STICKER_PERSIST_V1 (Sajad #178/#179): استیکرِ دکمهٔ قبلی با رفتن به صفحهٔ بعد پاک می‌شود، حتی بعد از ری‌استارت."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant


class FakeBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, mid):
        self.deleted.append((chat_id, mid))


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", mk)
    tenant._SETTING_CACHE.clear()
    tenant._LAST_BTN_STICKER.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="vpn", plan="pro", status="active"))
        await s.commit()
    yield
    await eng.dispose()


@pytest.mark.asyncio
async def test_previous_sticker_is_deleted_even_after_restart(env):
    b = FakeBot()
    await tenant.set_setting(1, "btnst:500", "321")          # ثبت‌شده توسطِ پردازشِ قبلی؛ حافظه خالی است
    await tenant._drop_btn_sticker(b, 1, 500)
    assert b.deleted == [(500, 321)]
    await tenant._drop_btn_sticker(b, 1, 500)                # بار دوم چیزی نمی‌ماند
    assert b.deleted == [(500, 321)]


@pytest.mark.asyncio
async def test_next_button_without_sticker_also_removes_previous_sticker(env, monkeypatch):
    sent, ran = [], []

    class Msg:
        chat = NS(id=500)

        async def answer_sticker(self, fid):
            sent.append(fid)
            return NS(message_id=700 + len(sent))

    from tenant.shared import home_sticker_admin as HS
    stickers = {10: "STK_A", 11: ""}

    async def gbs(bot_id, btn_id):
        return stickers[btn_id]

    async def run(*a, **k):
        ran.append(a[0])
    monkeypatch.setattr(HS, "get_button_sticker", gbs)
    monkeypatch.setattr(tenant, "_run_button_action", run)
    monkeypatch.setattr(tenant, "has_feature", lambda plan, feat: True)
    b = FakeBot()
    row = NS(plan="pro")
    await tenant._exec_main_button(NS(id=10, action="x"), Msg(), b, None, row, None, 1)
    assert sent == ["STK_A"] and b.deleted == []
    await tenant._exec_main_button(NS(id=10, action="x"), Msg(), b, None, row, None, 1)
    assert b.deleted == [(500, 701)]                       # استیکرِ اول با ارسالِ استیکرِ دوم پاک شد
    await tenant._exec_main_button(NS(id=11, action="x"), Msg(), b, None, row, None, 1)
    assert b.deleted[-1] == (500, 702) and len(sent) == 2   # دکمهٔ بدونِ استیکر هم استیکرِ قبلی را برداشت


@pytest.mark.asyncio
async def test_start_sticker_is_removed_when_user_opens_a_button_and_survives_restart(env, monkeypatch):
    b = FakeBot()
    await tenant.set_setting(1, "startst:500", "55")           # فرستاده‌شده در /start؛ حافظه خالی (ری‌استارت)
    from tenant.shared import home_sticker_admin as HS

    async def gbs(bot_id, btn_id):
        return ""

    async def run(*a, **k):
        return None
    monkeypatch.setattr(HS, "get_button_sticker", gbs)
    monkeypatch.setattr(tenant, "_run_button_action", run)
    monkeypatch.setattr(tenant, "has_feature", lambda plan, feat: True)
    await tenant._exec_main_button(NS(id=11, action="x"), NS(chat=NS(id=500)), b, None, NS(plan="pro"), None, 1)
    assert (500, 55) in b.deleted
    assert not await tenant.get_setting(1, "startst:500", "")


@pytest.mark.asyncio
async def test_home_menu_does_not_delete_the_fresh_start_sticker(env):
    b = FakeBot()
    tenant._LAST_START_STICKER[(1, 500)] = 77
    await tenant._drop_btn_sticker(b, 1, 500)                  # همان کاری که send_home_menu می‌کند
    assert b.deleted == [] and tenant._LAST_START_STICKER[(1, 500)] == 77
