"""PATCH_TRIAL_REPORT_CHANNEL_V1 — گزارشِ اکانتِ تست در کانالِ مالک."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from plans import Feature, has_feature
from tenant.shared import trial_report as TR


def test_plan_gating_growth_and_pro_only():
    assert has_feature("growth", Feature.TRIAL_REPORT_CHANNEL)
    assert has_feature("pro", Feature.TRIAL_REPORT_CHANNEL)
    assert not has_feature("free", Feature.TRIAL_REPORT_CHANNEL)


def test_build_report_content_and_escaping():
    t = TR.build_report(user_id=7, username="ali", full_name="A<b>", link="https://x/s?a=1&b=2",
                        volume="0.1 گیگ", duration="1 ساعت", panel="GAMING", source="panel")
    assert t.startswith("#تست_جدید")
    assert "<code>https://x/s?a=1&amp;b=2</code>" in t and "A&lt;b&gt;" in t
    assert "⏱ 1 ساعت | 📦 0.1 گیگ" in t and "🚀 پنل: GAMING" in t
    assert "id: <code>7</code>" in t and "@ali" in t
    t2 = TR.build_report(user_id=7, username=None, full_name="B", link="L", volume="v", duration="d",
                         panel=None, source="stock", show_link=False)
    assert "لینک" not in t2 and "انبار دستی" in t2 and "—" in t2


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text))


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="vpn_sales",
                         plan="growth", status="active"))
        s.add(models.Bot(id=6, owner_id=1, token="6:T", username="y", field="vpn_sales",
                         plan="free", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


class U:
    telegram_id = 99
    full_name = "T"
    username = None


@pytest.mark.asyncio
async def test_report_posts_only_when_channel_set_and_respects_link_toggle(db):
    b = FakeBot()
    await TR.report(b, 5, U(), link="L1", volume="1 گیگ", duration="1 روز", panel_name="P")
    assert b.sent == []                                         # کانال تنظیم نشده
    await tenant.set_setting(5, "trial_report_channel", "-1001")
    await TR.report(b, 5, U(), tg_username="zed", link="L1", volume="1 گیگ", duration="1 روز", panel_name="P")
    assert b.sent[0][0] == -1001 and "L1" in b.sent[0][1] and "@zed" in b.sent[0][1]
    await tenant.set_setting(5, "trial_report_link", "0")
    await TR.report(b, 5, U(), link="SECRET", volume="v", duration="d", panel_name="P")
    assert "SECRET" not in b.sent[1][1]


@pytest.mark.asyncio
async def test_hook_is_plan_gated_and_never_raises(db):
    await tenant.set_setting(5, "trial_report_channel", "-1001")
    await tenant.set_setting(6, "trial_report_channel", "-1002")
    b = FakeBot()
    await tenant._trial_report_post(b, 6, U(), None, "L", "v", "d", None, "stock")   # پلن رایگان → هیچ
    assert b.sent == []
    await tenant._trial_report_post(b, 5, U(), None, "L", "v", "d", None, "stock")
    assert len(b.sent) == 1

    class Boom(FakeBot):
        async def send_message(self, *a, **k):
            raise RuntimeError("blocked")
    await tenant._trial_report_post(Boom(), 5, U(), None, "L", "v", "d", None, "stock")  # خطا قورت می‌شود
