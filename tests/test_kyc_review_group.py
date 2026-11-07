"""PATCH_KYC_REVIEW_GROUP_V1 (فدایی): درخواستِ احراز در گروهِ بررسی؛ تأیید/ردِ همان‌جا و ادیتِ پیام."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.shared import kyc_admin as KA
from tenant.shared import kyc_review as KR
from tenant.shared import kyc_user as KU

GROUP = -100777


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_photo(self, chat, fid, **kw):
        self.calls.append(("photo", chat, fid, kw))
        return NS(message_id=500)

    async def send_message(self, chat, text, **kw):
        self.calls.append(("msg", chat, text, kw))
        return NS(message_id=501)

    async def send_video(self, chat, fid, **kw):
        self.calls.append(("video", chat, fid, kw))

    async def send_video_note(self, chat, fid, **kw):
        self.calls.append(("vnote", chat, fid, kw))

    async def edit_message_caption(self, **kw):
        self.calls.append(("edit_caption", kw))

    async def edit_message_text(self, **kw):
        self.calls.append(("edit_text", kw))


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (KA, KU, tenant):
        monkeypatch.setattr(mod, "Session", mk) if hasattr(mod, "Session") else None
    monkeypatch.setattr(KA, "_S", mk)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=11, full_name="علی", username="ali", balance=0, kyc_level=0))
        await s.commit()
    await tenant.set_setting(1, "kyc_review_chat", str(GROUP))
    row = NS(id=1, owner_id=9, token="1:T", plan="pro", status="active", field="virtual_service")

    async def gbr(bot):
        return row

    async def ioa(bot_id, uid, owner):
        return uid in (9, 77)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "is_owner_or_admin", ioa)
    yield mk, row
    await eng.dispose()


async def _make_req(mk, level=2, ftype="video_note"):
    async with mk() as s:
        r = models.KycRequest(bot_id=1, user_id=1, level=level, file_id="FID", file_type=ftype, card="6037991234567890" if level == 1 else None)
        s.add(r)
        await s.flush()
        rid = r.id
        await s.commit()
    return rid


def _group_cb(data, uid=77):
    answers = []

    async def answer(t=None, show_alert=False, **k):
        answers.append(t)

    async def reply(text, **k):
        answers.append(("reply", text))
        return NS(message_id=900)
    msg = NS(chat=NS(id=GROUP, type="supergroup"), message_id=501, answer=reply, reply=reply)

    async def delete():
        answers.append("deleted")
    msg.delete = delete
    cb = NS(data=data, from_user=NS(id=uid, full_name="مدیر", username="mod"), message=msg, answer=answer)
    cb.log = answers
    return cb


def _fsm(uid=77):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=GROUP, user_id=uid))


@pytest.mark.asyncio
async def test_request_goes_to_group_with_buttons_and_user_info(env):
    mk, row = env
    rid = await _make_req(mk, level=1, ftype="photo")
    bot = FakeBot()
    async with mk() as s:
        u = await s.get(models.BotUser, 1)
        req = await s.get(models.KycRequest, rid)
    await KU._notify_admins_new(bot, row, u, 1, req)
    kind, chat, fid, kw = bot.calls[0]
    assert kind == "photo" and chat == GROUP and fid == "FID"
    assert "علی" in kw["caption"] and "11" in kw["caption"] and "@ali" in kw["caption"] and "سطح 1" in kw["caption"].replace("۱", "1")
    datas = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]
    assert datas == [f"kyc:ok:{rid}", f"kyc:no:{rid}"]


@pytest.mark.asyncio
async def test_no_group_falls_back_to_owner_dm(env, monkeypatch):
    mk, row = env
    await tenant.set_setting(1, "kyc_review_chat", "")
    sent = []

    async def na(bot, r, text, *a, **k):
        sent.append(text)
    monkeypatch.setattr(tenant, "_notify_admins", na)
    async with mk() as s:
        u = await s.get(models.BotUser, 1)
    await KU._notify_admins_new(FakeBot(), row, u, 2, None)
    assert sent and "احرازِ هویت" in sent[0]


@pytest.mark.asyncio
async def test_approve_in_group_edits_message_and_updates_user(env):
    mk, row = env
    rid = await _make_req(mk, level=2)
    bot = FakeBot()
    cb = _group_cb(f"kyc:ok:{rid}")
    await KA.kyc_approve(cb, bot)
    edits = [c for c in bot.calls if c[0] in ("edit_caption", "edit_text")]
    assert edits and "سطح 2" in edits[-1][1].get("caption", edits[-1][1].get("text", "")).replace("۲", "2")
    txt = edits[-1][1].get("caption") or edits[-1][1].get("text")
    assert "تأیید شد" in txt and "علی" in txt and "@ali" in txt and "مدیر" in txt
    assert edits[-1][1]["chat_id"] == GROUP and edits[-1][1]["message_id"] == 501
    async with mk() as s:
        assert (await s.get(models.BotUser, 1)).kyc_level == 2
        assert (await s.get(models.KycRequest, rid)).status == "approved"


@pytest.mark.asyncio
async def test_reject_in_group_asks_reason_then_edits_with_reason(env):
    mk, row = env
    rid = await _make_req(mk, level=2)
    bot, st = FakeBot(), _fsm()
    cb = _group_cb(f"kyc:no:{rid}")
    await KA.kyc_reject(cb, bot, st)
    assert any(isinstance(x, tuple) and x[0] == "reply" and "دلیل" in x[1] for x in cb.log)
    assert (await st.get_state()) == KA.KycFSM.reject_reason.state
    # ادمین دلیل را می‌نویسد
    replies = []

    async def reply(t, **k):
        replies.append(t)
    msg = NS(text="ویدیو واضح نبود", from_user=NS(id=77, full_name="مدیر"), chat=NS(id=GROUP, type="supergroup"), reply=reply,
             answer=reply)
    await KA.kyc_reject_reason_in(msg, bot, st)
    edits = [c for c in bot.calls if c[0] in ("edit_caption", "edit_text")]
    txt = edits[-1][1].get("caption") or edits[-1][1].get("text")
    assert "رد شد" in txt and "ویدیو واضح نبود" in txt and "علی" in txt
    async with mk() as s:
        r = await s.get(models.KycRequest, rid)
        assert r.status == "rejected" and r.admin_note == "ویدیو واضح نبود"
    assert replies and "رد شد" in replies[0]


@pytest.mark.asyncio
async def test_reject_without_reason_button_in_group(env):
    mk, row = env
    rid = await _make_req(mk, level=1, ftype="photo")
    bot, st = FakeBot(), _fsm()
    await KA.kyc_reject(_group_cb(f"kyc:no:{rid}"), bot, st)
    cb2 = _group_cb(f"kyc:nr:{rid}")
    await KA.kyc_reject_noreason(cb2, bot, st)
    edits = [c for c in bot.calls if c[0] in ("edit_caption", "edit_text")]
    txt = edits[-1][1].get("caption") or edits[-1][1].get("text")
    assert "رد شد" in txt and "بدونِ دلیل" in txt
    assert "deleted" in cb2.log                                # پیامِ پرسشِ دلیل پاک شد


@pytest.mark.asyncio
async def test_non_admin_cannot_decide_in_group(env):
    mk, row = env
    rid = await _make_req(mk, level=2)
    bot = FakeBot()
    cb = _group_cb(f"kyc:ok:{rid}", uid=555)
    await KA.kyc_approve(cb, bot)
    assert not [c for c in bot.calls if c[0].startswith("edit")]
    async with mk() as s:
        assert (await s.get(models.KycRequest, rid)).status == "pending"
