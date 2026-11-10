import asyncio
from tenant.bots.vpn import vpn_user as V


def test_min_quote_when_day_price_zero(monkeypatch):
    async def go():
        rc = V._RenewCfg(True, 0, 1000, 30, 60, 30, 10, 40, 10, 0, [])
        async def quote(b, t, pk, rc_, days, gb=0):
            return gb * 1000 if gb else 0           # فروش فقط بر اساسِ حجم؛ روزانه صفر
        monkeypatch.setattr(V, "_renew_quote", quote)
        assert V._renew_gb_options(rc) == [10, 20, 30, 40]
        q, only = await V._renew_min_quote(1, 2, "p", rc, 30)
        assert (q, only) == (10000, False)
        async def quote2(b, t, pk, rc_, days, gb=0):
            return days * 100 + gb * 1000
        monkeypatch.setattr(V, "_renew_quote", quote2)
        q, only = await V._renew_min_quote(1, 2, "p", rc, 30)
        assert (q, only) == (3000, True)
        async def none(b, t, pk, rc_, days, gb=0): return 0
        monkeypatch.setattr(V, "_renew_quote", none)
        assert (await V._renew_min_quote(1, 2, "p", rc, 30)) == (0, False)
    asyncio.run(go())
