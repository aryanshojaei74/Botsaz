"""PATCH_FJ_TEST_MODE_V1 — مالک/ادمین معافِ عضویتِ اجباری‌اند؛ «حالتِ تست» این معافیت را برمی‌دارد + تشخیصِ وضعیت."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant


class FakeBot:
    id = 99

    def __init__(self, member_status="left", bot_status="administrator"):
        self.ms, self.bs, self.msgs = member_status, bot_status, []

    async def me(self):
        return NS(id=99)

    async def get_chat_member(self, chat, uid):
        return NS(status=self.bs if uid == 99 else self.ms)


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", mk)
    tenant._SETTING_CACHE.clear()
    tenant._FORCEJOIN_CACHE.clear()
    tenant._FORCEJOIN_UNCHECKABLE_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="giveaway", plan="pro", status="active"))
        s.add(models.ForceJoin(bot_id=1, chat_id="-1001", title="کانال من", is_active=True))
        await s.commit()
    yield mk
    await eng.dispose()


@pytest.mark.asyncio
async def test_owner_is_exempt_unless_test_mode_is_on(env):
    b = FakeBot("left")
    assert await tenant.check_force_join(b, 1, 9, bypass_cache=True) == []            # مالک معاف
    assert len(await tenant.check_force_join(b, 1, 555, bypass_cache=True)) == 1       # کاربرِ عادیِ غیرِعضو
    await tenant.set_setting(1, "fj_test_mode", "1")
    nj = await tenant.check_force_join(b, 1, 9, bypass_cache=True)
    assert len(nj) == 1 and nj[0]["chat_id"] == "-1001"                                # حالا مالک هم می‌بیند
    assert await tenant.check_force_join(FakeBot("member"), 1, 9, bypass_cache=True) == []


def _cb(uid=9):
    out = []

    async def answer(*a, **k):
        out.append(("cb", a))

    async def m_answer(t, **k):
        out.append(("msg", t))

    async def edit_rm(**k):
        out.append(("edit", k))
    return NS(data="x", from_user=NS(id=uid), answer=answer,
              message=NS(answer=m_answer, edit_reply_markup=edit_rm)), out


@pytest.mark.asyncio
async def test_diag_reports_bot_admin_user_membership_and_exemption_hint(env, monkeypatch):
    async def gbr(bot):
        return NS(id=1, owner_id=9, plan="pro")
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    cb, out = _cb(9)
    await tenant.fj_diag(cb, FakeBot("left", "administrator"))
    text = [x[1] for x in out if x[0] == "msg"][0]
    assert "کانال من" in text and "✅ ادمین" in text and "عضو نیست" in text and "معاف" in text
    cb, out = _cb(9)
    await tenant.fj_diag(cb, FakeBot("left", "member"))
    assert "باید ادمین باشد" in [x[1] for x in out if x[0] == "msg"][0]


@pytest.mark.asyncio
async def test_hub_shows_test_mode_toggle_and_toggle_flips_setting(env, monkeypatch):
    async def gbr(bot):
        return NS(id=1, owner_id=9, plan="pro")
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    kb = await tenant._fj_hub_kb(1, "pro")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "fj:diag" in cbs and "fj:testmode" in cbs
    cb, out = _cb(9)
    await tenant.fj_test_mode(cb, NS())
    assert await tenant.get_setting(1, "fj_test_mode", "0") == "1"
    cb, out = _cb(9)
    await tenant.fj_test_mode(cb, NS())
    assert await tenant.get_setting(1, "fj_test_mode", "0") == "0"


@pytest.mark.asyncio
async def test_start_deeplink_of_giveaway_is_blocked_for_non_member_and_resumed_payload_saved(env, monkeypatch):
    """/start gw_… از کاربرِ عادیِ غیرِعضو ⇒ صفحهٔ عضویت (نه کارتِ قرعه‌کشی) و payload برایِ بعد از «عضو شدم» ذخیره می‌شود."""
    from aiogram.filters import CommandObject
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    row = NS(id=1, owner_id=9, token="1:T", plan="pro", status="active", field="giveaway", username="b")

    async def gbr(bot):
        return row
    shown, hooked = [], []

    async def fake_fjm(bot, msg, nj):
        shown.append([x["chat_id"] for x in nj])

    async def fake_hook(*a, **k):
        hooked.append(1)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "force_join_msg", fake_fjm)
    monkeypatch.setattr(tenant, "_giveaway_start_hook", fake_hook)

    async def answer(*a, **k):
        return NS(message_id=1, chat=NS(id=555))
    msg = NS(chat=NS(id=555, type="private"), from_user=NS(id=555, full_name="u", username="u", is_bot=False),
             text="/start gw_7_3", answer=answer, reply=answer, message_id=2)
    st = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=555, user_id=555))
    await tenant.tenant_start(msg, CommandObject(prefix="/", command="start", args="gw_7_3"), FakeBot("left"), st)
    assert shown == [["-1001"]] and not hooked
    assert (await st.get_data()).get("pending_gw") == "gw_7_3"


def test_broken_reason_uses_real_telegram_error():
    f = tenant._fj_broken_reason
    assert "پیدا نشد" in f(Exception("Bad Request: chat not found"))
    assert "ادمین" in f(Exception("Bad Request: CHAT_ADMIN_REQUIRED"))
    assert "همین کاربر" in f(Exception("Bad Request: user not found"))
    assert f(Exception("Bad Request: weird thing")).startswith("خطای تلگرام: weird thing")


@pytest.mark.asyncio
async def test_diag_clears_stale_warning_when_live_check_works(env, monkeypatch):
    async def gbr(bot):
        return NS(id=1, owner_id=9, plan="pro")
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    tenant._FORCEJOIN_BROKEN[(1, "-1001")] = "ربات ادمینِ این کانال/گروه نیست"
    tenant._FORCEJOIN_UNCHECKABLE_CACHE[(1, "-1001")] = 10 ** 12
    cb, out = _cb(9)
    await tenant.fj_diag(cb, FakeBot("member", "administrator"))        # بررسیِ زنده موفق
    assert (1, "-1001") not in tenant._FORCEJOIN_BROKEN and (1, "-1001") not in tenant._FORCEJOIN_UNCHECKABLE_CACHE


@pytest.mark.asyncio
async def test_diag_shows_real_reason_when_live_check_fails(env, monkeypatch):
    async def gbr(bot):
        return NS(id=1, owner_id=9, plan="pro")
    monkeypatch.setattr(tenant, "get_bot_row", gbr)

    class Bad(FakeBot):
        async def get_chat_member(self, chat, uid):
            if uid == 99:
                return NS(status="administrator")
            raise Exception("Bad Request: chat not found")
    tenant._FORCEJOIN_BROKEN.clear()
    cb, out = _cb(9)
    await tenant.fj_diag(cb, Bad())
    text = [x[1] for x in out if x[0] == "msg"][0]
    assert "علت" in text and "پیدا نشد" in text
