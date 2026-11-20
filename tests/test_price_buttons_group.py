"""PATCH_PRICE_BUTTONS_EDITOR_V1 + PATCH_PRICE_LOOKUP_PLATFORM_GROUP_V1 (سینا/مدیر): ویرایشگر دکمه، ریال، راهنما، گروه بات‌ساز."""
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from tenant.shared import price_buttons as pb
from tenant.shared import price_lookup as pl


def test_parse_dump_color_roundtrip():
    raw = "a | https://a | green && b | https://b\nc | https://c | قرمز"
    rows = pb.parse(raw)
    assert rows == [[["a", "https://a", "success"], ["b", "https://b", ""]], [["c", "https://c", "danger"]]]
    assert pb.parse(pb.dump(rows)) == rows
    kb = pb.keyboard(rows)
    assert kb.inline_keyboard[0][0].style == "success" and kb.inline_keyboard[0][1].style is None


def test_apply_actions():
    rows = pb.parse("a | https://a\nb | https://b\nc | https://c")
    assert [r[0][0] for r in pb.apply(rows, "up", 2)] == ["a", "c", "b"]
    assert [r[0][0] for r in pb.apply(rows, "down", 0)] == ["b", "a", "c"]
    assert pb.apply(rows, "join", 1) == [[["a", "https://a", ""], ["b", "https://b", ""]], [["c", "https://c", ""]]]
    assert pb.apply(rows, "color", 0)[0][0][2] == "primary"
    assert len(pb.flat(pb.apply(rows, "del", 1))) == 2
    full = pb.parse("\n".join(f"x{i} | https://x/{i}" for i in range(8)))
    _r, ok = pb.add(full, "y", "https://y")                      # ۸ ردیف پر ⇒ ردیف جدید ممکن نیست
    assert ok is False
    r2, ok = pb.add(full, "y", "https://y", same_row=True)       # ولی کنار قبلی جا دارد
    assert ok and r2[-1][-1][0] == "y"


class _Store(pb.Store):
    def __init__(self, raw="", main=None):
        self.raw, self.main = raw, main or []

    async def get(self):
        return self.raw

    async def set(self, raw):
        self.raw = raw

    async def main_buttons(self):
        return self.main


class _M:
    def __init__(self, text=""):
        self.text, self.out = text, []

    async def answer(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))

    async def edit_text(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))


class _U:
    id = 5


class _Cb:
    def __init__(self, data, msg):
        self.data, self.message, self.from_user, self.alerts = data, msg, _U(), []

    async def answer(self, text=None, show_alert=False, **kw):
        self.alerts.append(text)


def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=5, user_id=5))


@pytest.mark.asyncio
async def test_add_link_button_flow_with_color_and_row():
    st, store, m = _state(), _Store("a | https://a"), _M()
    await pb.on_cb(_Cb("pqb:addl:0", m), st, store, "pqb", "pq:menu", True)
    assert await st.get_state() == pb.PbFSM.label.state
    await pb.on_msg(_M("کانال ما"), st, store, "pqb", "pq:menu", True)
    await pb.on_msg(_M("t.me/mychan"), st, store, "pqb", "pq:menu", True)       # بدون https هم قبول
    await pb.on_cb(_Cb("pqb:pick:2", m), st, store, "pqb", "pq:menu", True)      # سبز
    await pb.on_cb(_Cb("pqb:row:1", m), st, store, "pqb", "pq:menu", True)       # کنار قبلی
    assert pb.parse(store.raw) == [[["a", "https://a", ""], ["کانال ما", "https://t.me/mychan", "success"]]]


@pytest.mark.asyncio
async def test_add_main_menu_button_and_move():
    st, m = _state(), _M()
    store = _Store("", [("🛍 فروشگاه", "https://t.me/shopbot?start=mbtn_7")])
    await pb.on_cb(_Cb("pqb:addm:0", m), st, store, "pqb", "pq:menu", True)
    await pb.on_cb(_Cb("pqb:m:0", m), st, store, "pqb", "pq:menu", True)
    await pb.on_cb(_Cb("pqb:pick:1", m), st, store, "pqb", "pq:menu", True)
    await pb.on_cb(_Cb("pqb:row:0", m), st, store, "pqb", "pq:menu", True)
    assert pb.parse(store.raw) == [[["🛍 فروشگاه", "https://t.me/shopbot?start=mbtn_7", "primary"]]]
    await pb.on_cb(_Cb("pqb:color:0", m), st, store, "pqb", "pq:menu", True)
    assert pb.parse(store.raw)[0][0][2] == "success"
    await pb.on_cb(_Cb("pqb:del:0", m), st, store, "pqb", "pq:menu", True)
    assert store.raw == ""


