

# === PATCH_TRUST_PRODUCT_SPEC_ONLY_V1 === (ιℓιуα: «سفارش» باید فقط حجم/مدت باشد، نه نامِ پنل)
def test_strip_panel_prefix():
    from tenant.shared.trust_report import strip_panel_prefix as f
    assert f("👑 رویال — نامحدود · 30 روز", "👑 رویال") == "نامحدود · 30 روز"
    assert f("🥇طلایی — +10 گیگ / +365 روز", "🥇طلایی") == "+10 گیگ / +365 روز"
    assert f("Royal - 50 GB / 30 روز", "royal") == "50 GB / 30 روز"
    assert f("👑 رویال — نامحدود", "طلایی") == "👑 رویال — نامحدود"        # پیشوند نامِ پنل نیست ⇒ دست‌نخورده
    assert f("۵ گیگ / ۳۰ روز", "رویال") == "۵ گیگ / ۳۰ روز" and f("x", None) == "x" and f(None, "a") == ""
    assert f("رویال —", "رویال") == "رویال —"                              # باقی‌مانده خالی ⇒ دست‌نخورده


import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import trust_report as tr

TEMPLATE = ("🛍 #خرید_موفق | Next V2ray\n\n👤 خریدار: {buyer}\n📦 محصول: {panel}\n🛒 سفارش: {product}\n"
            "💳 مبلغ پرداخت شده: {amount} تومان\n\n📅 {date} | 🕒 {time}\n🤖 @Next_v2bot")


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=681, owner_id=77, token="2:T", username="x", field="vpn_sales", plan="pro", status="active"))
        s.add(models.Setting(bot_id=681, key="trust_channel_id", value="-1002"))
        s.add(models.Setting(bot_id=681, key="trust_channel_template", value=TEMPLATE))
        await s.commit()
    yield maker
    await engine.dispose()


class _Bot:
    def __init__(self):
        self.posts = []

    async def send_message(self, chat_id, text, **kw):
        self.posts.append(text)


@pytest.mark.asyncio
async def test_custom_template_order_line_has_spec_only(db):
    for prod, spec in (("🥇طلایی — +10 گیگ / +365 روز", "+10 گیگ / +365 روز"), ("👑 رویال — نامحدود · 30 روز", "نامحدود · 30 روز")):
        b = _Bot()
        name = prod.split(" — ")[0]
        await tr.send_trust_channel_post(b, 681, buyer_telegram_id=7512345670, product_name=prod, amount=120000, panel_name=name)
        t = b.posts[0]
        assert f"🛒 سفارش: ⁨{spec}⁩" in t and f"📦 محصول: ⁨{name}⁩" in t
        assert t.count(name) == 1
