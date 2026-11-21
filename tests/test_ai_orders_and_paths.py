"""SINA («اکسا»): لیستِ سفارش‌ها نباید در کوتاه‌کردنِ context جا بماند؛ و بوست نباید به دکمهٔ استارز ارجاع شود."""
from tenant.bots.virtual_service import smm_ai as A


def test_order_lines_survive_tight_budget():
    ctx = "\n".join(["اطلاعات:"] + [f"- مسیر «دکمه {i}»: " + "x" * 120 for i in range(200)]
                    + ["آخرین سفارش‌هایِ همین مشتری:", "▪ #AB12: بوست × 10, وضعیت: done, مبلغ: 17,520 تومان"])
    out = A.fit_context(ctx, "لیست سفارشاتمو بفرست", 3000)
    assert "▪ #AB12" in out and "آخرین سفارش‌هایِ همین مشتری" in out


def test_system_prompt_forbids_stars_for_boost_and_requires_order_list():
    class R:
        field = "virtual_service"
    p = A._bot_system_prompt(R())
    assert "نگو اطلاعات در دسترس نیست" in p and "استارز/تون فقط" in p


import pytest


@pytest.mark.asyncio
async def test_recent_orders_context_uses_all_virtual_orders(monkeypatch):
    """SINA: سفارشِ استارز/گیفت هم باید در context بیاید (قبلاً فقط SMM ⇒ «سفارشی ثبت نکرده»)."""
    import datetime as dt
    from tenant.bots.virtual_service import smm

    async def fake(bot_id, tg, query=""):
        return [{"kind": "gift", "id": 1, "code": "ST77", "name": "⭐ 100 استارز", "price": 20000, "status": "تحویل شد",
                 "at": dt.datetime(2026, 11, 4)}]
    monkeypatch.setattr(smm, "user_vs_orders", fake)
    out = await A._recent_orders_context(1, 5)
    assert "▪ #ST77" in out and "100 استارز" in out and "مجموعِ سفارش‌ها: 1" in out

    async def empty(bot_id, tg, query=""):
        return []
    monkeypatch.setattr(smm, "user_vs_orders", empty)
    assert "هنوز هیچ سفارشی" in await A._recent_orders_context(1, 5)


def test_orders_first_survive_hard_truncation():
    """وقتی تیترهایِ غیرِفهرست از بودجه بیشتر شود، fit_context «برشِ سخت» از انتها می‌زند؛ سفارش‌ها باید اولِ context باشند."""
    orders = "آخرین سفارش‌هایِ همین مشتری (مجموعِ سفارش‌ها: 1؛ 1 موردِ آخر در زیر آمده):\n▪ #ST77: ⭐ 100 استارز, وضعیت: تحویل شد, مبلغ: 20,000 تومان"
    live = "\n".join(f"عنوانِ غیرِفهرست {i} " + "y" * 80 for i in range(100))
    out = A.fit_context(orders + "\n\n" + live, "لیست سفارشاتمو بفرست", 1500)
    assert "▪ #ST77" in out


def test_price_survives_tight_budget_when_asked_about_a_service():
    """SINA: «بوست ۱ روزه چنده؟» ⇒ خطِ همان سرویس با قیمتش باید در context بماند (نام+قیمت اول است، حذف/کوتاه نمی‌شود)."""
    rules = "- قوانینِ بخشِ خدماتِ مجازیِ این ربات — متنِ دقیقِ مالک: «" + "ق" * 1500 + "»"
    menu = [f"- مسیر «دکمه {i}»: " + "x" * 150 for i in range(40)]
    svc = ["- بوست ۱ روزه: حداقل 10 عدد = 17,520 تومان (۱۰۰۰ عدد = 1,752,000 تومان)؛ دسته تلگرام؛ حداکثر 1000"] + \
          [f"- سرویس دیگر {i}: حداقل 10 عدد = 5,000 تومان؛ دسته عمومی" for i in range(30)]
    ctx = "اطلاعات اختصاصی:\n" + "\n".join(menu) + "\n" + rules + "\nسرویس‌های فعال:\n" + "\n".join(svc)
    out = A.fit_context(ctx, "اکسا بوست 1 روزه چنده؟", 2500)
    assert "17,520" in out
    assert "قققق" not in out                         # قوانینِ بی‌ربط جا را نمی‌گیرند


@pytest.mark.asyncio
async def test_user_facts_only_for_the_asking_user(monkeypatch):
    from decimal import Decimal
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import config
    import models
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(A, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=2, owner_id=1, token="2:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=2, telegram_id=10, balance=Decimal(12345), kyc_level=1, phone_verified=True))
        s.add(models.BotUser(bot_id=2, telegram_id=11, balance=Decimal(99999), kyc_level=2))
        await s.commit()
    out = await A._user_facts_context(2, 10)
    assert "12,345" in out and "سطحِ احرازِ هویت: 1" in out and "99,999" not in out
    assert await A._user_facts_context(2, 777) == ""
    await eng.dispose()
