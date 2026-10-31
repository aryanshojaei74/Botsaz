"""محصولِ دستی که به‌جایِ لینک «عدد (آیدی)» می‌گیرد — مثلاً «میو پوینت» (PATCH_SMM_INPUT_KIND_V1)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm

pytestmark = pytest.mark.asyncio
BOT_ID = 3


def test_number_normalization():
    n = smm._norm_number_input
    assert n("۱۲۳۴۵۶") == "123456" and n(" 123 456 ") == "123456" and n("1,234") == "1234" and n("٠٩١") == "091"
    assert n("12a4") is None and n("") is None and n("https://t.me/x") is None and n("1" * 41) is None


def test_input_prompt_and_kind_defaults():
    assert smm._input_kind(None) == "link" and smm._input_kind(SimpleNamespace(input_kind="")) == "link"
    assert smm._input_kind(SimpleNamespace(input_kind="number")) == "number"
    assert smm._input_kind(SimpleNamespace(input_kind="weird")) == "link"
    assert "فقط عدد" in smm._input_prompt("number", "لینک")
    assert smm._input_prompt("link", "لینک هدف") == "لینک هدف"


class St:
    def __init__(self, data):
        self.data, self.state = dict(data), None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, s=None):
        self.state = s


class Msg:
    def __init__(self, text):
        self.text, self.out = text, []
        self.from_user = SimpleNamespace(id=777)

    async def answer(self, text, **kw):
        self.out.append((text, kw))


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", maker)
    monkeypatch.setattr(smm, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=BOT_ID, owner_id=1, token="3:T", username="b", field="virtual_service", plan="pro", status="active"))
        await s.flush()
        s.add(models.SmmService(id=10, bot_id=BOT_ID, service_id="man1", name="میو پوینت", sell_rate=1000,
                                min_qty=1, max_qty=100, is_active=True, is_manual=True, input_kind="number"))
        s.add(models.SmmService(id=11, bot_id=BOT_ID, service_id="man2", name="فالوور", sell_rate=1000,
                                min_qty=1, max_qty=100, is_active=True, is_manual=True))
        await s.commit()

    async def bot_row(_b):
        return SimpleNamespace(id=BOT_ID, owner_id=1, plan="pro")

    async def owner(_cb, _b):
        return SimpleNamespace(id=BOT_ID, owner_id=1, plan="pro")
    monkeypatch.setattr(tenant, "get_bot_row", bot_row)
    monkeypatch.setattr(smm, "_owner", owner)
    yield maker
    await engine.dispose()


async def test_number_product_accepts_digits_and_rejects_text_and_links(env):
    st = St({"smmu_svc_id": 10})
    m = Msg("۱۲۳٬۴۵۶")
    await smm.smmu_link_in(m, object(), st)
    assert st.data["smmu_link"] == "123456" and "آیدی" in m.out[0][0] and "123456" in m.out[0][0]
    for bad in ("abc", "https://t.me/x", "12 ab"):
        st2, m2 = St({"smmu_svc_id": 10}), Msg(bad)
        await smm.smmu_link_in(m2, object(), st2)
        assert "smmu_link" not in st2.data and "فقط عدد" in m2.out[0][0]


async def test_link_product_still_requires_a_link(env):
    st, m = St({"smmu_svc_id": 11}), Msg("123456")
    await smm.smmu_link_in(m, object(), st)
    assert "smmu_link" not in st.data and "لینک معتبر" in m.out[0][0]
    st, m = St({"smmu_svc_id": 11}), Msg("https://t.me/chan")
    await smm.smmu_link_in(m, object(), st)
    assert st.data.get("smmu_link") == "https://t.me/chan"


async def test_owner_can_cycle_input_kind_from_manual_list(env):
    shown = []

    class Cb:
        data = "smm:inpk:11"
        from_user = SimpleNamespace(id=1)

        class message:
            @staticmethod
            async def edit_text(t, **kw):
                shown.append(t)

        async def answer(self, *a, **k):
            pass
    seq = []
    for _ in range(3):
        await smm.smm_input_kind_cycle(Cb(), object())
        async with env() as s:
            seq.append((await s.get(models.SmmService, 11)).input_kind)
    assert seq == ["number", "text", "link"]
    assert "ورودیِ سفارش" in shown[-1]


async def test_bulk_order_is_refused_for_number_product(env):
    m = Msg("")
    async with env() as s:
        v = await s.get(models.SmmService, 10)
    await smm._bulk_start_prompt(m, St({}), v)
    assert "سفارشِ انبوه وجود ندارد" in m.out[0][0]
