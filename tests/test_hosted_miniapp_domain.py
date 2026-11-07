"""PATCH_MINIAPP_DOMAIN_V1 — ساب‌دامینِ خودکارِ مینی‌اپ برایِ اپ‌هایِ میزبانی‌شده (اشتراکِ ماهانه + نگاشتِ nginx)."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import hosted_miniapp_domain as D


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(D, "Session", mk)
    monkeypatch.setattr(D, "MAP_DIR", tmp_path / "hosts.d")
    async with mk() as s:
        s.add(models.PlatformUser(telegram_id=500, full_name="U", balance=250_000))
        s.add(models.HostedApp(id=1, owner_id=500, name="shop1", status="running"))
        s.add(models.HostedApp(id=2, owner_id=500, name="shop2", status="running"))
        await s.commit()
    calls = []

    async def run(*cmd):
        calls.append(cmd)
        return 0, ""
    yield NS(mk=mk, run=run, calls=calls, dir=tmp_path / "hosts.d")
    await engine.dispose()


async def app(env, i):
    async with env.mk() as s:
        return await s.get(models.HostedApp, i)


def test_write_map_validates_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAP_DIR", tmp_path / "d")
    assert D.write_map("shop1", "m1a2b3c4d", "172.31.0.4:8080") is True
    assert (tmp_path / "d" / "shop1.map").read_text() == "m1a2b3c4d.supraplus.ir 172.31.0.4:8080;\n"
    assert D.write_map("shop1", "m1a2b3c4d", "172.31.0.4:8080") is False        # بدون تغییر ⇒ reload لازم نیست
    assert D.write_map("shop1", "m1a2b3c4d", "172.31.0.5:8080") is True
    for bad in (("shop1", "BAD sub", "1.2.3.4:80"), ("shop1", "m1a2b3c4d", "1.2.3.4"), ("shop1", "m1a2b3c4d", "x;1.2.3.4:80"),
                ("../x", "m1a2b3c4d", "1.2.3.4:80")):
        with pytest.raises(ValueError):
            D.write_map(*bad)
    assert D.remove_map("shop1") is True and D.remove_map("shop1") is False


@pytest.mark.asyncio
async def test_price_default_and_setting(env):
    assert await D.get_price() == 100_000
    async with env.mk() as s:
        s.add(models.Setting(bot_id=0, key="vip_miniapp_price", value="150000"))
        await s.commit()
    assert await D.get_price() == 150_000


@pytest.mark.asyncio
async def test_buy_charges_wallet_assigns_sub_and_sets_env(env):
    r = await D.buy(1)
    assert r.ok and r.url.startswith("https://m") and r.url.endswith(".supraplus.ir")
    a = await app(env, 1)
    assert a.miniapp_sub and D.is_active(a)
    assert 29 <= (a.miniapp_until - dt.datetime.now()).days <= 30
    async with env.mk() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 500))
        tx = (await s.scalars(select(models.PlatformTxn))).all()
        envs = {e.key: e.value for e in (await s.scalars(select(models.HostedAppEnv))).all()}
    assert int(u.balance) == 150_000 and len(tx) == 1 and int(tx[0].amount) == -100_000 and tx[0].kind == "miniapp"
    assert envs["MINIAPP_URL"] == r.url
    sub, until = a.miniapp_sub, a.miniapp_until
    r2 = await D.buy(1)                                            # تمدید: همان ساب‌دامین، ۳۰ روز اضافه
    a2 = await app(env, 1)
    assert r2.ok and a2.miniapp_sub == sub and (a2.miniapp_until - until).days == 30


@pytest.mark.asyncio
async def test_buy_insufficient_balance_changes_nothing(env):
    await D.buy(1)
    await D.buy(1)                                                  # موجودی: ۵۰٬۰۰۰
    r = await D.buy(2)
    assert not r.ok and r.need == 50_000 and "کافی نیست" in r.message
    a = await app(env, 2)
    assert a.miniapp_sub is None and a.miniapp_until is None
    async with env.mk() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 500))
    assert int(u.balance) == 50_000


@pytest.mark.asyncio
async def test_unknown_or_archived_app(env):
    assert not (await D.buy(99)).ok
    async with env.mk() as s:
        (await s.get(models.HostedApp, 2)).status = "archived"
        await s.commit()
    assert not (await D.grant(2)).ok


@pytest.mark.asyncio
async def test_grant_is_free_and_disable_removes_map(env, monkeypatch):
    monkeypatch.setattr(D, "reload_nginx", lambda run=None: _noerr())
    r = await D.grant(1, 30)
    assert r.ok
    async with env.mk() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 500))
    assert int(u.balance) == 250_000
    D.write_map("shop1", (await app(env, 1)).miniapp_sub, "172.31.0.4:8080")
    assert await D.disable(1) is True
    assert not (env.dir / "shop1.map").exists() and not D.is_active(await app(env, 1))
    assert (await app(env, 1)).miniapp_sub                           # ساب‌دامین برایِ تمدیدِ بعدی رزرو می‌ماند


async def _noerr():
    return None


class FakeDM:
    def __init__(self):
        self.ip = {"shop1": "172.31.0.4", "shop2": "172.31.0.9"}
        self.port = {"shop1": 8080, "shop2": None}
        self.fail = set()

    async def detect_listen_port(self, name):
        if name in self.fail:
            raise RuntimeError("docker down")
        return self.port.get(name)

    def guess_internal_port(self, envd):
        return 3000

    async def container_ip(self, name):
        return self.ip.get(name)


@pytest.mark.asyncio
async def test_sync_writes_updates_and_removes(env):
    await D.grant(1, 30)
    await D.grant(2, 30)
    dm = FakeDM()
    res = await D.sync(dm, run=env.run)
    assert res["changed"] == 2 and res["removed"] == 0 and res["error"] is None
    s1 = (await app(env, 1)).miniapp_sub
    assert (env.dir / "shop1.map").read_text() == f"{s1}.supraplus.ir 172.31.0.4:8080;\n"
    assert "172.31.0.9:3000" in (env.dir / "shop2.map").read_text()      # پورتِ تشخیص‌داده‌نشده ⇒ حدسِ env
    assert ("nginx", "-t") in env.calls and ("systemctl", "reload", "nginx") in env.calls
    n = len(env.calls)
    assert (await D.sync(dm, run=env.run))["changed"] == 0 and len(env.calls) == n   # بدونِ تغییر ⇒ reload نمی‌شود
    dm.ip["shop1"] = "172.31.0.77"                                       # ری‌استارت ⇒ IP عوض شد
    assert (await D.sync(dm, run=env.run))["changed"] == 1
    assert "172.31.0.77:8080" in (env.dir / "shop1.map").read_text()
    dm.fail.add("shop1")                                                 # خطایِ تشخیص ⇒ نگاشتِ قبلی دست‌نخورده
    await D.sync(dm, run=env.run)
    assert "172.31.0.77:8080" in (env.dir / "shop1.map").read_text()
    dm.fail.clear()
    async with env.mk() as s:                                            # اشتراکِ shop2 تمام شد، shop1 خاموش شد
        (await s.get(models.HostedApp, 2)).miniapp_until = dt.datetime.now() - dt.timedelta(days=1)
        (await s.get(models.HostedApp, 1)).status = "stopped"
        await s.commit()
    res = await D.sync(dm, run=env.run)
    assert res["removed"] == 2 and not list(env.dir.glob("*.map"))
    (env.dir / "ghost.map").write_text("x.supraplus.ir 1.1.1.1:1;\n")   # فایلِ یتیم
    assert (await D.sync(dm, run=env.run))["removed"] == 1 and not (env.dir / "ghost.map").exists()


@pytest.mark.asyncio
async def test_reload_error_is_reported(env):
    async def run(*cmd):
        return (1, "bad conf") if cmd[:2] == ("nginx", "-t") else (0, "")
    assert "معتبر نشد" in await D.reload_nginx(run)
    async def run2(*cmd):
        return (1, "boom") if cmd[0] == "systemctl" else (0, "")
    assert "ریلود" in await D.reload_nginx(run2)


@pytest.mark.asyncio
async def test_set_menu_button_needs_active_sub_and_uses_url(env, monkeypatch):
    import hosted_miniapp as HM
    r = await D.set_menu_button(1)
    assert not r.ok
    await D.grant(1, 30)
    got = {}

    async def fake(envs, url, text="x", **kw):
        got.update(url=url, text=text)
        return HM.Result(True, "ok")
    monkeypatch.setattr(HM, "set_for_app", fake)
    assert (await D.set_menu_button(1, "🛍 فروشگاه")).ok and got["url"] == D.url_of((await app(env, 1)).miniapp_sub) and got["text"] == "🛍 فروشگاه"
