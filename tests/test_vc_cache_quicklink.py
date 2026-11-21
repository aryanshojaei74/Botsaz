"""کش‌هایِ سرعت: تنظیماتِ «خریدِ حجم/روز دلخواه» (PATCH_VC_SETTINGS_CACHE_V1) و فهرستِ «اتصالِ سریع» (PATCH_SMM_QUICKLINK_ITEMS_CACHE_V1)."""
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    import tenant.bots.vpn.vpn_user as V
    from tenant.bots.virtual_service import smm
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, V, smm):
        monkeypatch.setattr(mod, "Session", maker)
    V._VC_CACHE.clear()
    smm._QL_ITEMS_CACHE.clear()
    queries = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _q(conn, cur, st, par, ctx, many):
        queries.append(" ".join(st.split()))
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="x", field="vpn", plan="pro", status="active")
        s.add(b)
        await s.commit()
        bid = b.id
    return dict(V=V, smm=smm, maker=maker, bid=bid, queries=queries)


def _settings_q(queries):
    return [q for q in queries if "FROM settings" in q]


@pytest.mark.asyncio
async def test_vc_reads_are_cached_between_calls(env):
    V, bid = env["V"], env["bid"]
    await V._vc_all(bid, "")
    n1 = len(_settings_q(env["queries"]))
    env["queries"].clear()
    await V._vc_all(bid, "")
    assert n1 >= 10 and len(_settings_q(env["queries"])) == 0      # دفعهٔ دوم هیچ کوئری ندارد


@pytest.mark.asyncio
async def test_vc_semantics_unchanged_empty_row_per_panel_and_legacy_fallback(env):
    V, bid, maker = env["V"], env["bid"], env["maker"]
    async with maker() as s:
        s.add(models.Setting(bot_id=bid, key="vc_price_gb", value="2500"))          # قدیمیِ عمومی
        s.add(models.Setting(bot_id=bid, key="vc_sanai_price_day", value="777"))    # مخصوصِ پنل
        s.add(models.Setting(bot_id=bid, key="rs_price_gb", value=""))             # ردیف هست ولی خالی
        await s.commit()
    V._VC_CACHE.clear()
    assert await V._vc(bid, "vc_price_gb", "sanai") == "2500"        # per-panel نبود ⇒ قدیمیِ عمومی
    assert await V._vc(bid, "vc_price_day", "sanai") == "777"        # per-panel هست
    assert await V._vc(bid, "vc_min_gb", "sanai") == V._VC_DEF["vc_min_gb"]   # هیچ‌کدام ⇒ پیش‌فرض
    assert await V._vc(bid, "rs_price_gb", "") == ""                 # ردیفِ خالی ⇒ "" (نه پیش‌فرض)


@pytest.mark.asyncio
async def test_vc_cache_expires(env, monkeypatch):
    V, bid, maker = env["V"], env["bid"], env["maker"]
    assert await V._vc(bid, "vc_min_gb", "") == V._VC_DEF["vc_min_gb"]
    async with maker() as s:
        s.add(models.Setting(bot_id=bid, key="vc_min_gb", value="9"))
        await s.commit()
    assert await V._vc(bid, "vc_min_gb", "") == V._VC_DEF["vc_min_gb"]       # هنوز در پنجرهٔ کش
    monkeypatch.setattr(V, "_VC_CACHE_TTL", 0.0)
    V._VC_CACHE.clear()
    assert await V._vc(bid, "vc_min_gb", "") == "9"                          # بعد از انقضا تازه می‌شود


@pytest.mark.asyncio
async def test_quicklink_items_loaded_once_and_refreshed_when_services_change(env, monkeypatch):
    smm, bid, maker = env["smm"], env["bid"], env["maker"]
    async with maker() as s:
        for i in range(3):
            s.add(models.SmmService(bot_id=bid, service_id=str(i), name=f"s{i}", is_active=True,
                                    platform_key="telegram", type_key="member"))
        s.add(models.SmmService(bot_id=bid, service_id="9", name="other", is_active=True,
                                platform_key="instagram", type_key="like"))
        await s.commit()
    calls = {"n": 0}
    real = smm._svc_platform_type

    def counting(v):
        calls["n"] += 1
        return real(v)
    monkeypatch.setattr(smm, "_svc_platform_type", counting)
    a = await smm._ql_items_for(bid, "", "telegram", "member")
    n_first = calls["n"]
    b = await smm._ql_items_for(bid, "", "telegram", "member")
    assert [x[1] for x in a] == ["s0", "s1", "s2"] and a == b
    assert n_first == 4 and calls["n"] == n_first                    # دفعهٔ دوم طبقه‌بندیِ تازه نشد
    async with maker() as s:
        s.add(models.SmmService(bot_id=bid, service_id="5", name="s5", is_active=True,
                                platform_key="telegram", type_key="member"))
        await s.commit()
    c = await smm._ql_items_for(bid, "", "telegram", "member")
    assert [x[1] for x in c] == ["s0", "s1", "s2", "s5"]             # با تغییرِ سرویس‌ها خودکار تازه شد
