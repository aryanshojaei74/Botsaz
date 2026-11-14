"""/addays — افزودنِ روز به همهٔ ربات‌ها (جبرانِ قطعی)."""
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_add_days as AD

NOW = dt.datetime(2026, 10, 22, 12, 0)


def bot(i, **kw):
    base = dict(id=i, owner_id=100 + i, token=f"{i}:T", username=f"b{i}", field="vpn_sales", plan="growth",
                status="active", plan_expires_at=NOW + dt.timedelta(days=10))
    base.update(kw)
    return models.Bot(**base)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(AD, "Session", maker)
    monkeypatch.setattr(AD, "PLATFORM_ADMIN_ID", 42)
    async with maker() as s:
        s.add_all([
            bot(1),                                                            # فعال با سررسیدِ آینده
            bot(2, plan_expires_at=None),                                      # بدونِ سررسید
            bot(3, plan_expires_at=NOW - dt.timedelta(days=1)),                # سررسید گذشته ولی هنوز بایگانی نشده
            bot(4, status="paused", archived_at=NOW - dt.timedelta(days=1),    # بایگانیِ اخیرِ عدمِ پرداخت
                archive_reason="nonpayment", plan_expires_at=NOW - dt.timedelta(days=1)),
            bot(5, status="paused", archived_at=NOW - dt.timedelta(days=30),   # بایگانیِ قدیمی
                archive_reason="nonpayment"),
            bot(6, status="locked", archived_at=NOW - dt.timedelta(days=1),    # قفلِ مدیر
                archive_reason="nonpayment"),
            bot(7, username="x#merged", archive_reason="merged_duplicate"),    # سنگِ‌قبرِ ادغام
            bot(8, status="paused", archived_at=NOW - dt.timedelta(days=1),    # بایگانیِ اخیر با دلیلِ دیگر
                archive_reason="user_deleted"),
        ])
        await s.commit()
    yield maker
    await engine.dispose()


async def get(maker, i):
    async with maker() as s:
        return await s.get(models.Bot, i)


@pytest.mark.asyncio
async def test_targets_split_correctly(env):
    plan = await AD.plan_targets(NOW)
    assert sorted(b.id for b in plan["active"]) == [1, 3]
    assert [b.id for b in plan["restore"]] == [4]


@pytest.mark.asyncio
async def test_extend_only(env):
    res = await AD.apply_days(5, include_restore=False, now=NOW)
    assert res["extended"] == 2 and res["restored"] == 0
    assert (await get(env, 1)).plan_expires_at == NOW + dt.timedelta(days=15)          # از سررسیدِ فعلی
    assert (await get(env, 3)).plan_expires_at == NOW + dt.timedelta(days=5)           # منقضی‌شده: از همین‌الان
    assert (await get(env, 2)).plan_expires_at is None
    b4 = await get(env, 4)
    assert b4.archived_at is not None and b4.status == "paused"


@pytest.mark.asyncio
async def test_extend_and_restore(env):
    res = await AD.apply_days(5, include_restore=True, now=NOW)
    assert res["extended"] == 2 and res["restored"] == 1 and res["tokens_restored"] == ["4:T"]
    b4 = await get(env, 4)
    assert b4.archived_at is None and b4.archive_reason is None and b4.status == "active"
    assert b4.plan_expires_at == NOW + dt.timedelta(days=5)
    for untouched in (5, 6, 7, 8):
        assert (await get(env, untouched)).archived_at is not None or untouched == 7
    assert (await get(env, 6)).status == "locked"


@pytest.mark.asyncio
async def test_expiry_notice_is_reset(env):
    async with env() as s:
        b = await s.get(models.Bot, 1)
        b.last_expiry_notice = "2026-10-21"
        await s.commit()
    await AD.apply_days(2, False, now=NOW)
    assert (await get(env, 1)).last_expiry_notice is None


@pytest.mark.asyncio
async def test_days_input_validation_and_preview(env):
    out = []

    async def answer(text, **kw):
        out.append((text, kw))

    class FakeState:
        def __init__(self):
            self.data, self.cleared = {}, False

        async def update_data(self, **kw):
            self.data.update(kw)

        async def get_data(self):
            return self.data

        async def clear(self):
            self.cleared = True

    st = FakeState()
    adm = SimpleNamespace(id=42)
    await AD.ad_days(SimpleNamespace(text="abc", from_user=adm, answer=answer), st)
    assert "❌" in out[-1][0] and not st.data
    await AD.ad_days(SimpleNamespace(text="۵", from_user=adm, answer=answer), st)      # ارقامِ فارسی
    assert st.data["days"] == 5 and "افزودنِ 5 روز" in out[-1][0]
    assert any(b.callback_data == "adays:go" for r in out[-1][1]["reply_markup"].inline_keyboard for b in r)
    await AD.ad_days(SimpleNamespace(text="9999", from_user=adm, answer=answer), st)
    assert "❌" in out[-1][0] and st.data["days"] == 5


@pytest.mark.asyncio
async def test_notify_owners_once_per_bot(env):
    sent = []

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append(chat)

    ok, fail = await AD._notify_owners(Bot(), [(1, "a", 5), (1, "a", 5), (2, "b", 5)], 5)
    assert (ok, fail) == (2, 0) and sent == [1, 2]


@pytest.mark.asyncio
async def test_go_applies_once_and_blocks_double_run(env, monkeypatch):
    async def noop(res):
        pass

    monkeypatch.setattr(AD, "_after_apply", noop)

    class FakeState:
        def __init__(self):
            self.data = {"days": 3, "restore": True, "notify": False}

        async def get_data(self):
            return self.data

        async def update_data(self, **kw):
            self.data.update(kw)

        async def clear(self):
            self.data = {}

    edits, answers = [], []

    async def edit_text(text, **kw):
        edits.append(text)

    async def cb_answer(text=None, show_alert=False):
        answers.append((text, show_alert))

    def cb():
        return SimpleNamespace(data="adays:go", from_user=SimpleNamespace(id=42), answer=cb_answer, bot=None,
                               message=SimpleNamespace(edit_text=edit_text, answer=edit_text))

    st = FakeState()
    await AD.ad_cb(cb(), st)
    assert edits and "انجام شد" in edits[-1] and "ربات از بایگانی برگشت" in edits[-1]
    first_exp = (await get(env, 1)).plan_expires_at
    # دومین اعمالِ فوری باید رد شود و چیزی تغییر نکند
    st2 = FakeState()
    await AD.ad_cb(cb(), st2)
    assert answers[-1][1] is True and "جلوگیری" in answers[-1][0]
    assert (await get(env, 1)).plan_expires_at == first_exp
