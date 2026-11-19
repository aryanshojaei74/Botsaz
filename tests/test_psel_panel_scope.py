"""Whale Support (۱۹ نوامبر): «اول باید پنل انتخاب بشه، بعد مدیریتِ همان پنل بیاد؛ اینجا اول مدیریت میاد» — انتخابِ پنل قبل از مدیریت."""
import types

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import admin as PA
from tenant.bots.panel_seller import manage as PM

BOT, OWNER = 5, 1


async def _mk_env(monkeypatch, n_pasargad=2):
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
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="b", field="panel_seller", plan="pro", status="active"))
        for i in range(1, n_pasargad + 1):
            s.add(models.VpnPanel(id=i, bot_id=BOT, panel_key="pasargad" if i == 1 else f"pasargad#{i}",
                                  base_url=f"https://panel{i}.example.com", username="a", password="b", enabled=True))
        # بسته‌ها و ادمین‌هایِ فروخته‌شده روی هر پنل
        s.add(models.PanelSellerPackage(id=11, bot_id=BOT, panel_id=1, title="بستهٔ P1", price_toman=1000, active=True))
        s.add(models.PanelSellerPackage(id=12, bot_id=BOT, panel_id=1, title="بستهٔ P1 دوم", price_toman=2000, active=True))
        s.add(models.PanelSellerPackage(id=21, bot_id=BOT, panel_id=2, title="بستهٔ P2", price_toman=3000, active=True))
        s.add(models.PanelSellerPackage(id=90, bot_id=BOT, panel_id=0, title="دستی", price_toman=500, active=True, manual=True))
        s.add(models.PanelSellerAccount(id=1, bot_id=BOT, buyer_tg=7, panel_id=1, title="a1", admin_username="adm_p1", status="active"))
        s.add(models.PanelSellerAccount(id=2, bot_id=BOT, buyer_tg=8, panel_id=2, title="a2", admin_username="adm_p2", status="active"))
        s.add(models.PanelSellerAccount(id=3, bot_id=BOT, buyer_tg=9, panel_id=2, title="a3", admin_username="adm_p2b", status="suspended"))
        await s.commit()
    return engine, mk


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine, mk = await _mk_env(monkeypatch, 2)
    sent = []

    async def edit(event, text, markup=None, **k): sent.append((text, markup))
    monkeypatch.setattr(PA, "edit_or_send", edit)
    monkeypatch.setattr(PM, "edit_or_send", edit)
    row = types.SimpleNamespace(id=BOT, owner_id=OWNER, plan="pro", field="panel_seller", username="b")

    async def guard(ev, bot): return row
    monkeypatch.setattr(PA, "_guard", guard)
    monkeypatch.setattr(PM, "_guard", guard)

    async def status_line(r): return "STATUS"
    monkeypatch.setattr(PA, "status_line", status_line)
    yield types.SimpleNamespace(mk=mk, sent=sent, row=row)
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


class Cb:
    def __init__(self, data, uid=OWNER):
        self.data, self.answers = data, []
        self.from_user = types.SimpleNamespace(id=uid)
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=1), message_id=1)

    async def answer(self, text="", show_alert=False, **k): self.answers.append((text, show_alert))

    def model_copy(self, update): 
        c = Cb(update.get("data", self.data), self.from_user.id)
        return c


def st():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=5, chat_id=1, user_id=1))


def cbs(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r]


