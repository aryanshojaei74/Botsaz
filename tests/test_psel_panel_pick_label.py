"""Whale Support (۱۹ نوامبر): «می‌خوام دو تا پاسارگاد بفروشم ولی نمی‌تونم» — دکمهٔ انتخابِ پنل فقط نوع را نشان می‌داد؛ دو پنلِ هم‌نوع یکسان بودند."""
import types

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import admin as PA

BOT = 5


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
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
        s.add(models.Bot(id=BOT, owner_id=1, token="5:T", username="b", field="panel_seller", plan="pro", status="active"))
        s.add(models.VpnPanel(id=1, bot_id=BOT, panel_key="pasargad", base_url="https://panel1.example.com", username="a", password="b", enabled=True))
        s.add(models.VpnPanel(id=2, bot_id=BOT, panel_key="pasargad#2", base_url="https://www.panel2.example.org:8443", username="a", password="b", enabled=True))
        s.add(models.VpnPanel(id=3, bot_id=BOT, panel_key="marzban", base_url="https://mz.example.com", username="a", password="b", enabled=True))
        await s.commit()
    yield mk
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


async def _panel(env, pid):
    async with env() as s:
        return await s.get(models.VpnPanel, pid)


@pytest.mark.asyncio
async def test_two_panels_of_the_same_type_get_distinct_labels(env):
    a, b = await PA.panel_choice_label(BOT, await _panel(env, 1)), await PA.panel_choice_label(BOT, await _panel(env, 2))
    assert a != b and "panel1.example.com" in a and "panel2.example.org:8443" in b and "www." not in b
    assert "پاسارگاد" in a and "پاسارگاد" in b                                   # نوع هم دیده می‌شود (نه «pasargad#2»ِ خام)
    assert "pasargad#2" not in b


@pytest.mark.asyncio
async def test_owner_custom_name_wins_and_host_still_disambiguates(env):
    from tenant.bots.vpn.vpn_admin import panel_label_key
    await tenant.set_setting(BOT, panel_label_key("pasargad#2"), "پاسارگاد VIP")
    b = await PA.panel_choice_label(BOT, await _panel(env, 2))
    assert b.startswith("پاسارگاد VIP") and "panel2.example.org" in b
    assert len(b) <= 60


@pytest.mark.asyncio
async def test_package_panel_picker_lists_every_panel_with_its_own_callback(env, monkeypatch):
    row = types.SimpleNamespace(id=BOT, owner_id=1, plan="pro")

    async def guard(cb, bot): return row
    monkeypatch.setattr(PA, "_guard", guard)
    got = {}

    async def edit(cb, text, kb): got["kb"] = kb

    async def noop(*a, **k): return None
    monkeypatch.setattr(PA, "edit_or_send", edit)

    class St:
        async def clear(self): pass

    class Cb:
        data = "psel:pk:new"
        message = types.SimpleNamespace(chat=types.SimpleNamespace(id=1), message_id=1)
        from_user = types.SimpleNamespace(id=1)
        answer = staticmethod(noop)
    await PA.pk_new(Cb(), types.SimpleNamespace(), St())
    btns = [b for r in got["kb"].inline_keyboard for b in r]
    pick = {b.callback_data: b.text for b in btns if (b.callback_data or "").startswith("psel:pkp:")}
    assert set(pick) == {"psel:pkp:1", "psel:pkp:2", "psel:pkp:3"}
    assert len(set(pick.values())) == 3                                          # هر پنل متنِ جدا دارد
    assert "psel:pkm" in [b.callback_data for b in btns]                         # فروشِ دستی هنوز هست


def test_default_panel_label_never_shows_raw_instance_keys():
    """PATCH_PANEL_LABEL_INSTANCE_V1: «pasargad#2» ⇒ «پاسارگارد #2»؛ نوعِ ناشناخته دست‌نخورده."""
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS, default_panel_label
    assert default_panel_label("pasargad") == PANEL_LABELS["pasargad"]
    assert default_panel_label("pasargad#2") == f"{PANEL_LABELS['pasargad']} #2"
    assert default_panel_label("weird#3") == "weird#3" and default_panel_label("nope") == "nope"


@pytest.mark.asyncio
async def test_panelyar_list_shows_name_and_address_for_each_panel(env):
    """وهیل (WHALESHOP1BOT با ۶ پاسارگارد): لیستِ «پنل‌یار» هم هر پنل را با نام + آدرس نشان می‌دهد."""
    from tenant.shared.panel_clients.panelyar import _panels_menu_content
    txt, kb = await _panels_menu_content(BOT)
    labels = [b.text for r in kb.inline_keyboard for b in r if (b.callback_data or "").startswith("pyar:panel:")]
    assert len(labels) == 3 and len(set(labels)) == 3
    assert any("panel1.example.com" in x for x in labels) and any("panel2.example.org:8443" in x for x in labels)
    assert "pasargad#2" not in txt and all("pasargad#2" not in x for x in labels)
