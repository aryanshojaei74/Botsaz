"""PATCH_CATEGORY_SALE_FULL_V1 — «ظرفیتِ فروش پر است» برایِ دسته‌هایِ ربات و مینی‌اپ (تیکت #845)."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
import tenant.bots.vpn.vpn_user as VU
import tenant.shared.miniapp as MA
import tenant.shared.miniapp_admin as MAD


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (tenant, VU, MA, MAD):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="w", field="vpn_sales",
                         plan="pro", status="active"))
        s.add(models.MiniAppCategory(id=1, bot_id=4, title="حجمی - پرسرعت", sale_full=True))
        s.add(models.MiniAppCategory(id=2, bot_id=4, title="حجمی - vip"))
        s.add(models.VpnPlan(id=10, bot_id=4, panel_key="sanai", title="P", volume_gb=10, days=30,
                             price=1000, active=True, miniapp_category_id=1))
        s.add(models.VpnPlan(id=11, bot_id=4, panel_key="sanai", title="Q", volume_gb=10, days=30,
                             price=1000, active=True, miniapp_category_id=2))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_plan_full_message_only_for_full_category(db):
    assert "پر شده" in (await VU.plan_sale_full_message(4, 10))
    assert await VU.plan_sale_full_message(4, 11) is None
    assert await VU.plan_sale_full_message(5, 10) is None            # ربات دیگر


class _Msg:
    def __init__(self):
        self.out = []

    async def answer(self, text, reply_markup=None, **k):
        self.out.append((text, reply_markup))


@pytest.mark.asyncio
async def test_all_categories_list_marks_full_and_pick_is_blocked(db):
    m = _Msg()
    await VU.show_bundle_all_categories(None, m, 4)
    kb = m.out[0][1]
    labels = {b.callback_data: b.text for r in kb.inline_keyboard for b in r}
    assert "ظرفیت پر" in labels["bndl:cat:1"] and "ظرفیت پر" not in labels["bndl:cat:2"]

    class Cb:
        data = "bndl:cat:1"
        alerts = []
        message = _Msg()

        async def answer(self, text="", show_alert=False, **k):
            Cb.alerts.append((text, show_alert))

    class St:
        async def get_data(self):
            return {}
    await VU.bundle_pick_category(Cb(), type("B", (), {"token": "4:T"})(), St())
    assert Cb.alerts and Cb.alerts[0][1] and "پر شده" in Cb.alerts[0][0]
    assert not Cb.message.out                                          # صفحهٔ خرید باز نشد


@pytest.mark.asyncio
async def test_miniapp_api_flags_and_blocks_full_category(db, monkeypatch):
    async def ctx(bot_id, init, kb):
        return None, type("U", (), {"telegram_id": 1})()
    monkeypatch.setattr(MA, "_authed_context", ctx)
    import json
    r = json.loads((await MA.api_categories(4, None, None)).body)
    full = {c["id"]: c["full"] for c in r["categories"] if c["id"]}
    assert full == {1: True, 2: False}
    r = json.loads((await MA.api_plans(4, 1, None, None)).body)
    assert r["full"] is True and r["plans"] == [] and "پر شده" in r["message"]
    r = json.loads((await MA.api_plans(4, 2, None, None)).body)
    assert [p["id"] for p in r["plans"]] == [11] and not r.get("full")


@pytest.mark.asyncio
async def test_admin_toggle_flips_flag_and_label(db):
    from types import SimpleNamespace

    class Msg:
        edits = []

        async def edit_text(self, text, reply_markup=None, **k):
            Msg.edits.append(reply_markup)

    class Cb:
        data = "miniapp:catfull:2"
        from_user = SimpleNamespace(id=1)
        message = Msg()

        async def answer(self, *a, **k):
            pass
    bot = SimpleNamespace(token="4:T")
    await MAD.miniapp_cat_full_toggle(Cb(), bot)                  # آزاد → پر
    async with db() as s:
        assert (await s.get(models.MiniAppCategory, 2)).sale_full is True
    labels = [b.text for r in Msg.edits[-1].inline_keyboard for b in r]
    assert any("ظرفیتِ فروش: پر" in t for t in labels)
    Cb.data = "miniapp:catfull:2"
    await MAD.miniapp_cat_full_toggle(Cb(), bot)                  # پر → آزاد
    async with db() as s:
        assert (await s.get(models.MiniAppCategory, 2)).sale_full is False