def texts(markup):
    return [b.text for r in markup.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_with_two_panels_the_first_screen_is_the_panel_chooser_not_management(env):
    await PA.adm_menu_fresh(Cb("psel:adm:new"), types.SimpleNamespace(), st())
    text, mk = env.sent[-1]
    assert "اول پنلی را که می‌خواهی مدیریت کنی انتخاب کن" in text
    c = cbs(mk)
    assert "psel:sc:1" in c and "psel:sc:2" in c and "psel:sc:0" in c                  # هر پنل + «همهٔ پنل‌ها»
    assert not any(x in c for x in ("psel:pk", "psel:ac:0", "psel:mg", "psel:rs"))        # هنوز بخشِ مدیریت/قیمت نیست
    t = texts(mk)
    assert any("panel1.example.com" in x for x in t) and any("panel2.example.com" in x for x in t)      # پنل‌ها قابلِ تشخیص‌اند
    assert any("📦2" in x or "📦۲" in x for x in t)                                          # شمارِ بسته‌هایِ هر پنل


@pytest.mark.asyncio
async def test_picking_a_panel_shows_that_panels_management_with_its_own_counts(env):
    await PA.adm_menu_fresh(Cb("psel:adm:new"), types.SimpleNamespace(), st())
    await PA.scope_pick(Cb("psel:sc:2"), types.SimpleNamespace(), st())
    text, mk = env.sent[-1]
    assert "پنلِ انتخاب‌شده" in text and "panel2.example.com" in text
    assert "بسته‌هایِ فعالِ این پنل: <b>۱</b>" in text.replace("1", "۱") or "بسته‌هایِ فعالِ این پنل: <b>1</b>" in text
    assert "🟢 ۱ · 🔴 ۱" in text or "🟢 1 · 🔴 1" in text                                   # P2: یک فعال و یک تعلیق
    c = cbs(mk)
    assert {"psel:pk", "psel:ac:0", "psel:mg", "psel:rs", "psel:sc:x"} <= set(c)
    await PA.scope_pick(Cb("psel:sc:1"), types.SimpleNamespace(), st())
    text, _ = env.sent[-1]
    assert "panel1.example.com" in text and "بسته‌هایِ فعالِ این پنل: <b>۲</b>" in text.replace("2", "۲") or "<b>2</b>" in text


@pytest.mark.asyncio
async def test_back_from_sub_screens_returns_to_the_same_panel_not_the_chooser(env):
    await PA.scope_pick(Cb("psel:sc:1"), types.SimpleNamespace(), st())
    await PA.adm_menu(Cb("psel:adm"), types.SimpleNamespace(), st())                      # «بازگشت» از یک زیرصفحه
    text, _ = env.sent[-1]
    assert "پنلِ انتخاب‌شده" in text and "panel1.example.com" in text


@pytest.mark.asyncio
async def test_change_panel_returns_to_the_chooser_and_a_fresh_entry_always_asks_first(env):
    await PA.scope_pick(Cb("psel:sc:1"), types.SimpleNamespace(), st())
    await PA.scope_pick(Cb("psel:sc:x"), types.SimpleNamespace(), st())
    assert "اول پنلی را که می‌خواهی مدیریت کنی" in env.sent[-1][0]
    await PA.scope_pick(Cb("psel:sc:2"), types.SimpleNamespace(), st())
    await PA.adm_menu_fresh(Cb("psel:adm:new"), types.SimpleNamespace(), st())             # از «سرویس‌ها و محصولات» دوباره
    assert "اول پنلی را که می‌خواهی مدیریت کنی" in env.sent[-1][0]


@pytest.mark.asyncio
async def test_package_list_and_new_package_are_scoped_to_the_chosen_panel(env):
    await PA.scope_pick(Cb("psel:sc:1"), types.SimpleNamespace(), st())
    await PA.pk_list(Cb("psel:pk"), types.SimpleNamespace(), st())
    c = cbs(env.sent[-1][1])
    assert "psel:pk:11" in c and "psel:pk:12" in c and "psel:pk:21" not in c and "psel:pk:90" not in c
    # «بستهٔ جدید»: سؤالِ «کدام پنل؟» نمی‌آید؛ مستقیم نام؛ پنل = پنلِ انتخاب‌شده
    s = st()
    await PA.pk_new(Cb("psel:pk:new"), types.SimpleNamespace(), s)
    assert "نامِ بسته" in env.sent[-1][0] and (await s.get_data())["psel_panel"] == 1
    await PA.scope_pick(Cb("psel:sc:2"), types.SimpleNamespace(), st())
    await PA.pk_list(Cb("psel:pk"), types.SimpleNamespace(), st())
    c2 = cbs(env.sent[-1][1])
    assert "psel:pk:21" in c2 and "psel:pk:11" not in c2


@pytest.mark.asyncio
async def test_all_panels_view_is_the_old_overview_with_manual_packages_and_the_panel_question(env):
    await PA.scope_pick(Cb("psel:sc:0"), types.SimpleNamespace(), st())
    text, mk = env.sent[-1]
    assert "پنل‌هایِ متصل" in text and "پنلِ انتخاب‌شده" not in text
    await PA.pk_list(Cb("psel:pk"), types.SimpleNamespace(), st())
    c = cbs(env.sent[-1][1])
    assert {"psel:pk:11", "psel:pk:12", "psel:pk:21", "psel:pk:90"} <= set(c)
    s = st()
    await PA.pk_new(Cb("psel:pk:new"), types.SimpleNamespace(), s)
    assert {"psel:pkp:1", "psel:pkp:2", "psel:pkm"} <= set(cbs(env.sent[-1][1]))          # نمای کلی: مثلِ قبل می‌پرسد


@pytest.mark.asyncio
async def test_sold_admins_list_is_scoped(env):
    await PA.scope_pick(Cb("psel:sc:2"), types.SimpleNamespace(), st())
    await PA.ac_list(Cb("psel:ac:0"), types.SimpleNamespace())
    c = cbs(env.sent[-1][1])
    assert "psel:ac:v:2" in c and "psel:ac:v:3" in c and "psel:ac:v:1" not in c
    await PA.scope_pick(Cb("psel:sc:1"), types.SimpleNamespace(), st())
    await PA.ac_list(Cb("psel:ac:0"), types.SimpleNamespace())
    c = cbs(env.sent[-1][1])
    assert "psel:ac:v:1" in c and "psel:ac:v:2" not in c


@pytest.mark.asyncio
async def test_manage_restart_and_role_skip_their_own_panel_pickers_when_a_panel_is_chosen(env, monkeypatch):
    got = {}

    async def dash(cb, bot, row, pid): got["mg"] = pid
    monkeypatch.setattr(PM, "_dashboard", dash)

    async def rs_do(cb, bot): got["rs"] = cb.data
    monkeypatch.setattr(PA, "rs_do", rs_do)

    async def role_list(cb, bot): got["role"] = cb.data
    monkeypatch.setattr(PA, "role_list", role_list)
    await PA.scope_pick(Cb("psel:sc:2"), types.SimpleNamespace(), st())
    await PM.mg_home(Cb("psel:mg"), types.SimpleNamespace(), st())
    await PA.rs_list(Cb("psel:rs"), types.SimpleNamespace())
    await PA.role_panels(Cb("psel:role"), types.SimpleNamespace())
    assert got == {"mg": 2, "rs": "psel:rs:2", "role": "psel:roleP:2"}
    got.clear()
    await PA.scope_pick(Cb("psel:sc:0"), types.SimpleNamespace(), st())                     # همهٔ پنل‌ها ⇒ همان انتخابگرهایِ قبلی
    await PM.mg_home(Cb("psel:mg"), types.SimpleNamespace(), st())
    await PA.rs_list(Cb("psel:rs"), types.SimpleNamespace())
    assert got == {} and "psel:mgP:1" in cbs(env.sent[-2][1]) and "psel:rs:2" in cbs(env.sent[-1][1])


@pytest.mark.asyncio
async def test_each_admin_has_their_own_scope_and_invalid_scope_asks_again(env):
    await PA.scope_pick(Cb("psel:sc:1", uid=OWNER), types.SimpleNamespace(), st())
    assert await PA.scope_get(BOT, OWNER) == 1 and await PA.scope_get(BOT, 77) is None
    await tenant.set_setting(BOT, f"psel_scope_{OWNER}", "999")                              # پنلی که دیگر نیست
    assert await PA.scope_get(BOT, OWNER) is None
    bad = Cb("psel:sc:999")
    await PA.scope_pick(bad, types.SimpleNamespace(), st())
    assert bad.answers and bad.answers[0][1]


@pytest.mark.asyncio
async def test_a_single_panel_keeps_the_old_flow_with_no_chooser(monkeypatch):
    engine, mk = await _mk_env(monkeypatch, 1)
    sent = []

    async def edit(event, text, markup=None, **k): sent.append((text, markup))
    monkeypatch.setattr(PA, "edit_or_send", edit)
    row = types.SimpleNamespace(id=BOT, owner_id=OWNER, plan="pro", field="panel_seller", username="b")

    async def guard(ev, bot): return row
    monkeypatch.setattr(PA, "_guard", guard)

    async def status_line(r): return "S"
    monkeypatch.setattr(PA, "status_line", status_line)
    await PA.adm_menu_fresh(Cb("psel:adm:new"), types.SimpleNamespace(), st())
    text, mk = sent[-1]
    assert "اول پنلی" not in text and "psel:sc:x" not in cbs(mk) and {"psel:pk", "psel:ac:0", "psel:mg"} <= set(cbs(mk))
    assert await PA.scope_get(BOT, OWNER) == 0
    await PA.pk_new(Cb("psel:pk:new"), types.SimpleNamespace(), st())
    assert "psel:pkp:1" in cbs(sent[-1][1])                                                  # مثلِ قبل
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


def test_products_menu_entry_is_a_fresh_entry():
    from pathlib import Path
    src = Path("tenant/bots/panel_seller/products_menu.py").read_text(encoding="utf-8")
    assert '"psel:adm:new"' in src and "fresh=True" in src
