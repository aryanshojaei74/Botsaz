"""PATCH_MANUAL_LINK_LIMITS_V1 / PATCH_UPANEL_USAGE_SHAPE_V1 — سرویسِ دستیِ متصل‌شده نباید
به‌اشتباه «نامحدود / بی‌انقضا» ثبت شود (تیکتِ #835، یوپنل)."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.shared.panel_clients import panel_tools as PT


def test_upanel_shape_from_get_client():
    """دقیقاً پاسخِ واقعیِ سرور: get_client درست، get_usage همه ۰."""
    exp = int((datetime.utcnow() + timedelta(days=20, hours=3)).timestamp())
    cur = {"data_limit": 268435456, "expire": exp, "used_traffic": 2932014, "status": "active"}
    usage = {"total": 0, "expiryTime": 0, "up": 0, "down": 0, "enable": False}
    gb, mb, days, expires = PT.panel_limits(cur, usage)
    assert (gb, mb) == (0, 256)
    assert days == 21 and expires is not None


def test_marzban_iso_and_xui_shapes():
    gb, mb, days, exp = PT.panel_limits(
        {"data_limit": 80530636800, "expire": "2030-10-21T07:45:01Z"}, None)
    assert gb == 75 and mb is None and days > 0 and exp.year == 2030
    ms = int((datetime.utcnow() + timedelta(days=10)).timestamp() * 1000)
    assert PT.panel_limits({"totalGB": 10 * 1024 ** 3, "expiryTime": ms}, {})[0] == 10
    # x-ui «شروع از اولین اتصال»: expiryTime منفی = مدت
    assert PT.panel_limits({"totalGB": 0, "expiryTime": -30 * 86_400_000}, {})[2] == 30
    # واقعاً نامحدود/بی‌انقضا
    assert PT.panel_limits({"data_limit": 0, "expire": None}, {"total": 0, "expiryTime": 0}) == (0, None, 0, None)


@pytest.mark.asyncio
async def test_upanel_usage_ignores_node_usage_shape(monkeypatch):
    from tenant.shared.panel_clients import upanel_mizban as UM
    c = UM.UPanelMizbanClient.__new__(UM.UPanelMizbanClient)
    user = {"data_limit": 268435456, "expire": 1792182599, "used_traffic": 2932014, "status": "active"}

    async def req(method, path, **kw):
        if path.endswith("/usage"):
            return {"usages": [{"node_id": None, "used_traffic": 2932014}]}
        return dict(user)
    monkeypatch.setattr(c, "_request", req, raising=False)
    monkeypatch.setattr(UM, "name_candidates", lambda self, e, f: [e])
    monkeypatch.setattr(UM, "remember", lambda *a: None)
    monkeypatch.setattr(c, "_abs_sub_link", lambda u: u, raising=False)
    u = await c.get_usage("x")
    assert u["total"] == 268435456 and u["expiryTime"] == 1792182599000 and u["enable"]


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(PT, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="v", field="vpn_sales",
                         plan="pro", status="active"))
        for i, (em, gb, days) in enumerate((("bad", 0, 0), ("ok", 20, 29), ("unl", 0, 0)), 1):
            s.add(models.PanelAccount(id=i, bot_id=3, user_telegram_id=9, email=em,
                                      panel_key="upanel_mizban", volume_gb=gb, days=days,
                                      plan_title="سرویس دستی", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_repair_only_touches_zero_rows(db, monkeypatch):
    exp = int((datetime.utcnow() + timedelta(days=5)).timestamp())
    data = {"bad": {"data_limit": 5 * 1024 ** 3, "expire": exp},
            "ok": {"data_limit": 99 * 1024 ** 3, "expire": exp},
            "unl": {"data_limit": 0, "expire": None}}

    class C:
        async def get_client(self, e):
            return data[e]

        async def get_usage(self, e):
            return {"total": 0, "expiryTime": 0}

    async def cf(bid, key):
        return C()

    async def close(c):
        return None
    monkeypatch.setattr(PT, "_client_for", cf)
    monkeypatch.setattr(PT, "_close", close)
    assert await PT.repair_manual_accounts() == 1
    async with db() as s:
        bad, ok, unl = [await s.get(models.PanelAccount, i) for i in (1, 2, 3)]
    assert (bad.volume_gb, bad.days) == (5, 5) and bad.expires_at is not None
    assert (ok.volume_gb, ok.days) == (20, 29)            # ردیفِ سالم دست نخورد
    assert (unl.volume_gb, unl.days) == (0, 0)            # واقعاً نامحدود
