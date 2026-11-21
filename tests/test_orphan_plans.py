import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import models
from tenant.bots.vpn import vpn_admin as A


def test_orphans(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(A, "Session", S)
        async with S() as s:
            s.add(models.VpnPanel(bot_id=1, panel_key="sanai", base_url="x", username="u", password="p"))
            for k in ("sanai", "marzban", "marzban", "wgres"):
                s.add(models.VpnPlan(bot_id=1, panel_key=k, title="t", volume_gb=1, days=1, price=1))
            s.add(models.VpnPlan(bot_id=2, panel_key="zzz", title="t", volume_gb=1, days=1, price=1))
            await s.commit()
        assert sorted(await A._orphan_plan_groups(1)) == [("marzban", 2), ("wgres", 1)]
    asyncio.run(go())


def test_reply_kb_shows_orphan_button(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t2.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(A, "Session", S)
        import tenant
        async def gs(b, k, d=""): return "reply" if k == "menu_mode" else d
        monkeypatch.setattr(tenant, "get_setting", gs)
        async def bp(b): return "pro"
        monkeypatch.setattr(A, "_bot_plan", bp)
        async with S() as s:
            s.add(models.VpnPlan(bot_id=1, panel_key="marzban", title="t", volume_gb=1, days=1, price=1))
            await s.commit()
        kb = await A.menu_kb([], 1)
        texts = [b.text for r in kb.keyboard for b in r]
        assert any(t.startswith("🧹 پلن‌هایِ بدونِ پنل") for t in texts)
    asyncio.run(go())


def test_label_to_category(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t3.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(A, "Session", S)
        class R: id = 1
        async def own(cb, bot): return R()
        async def lab(bid, key): return "کارمانیا"
        async def content(row, key): return "info", None
        monkeypatch.setattr(A, "_owner", own)
        monkeypatch.setattr(A, "panel_label", lab)
        monkeypatch.setattr(A, "_vpn_panel_menu_content", content)
        async with S() as s:
            for cat in ("KARMANIA", "KARMANIA", "VIP"):
                s.add(models.VpnPlan(bot_id=1, panel_key="sanai", title="t", volume_gb=1, days=1, price=1, category=cat))
            s.add(models.VpnPlan(bot_id=2, panel_key="sanai", title="t", volume_gb=1, days=1, price=1, category="KARMANIA"))
            await s.commit()
        assert await A._panel_categories(1, "sanai") == ["KARMANIA", "VIP"]
        class Cb:
            data = "tvpn:plabcat:sanai:0"
            from_user = type("U", (), {"id": 1})()
            class message:
                @staticmethod
                async def edit_text(*a, **k): pass
                @staticmethod
                async def answer(*a, **k): pass
            async def answer(self, *a, **k): pass
        await A.vpn_panel_label_to_category(Cb(), None)
        assert await A._panel_categories(1, "sanai") == ["کارمانیا", "VIP"]
        async with S() as s:     # رباتِ دیگر دست‌نخورده
            from sqlalchemy import select as _sel
            other = (await s.scalars(_sel(models.VpnPlan).where(models.VpnPlan.bot_id == 2))).all()
            assert other[0].category == "KARMANIA"
    asyncio.run(go())
