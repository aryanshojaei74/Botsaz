"""PATCH_SECRET_BOX_V1 — رمزنگاریِ شفافِ رمزِ پنل (اختیاری، پیش‌فرض خاموش)."""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import secret_box as SB


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("BOTSAZ_SECRET_KEY", "unit-test-key")
    monkeypatch.delenv("BOTSAZ_ENCRYPT_SECRETS", raising=False)
    SB._warned = False


def test_roundtrip_and_plaintext_passthrough():
    enc = SB.encrypt("p@ss-W0rd")
    assert enc.startswith("enc1:") and "p@ss" not in enc and SB.decrypt(enc) == "p@ss-W0rd"
    assert SB.decrypt("legacy-plain") == "legacy-plain"          # مقدارِ قدیمی بی‌تغییر
    assert SB.decrypt(None) is None


def test_wrong_key_returns_raw_value_without_crashing(monkeypatch):
    enc = SB.encrypt("secret")
    monkeypatch.setenv("BOTSAZ_SECRET_KEY", "another-key")
    assert SB.decrypt(enc) == enc                                  # خوانده نمی‌شود ولی خطا هم نمی‌دهد


def test_type_decorator_flag_length_and_idempotence(monkeypatch):
    t = SB.EncryptedStr(200)
    assert t.process_bind_param("abc", None) == "abc"              # پیش‌فرض: خاموش
    monkeypatch.setenv("BOTSAZ_ENCRYPT_SECRETS", "1")
    enc = t.process_bind_param("abc", None)
    assert enc.startswith("enc1:") and t.process_result_value(enc, None) == "abc"
    assert t.process_bind_param(enc, None) == enc                  # دوباره رمز نمی‌شود
    long_pw = "x" * 150
    assert t.process_bind_param(long_pw, None) == long_pw          # از طولِ ستون بیشتر ⇒ متنِ ساده
    assert t.process_bind_param(None, None) is None


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_vpn_panel_roundtrip_through_database(db, monkeypatch):
    monkeypatch.setenv("BOTSAZ_ENCRYPT_SECRETS", "1")
    async with db() as s:
        s.add(models.VpnPanel(id=1, bot_id=1, panel_key="marzban", base_url="https://p", username="u", password="TopSecret1",
                              api_key="KEY123", enabled=True))
        await s.commit()
    async with db() as s:
        raw = (await s.execute(text("SELECT password, api_key FROM vpn_panels"))).one()
        p = await s.get(models.VpnPanel, 1)
    assert raw[0].startswith("enc1:") and raw[1].startswith("enc1:") and "TopSecret1" not in raw[0]
    assert p.password == "TopSecret1" and p.api_key == "KEY123"      # خواندنِ شفاف
    # ردیفِ قدیمیِ متنِ ساده هم درست خوانده می‌شود
    async with db() as s:
        await s.execute(text("UPDATE vpn_panels SET password='plain-old'"))
        await s.commit()
        p = await s.get(models.VpnPanel, 1)
    assert p.password == "plain-old"
