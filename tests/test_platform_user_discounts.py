"""PATCH_PLATFORM_USER_DISCOUNT_V1 — تخفیفِ دائمیِ اختصاصیِ کاربر در رباتِ مادر."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_user_discounts as UD


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, UD):
        monkeypatch.setattr(mod, "Session", maker)
    monkeypatch.setattr(UD, "PLATFORM_ADMIN_ID", 1)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=55, full_name="Ali", username="ali", balance=0))
        await s.commit()
    yield maker
    await engine.dispose()


def test_parse_percent():
    assert UD.parse_percent("۲۰٪") == 20 and UD.parse_percent("0") == 0 and UD.parse_percent("90") == 90
    assert UD.parse_percent("91") is None and UD.parse_percent("abc") is None and UD.parse_percent("-5") is None


@pytest.mark.asyncio
async def test_set_update_remove_and_active_flag(db):
    assert await UD.percent_for(55) == 0
    assert await UD.set_discount(55, 15) == "created" and await UD.percent_for(55) == 15
    assert await UD.set_discount(55, 25) == "updated" and await UD.percent_for(55) == 25
    async with db() as s:
        d = await s.scalar(select(models.PlatformUserDiscount))
        d.active = False
        await s.commit()
    assert await UD.percent_for(55) == 0                       # خاموش = اثری ندارد
    assert await UD.set_discount(55, 30) == "updated" and await UD.percent_for(55) == 30   # دوباره روشن
    assert await UD.set_discount(55, 0) == "removed" and await UD.percent_for(55) == 0
    assert await UD.set_discount(55, 0) == "none"


@pytest.mark.asyncio
async def test_plan_filter(db):
    await UD.set_discount(55, 20, plans=["pro"])
    assert await UD.percent_for(55, "pro") == 20
    assert await UD.percent_for(55, "growth") == 0          # روی رشد اعمال نمی‌شود
    assert await UD.percent_for(55) == 20                   # بدونِ فیلترِ پلن
    await UD.set_discount(55, 20, plans=[])
    assert await UD.percent_for(55, "growth") == 20 and await UD.percent_for(55, "pro") == 20   # همهٔ پلن‌ها
    await UD.set_discount(55, 20, plans=["bogus", "growth"])
    assert await UD.percent_for(55, "growth") == 20 and await UD.percent_for(55, "pro") == 0


@pytest.mark.asyncio
async def test_resolve_user_id_username_and_telegram_fallback(db):
    assert await UD.resolve_user("۵۵") == 55
    assert await UD.resolve_user("@Ali") == 55               # از دیتابیس، بدونِ حساسیت به حروف
    assert await UD.resolve_user("nobody") is None
    assert await UD.resolve_user("bad name!") is None

    class Bot:
        async def get_chat(self, name):
            assert name == "@stranger"
            return SimpleNamespace(id=777, type="private")
    assert await UD.resolve_user("@stranger", Bot()) == 777
    class Chan:
        async def get_chat(self, name):
            return SimpleNamespace(id=-100, type="channel")
    assert await UD.resolve_user("@somechan", Chan()) is None   # کانال/گروه نه


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text))

    async def get_chat(self, name):
        raise RuntimeError("not found")


class _St:
    def __init__(self):
        self.d = {}

    async def update_data(self, **k):
        self.d.update(k)

    async def get_data(self):
        return dict(self.d)

    async def set_state(self, s):
        pass

    async def clear(self):
        self.d = {}


class _Msg:
    def __init__(self, text, bot, uid=1):
        self.text, self.from_user, self.bot, self.out = text, SimpleNamespace(id=uid), bot, []

    async def answer(self, text, **k):
        self.out.append(text)


class _Cb:
    def __init__(self, data, bot, uid=1):
        self.data, self.from_user, self.bot = data, SimpleNamespace(id=uid), bot
        self.out = []
        outer = self

        class M:
            async def answer(self, text, **k):
                outer.out.append(text)

            async def edit_reply_markup(self, **k):
                pass
        self.message = M()

    async def answer(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_admin_flow_username_percent_plans_save_and_notify(db):
    bot, st = _Bot(), _St()
    m1 = _Msg("@ali", bot)
    await UD.ud_user(m1, st)                                   # یوزرنیم پذیرفته می‌شود
    assert "Ali" in m1.out[0] and st.d["ud_uid"] == 55
    m2 = _Msg("20", bot)
    await UD.ud_percent(m2, st)                                # درصد → صفحهٔ انتخابِ پلن
    assert "کدام پلن" in m2.out[0] and st.d["ud_pct"] == 20
    await UD.ud_plan_toggle(_Cb("ud:p:pro", bot), st)
    assert st.d["ud_plans"] == ["pro"]
    cb = _Cb("ud:psave", bot)
    await UD.ud_save(cb, st)
    assert await UD.percent_for(55, "pro") == 20 and await UD.percent_for(55, "growth") == 0
    assert bot.sent and bot.sent[0][0] == 55 and "20" in bot.sent[0][1] and "حرفه‌ای" in bot.sent[0][1]
    # غیرِ مدیر اثری ندارد
    m3 = _Msg("55", bot, uid=999)
    await UD.ud_user(m3, st)
    assert not m3.out
    # صفر = حذف بدونِ پرسیدنِ پلن
    st.d = {"ud_uid": 55}
    m4 = _Msg("0", bot)
    await UD.ud_percent(m4, st)
    assert await UD.percent_for(55) == 0
