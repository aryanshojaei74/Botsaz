"""PATCH_BACKUP_DEST_V1 — مقصدِ بک‌آپ (گروه/کانال/پیویِ شخصِ دیگر) + ارسالِ فوری + بک‌آپِ خودکار."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.shared import backup_full as bf
from tenant.shared import extras

pytestmark = pytest.mark.asyncio
BOT, OWNER = 5, 100


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (bf, extras, tenant):
        monkeypatch.setattr(mod, "Session", mk)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="shop", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    row = SimpleNamespace(id=BOT, owner_id=OWNER, username="shop")

    async def owner_row(x, bot):
        return row if x.from_user.id == OWNER else None

    monkeypatch.setattr(bf, "_owner_row", owner_row)
    yield mk
    await eng.dispose()


class FakeBot:
    def __init__(self, fail_for=()):
        self.docs, self.msgs, self.fail_for = [], [], set(fail_for)

    async def get_chat(self, target):
        if str(target) in self.fail_for:
            raise RuntimeError("chat not found")
        return SimpleNamespace(id=int(target) if str(target).lstrip("-").isdigit() else -1009, title="گروه بک‌آپ")

    async def send_message(self, chat_id, text, **kw):
        if str(chat_id) in self.fail_for:
            raise RuntimeError("forbidden")
        self.msgs.append((chat_id, text))

    async def send_document(self, chat_id, doc, **kw):
        if str(chat_id) in self.fail_for:
            raise RuntimeError("forbidden")
        self.docs.append((chat_id, doc.filename))


class FakeState:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


def msg(text="", uid=OWNER, fwd_chat=None, fwd_user=None):
    out = []

    async def answer(t, **kw):
        out.append(t)

    origin = None
    if fwd_chat is not None:
        origin = SimpleNamespace(chat=SimpleNamespace(id=fwd_chat))
    elif fwd_user is not None:
        origin = SimpleNamespace(sender_user=SimpleNamespace(id=fwd_user))
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=uid), answer=answer, forward_origin=origin, out=out)


def cb(data="x", uid=OWNER):
    out = []

    async def answer(t="", **kw):
        out.append(("alert", t))

    async def msg_answer(t, **kw):
        out.append(("msg", t))

    async def edit_text(t, **kw):
        out.append(("edit", t))

    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), answer=answer,
                           message=SimpleNamespace(answer=msg_answer, edit_text=edit_text), out=out)


def test_parse_and_label():
    assert bf._parse_dest("-1001") == -1001 and bf._parse_dest("") is None and bf._parse_dest("0") is None
    assert bf.dest_label("", "") == "پیویِ خودم" and bf.dest_label("-100", "گروه") == "گروه" and bf.dest_label("77", "") == "77"


@pytest.mark.parametrize("m,expect", [
    (msg("@mychan"), "@mychan"), (msg("-1001234"), -1001234), (msg("555"), 555),
    (msg(fwd_chat=-1007), -1007), (msg(fwd_user=42), 42), (msg("سلام"), None), (msg("@a b"), None),
])
def test_dest_parse_from_message(m, expect):
    assert bf._dest_from_message(m) == expect


async def test_set_destination_verifies_and_saves(env):
    bot, st = FakeBot(), FakeState()
    m = msg("-1001234")
    await bf.full_dest_set_save(m, bot, st)
    assert bot.msgs and bot.msgs[0][0] == -1001234                      # پیامِ آزمایشی به مقصد رفت
    assert await tenant.get_setting(BOT, bf.DEST_KEY, "") == "-1001234" and st.cleared
    assert await tenant.get_setting(BOT, bf.DEST_TITLE_KEY, "") == "گروه بک‌آپ"


async def test_set_destination_failure_does_not_save(env):
    bot, st = FakeBot(fail_for={"-1001234"}), FakeState()
    m = msg("-1001234")
    await bf.full_dest_set_save(m, bot, st)
    assert await tenant.get_setting(BOT, bf.DEST_KEY, "") == "" and not st.cleared and "❌" in m.out[0]


async def test_invalid_input_keeps_waiting(env):
    st = FakeState()
    m = msg("هر چیزی")
    await bf.full_dest_set_save(m, FakeBot(), st)
    assert not st.cleared and "❌" in m.out[0]


async def test_non_owner_cannot_set(env):
    bot, st = FakeBot(), FakeState()
    await bf.full_dest_set_save(msg("-1001234", uid=999), bot, st)
    assert not bot.msgs and st.cleared and await tenant.get_setting(BOT, bf.DEST_KEY, "") == ""


async def test_send_now_requires_destination_then_sends_there(env):
    bot = FakeBot()
    c = cb()
    await bf.full_dest_send_now(c, bot)
    assert c.out[0][0] == "alert" and not bot.docs
    await tenant.set_setting(BOT, bf.DEST_KEY, "-1001234")
    c2 = cb()
    await bf.full_dest_send_now(c2, bot)
    assert bot.docs and bot.docs[0][0] == -1001234 and bot.docs[0][1].endswith(".botsazfull")
    assert ("msg", "✅ بک‌آپ به مقصد فرستاده شد.") in c2.out


async def test_send_now_failure_reports(env):
    await tenant.set_setting(BOT, bf.DEST_KEY, "-1001234")
    c = cb()
    await bf.full_dest_send_now(c, FakeBot(fail_for={"-1001234"}))
    assert any("ممکن نشد" in t for _, t in c.out)


async def test_clear_destination(env):
    await tenant.set_setting(BOT, bf.DEST_KEY, "-1001234")
    await bf.full_dest_clear(cb(), None)
    assert bf._parse_dest(await tenant.get_setting(BOT, bf.DEST_KEY, "")) is None


async def test_menu_shows_destination_button(env):
    from tenant.shared.backup_restore import _backup_menu_content
    await tenant.set_setting(BOT, bf.DEST_KEY, "-1001234")
    await tenant.set_setting(BOT, bf.DEST_TITLE_KEY, "کانال من")
    _, kb = await _backup_menu_content(BOT)
    btns = {b.callback_data: b.text for r in kb.inline_keyboard for b in r}
    assert "کانال من" in btns["tadm:bkfull:dest"]


async def _enable_auto(mk, dest=None):
    async with mk() as s:
        s.add(models.Setting(bot_id=BOT, key="auto_full_backup_days", value="1"))
        if dest:
            s.add(models.Setting(bot_id=BOT, key=bf.DEST_KEY, value=dest))
        await s.commit()


async def test_auto_backup_goes_to_owner_by_default_and_to_destination_when_set(env, monkeypatch):
    sent = FakeBot()
    monkeypatch.setattr(bf, "Bot", lambda **kw: sent)
    await _enable_auto(env)
    assert await bf.full_backup_auto_once() == 1 and sent.docs[0][0] == OWNER
    # مقصد تنظیم ⇒ فاصلهٔ زمانی را ریست کن و دوباره بفرست
    async with env() as s:
        for r in (await s.scalars(select(models.Setting).where(models.Setting.key == "auto_full_backup_last"))).all():
            r.value = (datetime.utcnow() - timedelta(days=3)).isoformat()
        s.add(models.Setting(bot_id=BOT, key=bf.DEST_KEY, value="-1001234"))
        await s.commit()
    tenant._SETTING_CACHE.clear()
    sent.docs.clear()
    assert await bf.full_backup_auto_once() == 1 and sent.docs[0][0] == -1001234


async def test_auto_backup_falls_back_to_owner_when_destination_broken(env, monkeypatch):
    sent = FakeBot(fail_for={"-1001234"})
    monkeypatch.setattr(bf, "Bot", lambda **kw: sent)
    await _enable_auto(env, dest="-1001234")
    assert await bf.full_backup_auto_once() == 1
    assert sent.docs[0][0] == OWNER and any("ممکن نبود" in t for _, t in sent.msgs)