def test_editor_shows_main_option_only_for_tenant():
    _t, kb = pb.editor([], "pqb", "pq:menu", True)
    assert any("صفحهٔ اصلی" in b.text for r in kb.inline_keyboard for b in r)
    _t, kb = pb.editor([], "ppb", "ppg:menu", False)
    assert not any("صفحهٔ اصلی" in b.text for r in kb.inline_keyboard for b in r)


def test_unit_and_help():
    assert pl.split_unit("قیمت 5 تون به ریال") == ("قیمت 5 تون", True)
    assert pl.split_unit("5 تون به تومن") == ("5 تون", False)
    assert pl.is_help("راهنما قیمت") and pl.is_help("/price") and pl.is_help("قیمت‌یاب")
    assert pl.looks_like("قیمت 5 تون به ریال") and not pl.looks_like("سلام بچه‌ها")


def test_rial_rendering():
    from tenant.shared.coin_data import Coin
    tok = pl._RIAL.set(True)
    try:
        t = pl.render_crypto(5, Coin(name="Gram", symbol="TON", price=2.0, change_pct=10.0), 100_000, title="GRAM (ton)")
    finally:
        pl._RIAL.reset(tok)
    assert "قیمت ریالی: 10,000,000 ریال" in t and "تومان" not in t
    t = pl.render_crypto(5, Coin(name="Gram", symbol="TON", price=2.0), 100_000, title="GRAM (ton)")
    assert "قیمت تومانی: 1,000,000 تومان" in t


class _GMsg:
    """پیام ادمین در گروه با حالت کانال (sender_chat)."""

    def __init__(self, text):
        self.text, self.sender_chat, self.replies = text, object(), []

    async def reply(self, text, **kw):
        self.replies.append(text)


@pytest.mark.asyncio
async def test_platform_group_answers_channel_admin_and_help(monkeypatch):
    import platform_price_group as PG
    pset = {}

    async def gp(k, d=""):
        return pset.get(k, d)
    monkeypatch.setattr(PG, "_gp", gp)
    calls = []

    async def core(msg, bot, bot_id, q, rial, kb):
        calls.append((bot_id, q, rial))
        return True
    monkeypatch.setattr(pl, "answer_core", core)
    assert await PG.maybe_answer(_GMsg("قیمت 5 تون به ریال"), None) is False          # خاموش
    pset[PG.ON_KEY] = "on"
    assert await PG.maybe_answer(_GMsg("قیمت 5 تون به ریال"), None) is True
    assert calls[-1] == (0, ("asset", 5.0, "ton"), True)
    assert await PG.maybe_answer(_GMsg("راهنما قیمت"), None) is True and calls[-1][1] is None
    assert await PG.maybe_answer(_GMsg("سلام"), None) is False


def test_platform_wiring():
    import inspect
    import platform_bot
    import platform_group_guard
    src = inspect.getsource(platform_group_guard)
    assert src.count('callback_data="ppg:menu"') == 2 and "platform_price_group import maybe_answer" in src
    assert "platform_price_group_router" in inspect.getsource(platform_bot)


@pytest.mark.asyncio
async def test_brs_items_platform_skips_tenant_settings(monkeypatch):
    seen = []

    async def fake_json(url):
        seen.append(url)
        return {"gold": [{"name": "طلای 18 عیار", "price": 100}]}
    import platform_bot
    async def gp(k, d=""):
        return ""
    monkeypatch.setattr(platform_bot, "get_pset", gp)
    monkeypatch.setattr(pl, "_http_json", fake_json)
    pl._CACHE.clear()
    rows = await pl.brs_items(0)
    assert rows and "Free" in seen[0]


@pytest.mark.asyncio
async def test_tenant_main_buttons_deeplinks(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import config
    import models
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    S = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", S)
    async with S() as s:
        b = models.Bot(owner_id=1, token="t:9", field="booking", plan="pro", username="shopbot")
        s.add(b)
        await s.flush()
        s.add_all([models.MainButton(bot_id=b.id, row=1, col=0, label="🛍 فروشگاه"),
                   models.MainButton(bot_id=b.id, row=0, col=0, label="👤 حساب")])
        await s.commit()
        bid = b.id
    ids = {}
    async with S() as s:
        from sqlalchemy import select
        for i, lbl in (await s.execute(select(models.MainButton.id, models.MainButton.label))).all():
            ids[lbl] = i
    mb = await pl.TenantStore(b, None).main_buttons()
    assert mb == [("👤 حساب", f"https://t.me/shopbot?start=mbtn_{ids['👤 حساب']}"),
                  ("🛍 فروشگاه", f"https://t.me/shopbot?start=mbtn_{ids['🛍 فروشگاه']}")]
    assert bid
    await eng.dispose()
