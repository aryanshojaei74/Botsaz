"""PATCH_GIFT_WIZARD_V1 — گیفت: کامنت ← شناس/ناشناس ← پرداخت (MoBiN)."""
import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm


class _T:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def _txt(kb):
    return [b.text for r in kb.inline_keyboard for b in r]


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, smm):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.TelegramGiftConfig(bot_id=5, gift_id="g1", star_count=50, emoji="🧸", active=True))
        await s.commit()
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    yield state
    await engine.dispose()


@pytest.mark.asyncio
async def test_support_path_goes_comment_then_anon_then_invoice(env):
    state = env
    t = _T()
    await smm._gift_show_invoice(t, 5, "g1", 77, "@u", 1, via="s", state=state)
    text, kb = t.sent[-1]
    assert "کامنتِ همراهِ گیفت" in text and any("wz:skip" in c for c in _cbs(kb))
    assert not any(c.startswith("gift:pay:") for c in _cbs(kb))
    # کامنت می‌نویسد → مرحلهٔ شناس/ناشناس
    await state.update_data(gc_gift="g1", gc_dest=77, gc_qty=1, gc_via="s")

    class M:
        text = "سلام"
        from_user = type("U", (), {"id": 1})()

        async def answer(self, text, reply_markup=None, **kw):
            t.sent.append((text, reply_markup))
    await smm.gift_comment_save(M(), type("B", (), {"token": "5:T"})(), state)
    text, kb = t.sent[-1]
    assert "شناس یا ناشناس" in text and "سلام" in text
    assert "✅ ثبت کردن" in _txt(kb)
    # انتخابِ ناشناس → تیک سبز روی همان
    gc = (await state.get_data())["gift_comment"]
    assert gc["wz"] == "a" and gc["t"] == "سلام"
    gc.update(anon=True, anon_set=True)
    await state.update_data(gift_comment=gc)
    t2 = _T()
    await smm._gift_show_invoice(t2, 5, "g1", 77, "@u", 1, via="s", state=state)
    assert "✅ 🕶 ناشناس" in _txt(t2.sent[-1][1]) and "✅ 👤 شناس" not in _txt(t2.sent[-1][1])
    # ثبت کردن → فاکتور با دکمه‌هایِ پرداخت، بدونِ دکمه‌هایِ ویرایشِ کامنت/ناشناس
    gc["wz"] = "d"
    await state.update_data(gift_comment=gc)
    t3 = _T()
    await smm._gift_show_invoice(t3, 5, "g1", 77, "@u", 1, via="s", state=state)
    text, kb = t3.sent[-1]
    cbs = _cbs(kb)
    assert any(c.startswith("gift:pay:") for c in cbs) and any(c.startswith("gift:cardpay:") for c in cbs)
    assert not any(c.startswith(("gift:cmt:", "gift:an:")) for c in cbs)
    assert "سلام" in text and "ناشناس" in text


@pytest.mark.asyncio
async def test_bot_path_only_comment_step_then_invoice(env):
    state = env
    t = _T()
    await smm._gift_show_invoice(t, 5, "g1", 77, "@u", 1, via="b", state=state)
    assert "کامنتِ همراهِ گیفت" in t.sent[-1][0]
    gc = (await state.get_data())["gift_comment"]
    gc.update(wz="a")           # رد کردن
    await state.update_data(gift_comment=gc)
    t2 = _T()
    await smm._gift_show_invoice(t2, 5, "g1", 77, "@u", 1, via="b", state=state)
    assert any(c.startswith("gift:pay:") for c in _cbs(t2.sent[-1][1]))   # ناشناس فقط برایِ ارسالِ دستی


class _Msg:
    def __init__(self):
        self.out = []

    async def edit_text(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))
        return self

    async def answer(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))


class _Cb:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()
        self.from_user = type("U", (), {"id": 1})()

    async def answer(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_handlers_skip_anon_ok_chain(env, monkeypatch):
    state = env
    monkeypatch.setattr(smm, "_bot_id", lambda bot: _coro(5))
    t = _T()
    await smm._gift_show_invoice(t, 5, "g1", 77, "@u", 1, via="s", state=state)
    bot = object()
    cb = _Cb("gift:wz:skip:g1:77:1:s")
    await smm.gift_wz_skip(cb, bot, state)
    assert "شناس یا ناشناس" in cb.message.out[-1][0]
    cb = _Cb("gift:wz:an:1:g1:77:1:s")
    await smm.gift_wz_anon(cb, bot, state)
    assert "✅ 🕶 ناشناس" in _txt(cb.message.out[-1][1])
    cb = _Cb("gift:wz:ok:g1:77:1:s")
    await smm.gift_wz_ok(cb, bot, state)
    assert any(c.startswith("gift:pay:") for c in _cbs(cb.message.out[-1][1]))
    gc = (await state.get_data())["gift_comment"]
    assert gc["anon"] is True and gc["wz"] == "d"


async def _coro(v):
    return v
