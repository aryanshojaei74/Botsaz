"""بکاپِ سرویس‌ها برایِ هر پنل یک فایلِ جدا (PATCH_BACKUP_PER_PANEL_V1) — گزارشِ NITRO SKY: «چند پنل ⇒ همه در یک فایل»."""
import json

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models


@pytest_asyncio.fixture
async def env(monkeypatch):
    import wallet_crypto
    monkeypatch.setattr(wallet_crypto, "WALLET_ENC_KEY", Fernet.generate_key().decode())
    import tenant
    import tenant.shared.panel_clients.panel_tools as pt
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, pt):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    monkeypatch.setattr(config, "PLATFORM_BOT_TOKEN", "999:PLATFORMTOKEN")   # کلیدِ رمزنگاریِ بکاپ از آن مشتق می‌شود
    async with maker() as s:
        b = models.Bot(owner_id=77, token="123:T", username="vpn", field="vpn", plan="pro", status="active")
        s.add(b)
        await s.flush()
        for i, (pk, n) in enumerate((("sanai", 2), ("pasarguard", 3), ("", 1))):
            for j in range(n):
                s.add(models.PanelAccount(bot_id=b.id, user_telegram_id=1000 + i * 10 + j, email=f"{pk or 'x'}{j}@t",
                                          panel_key=pk, plan_title="p", volume_gb=1, days=1, status="active"))
        await s.commit()
        bid = b.id
    return dict(pt=pt, bid=bid, maker=maker, tenant=tenant)


def _emails(pt, blob):
    return sorted(a["email"] for a in json.loads(pt._bk_decrypt(blob).decode())["accounts"])


@pytest.mark.asyncio
async def test_build_backup_filters_by_panel(env):
    pt, bid = env["pt"], env["bid"]
    a = await pt._build_accounts_backup(bid, with_usage=False, panel_key="sanai")
    b = await pt._build_accounts_backup(bid, with_usage=False, panel_key="pasarguard")
    legacy = await pt._build_accounts_backup(bid, with_usage=False, panel_key="")
    everything = await pt._build_accounts_backup(bid, with_usage=False)
    assert _emails(pt, a) == ["sanai0@t", "sanai1@t"]
    assert _emails(pt, b) == ["pasarguard0@t", "pasarguard1@t", "pasarguard2@t"]
    assert _emails(pt, legacy) == ["x0@t"]                # سرویس‌هایِ بدونِ panel_key هم گم نمی‌شوند
    assert len(_emails(pt, everything)) == 6              # رفتارِ قبلی (بدونِ فیلتر) سر جایش


@pytest.mark.asyncio
async def test_auto_backup_sends_one_file_per_panel(env, monkeypatch):
    pt, bid = env["pt"], env["bid"]
    await pt._set_setting(bid, "vpn_backup_hours", "6")
    sent = []

    class FakeBot:
        def __init__(self, token, default=None):
            self.session = type("S", (), {"close": staticmethod(lambda: _noop())})()

        async def send_document(self, chat_id, doc, caption=None):
            sent.append((chat_id, doc.filename, caption, doc.data))

    async def _noop():
        return None

    async def fast_usage(*a, **k):
        return {}
    monkeypatch.setattr(pt, "Bot", FakeBot)
    monkeypatch.setattr(pt, "_collect_usage", fast_usage)
    await pt.vpn_backup_once()
    assert len(sent) == 3 and {s[0] for s in sent} == {77}
    names = [s[1] for s in sent]
    assert len(set(names)) == 3 and any("_sanai_" in n for n in names) and any("_pasarguard_" in n for n in names)
    counts = sorted(len(json.loads(pt._bk_decrypt(s[3]).decode())["accounts"]) for s in sent)
    assert counts == [1, 2, 3]                            # هر فایل فقط سرویس‌هایِ پنلِ خودش
    assert all("بکاپِ خودکارِ پنل" in s[2] for s in sent)
    assert await pt._setting(bid, "vpn_backup_last", "")   # زمانِ آخرین بکاپ ثبت شد
