"""PATCH_GATEWAY_SERVER_IP_V1 — زرین‌پال موقع ساخت درگاه «آی‌پی سرور» می‌خواهد؛ ربات باید آی‌پی را نشان بدهد
و راهنمای داخل ربات (که می‌گفت دامنهٔ مرکزی پیش‌فرض است) با رفتار واقعی یکی باشد."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_zarinpal
import tenant
from tenant.gateways import custom_domain as CD
from tenant.gateways import server_ip as SIP
from tenant.gateways import zarinpal_wallet as ZW

IP = "188.132.174.150"
IP2 = "8.8.8.8"


# ───────────────────────── منطق خالص ─────────────────────────
def test_clean_ips_accepts_commas_spaces_persian_comma_and_dedupes():
    assert SIP.clean_ips("1.1.1.1, 8.8.8.8،1.1.1.1;9.9.9.9\n4.4.4.4 bad 999.1.1.1") == ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.4.4.4"]
    assert SIP.clean_ips("") == [] and SIP.clean_ips(None) == []
    assert len(SIP.clean_ips(",".join(f"1.1.1.{i}" for i in range(1, 9)))) == 5          # سقف ۵ تای زرین‌پال
    assert SIP.clean_ips("10.0.0.1,192.168.1.5,127.0.0.1,8.8.8.8", public_only=True) == ["8.8.8.8"]
    assert SIP.clean_ips("10.0.0.1") == ["10.0.0.1"]                                      # override صریح مالک پلتفرم قبول است


def test_is_ip_error_matches_zarinpal_wording_only():
    assert SIP.is_ip_error("Terminal is not valid, please check merchant_id or ip address.")
    assert SIP.is_ip_error("ای پی و يا مرچنت كد پذيرنده صحيح نيست")
    assert SIP.is_ip_error("آی‌پی سرور ثبت نشده")
    for ok in ("", None, "Session is not valid", "دامنهٔ callback با دامنهٔ ثبت‌شده مطابقت ندارد",
               "zip code description", "amount must be bigger"):
        assert not SIP.is_ip_error(ok), ok


def test_ip_line_variants():
    one = SIP.ip_line([IP], "detected")
    assert f"<code>{IP}</code>" in one and "زرین‌پال" in one and "حدسی" not in one
    two = SIP.ip_line([IP, IP2], "override")
    assert f"<code>{IP}</code>، <code>{IP2}</code>" in two
    assert "حدسی" in SIP.ip_line([IP], "dns")
    assert "خوانده نشد" in SIP.ip_line([], "")


# ───────────────────────── منبع آی‌پی ─────────────────────────
@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, SIP, ZW, CD):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_STAFF_PERMS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    SIP._CACHE.update(until=0.0, v=([], ""))
    monkeypatch.delenv("PAYMENT_SERVER_IPS", raising=False)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="shop_bot", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    yield maker
    SIP._CACHE.update(until=0.0, v=([], ""))
    await engine.dispose()


async def _set(maker, bot_id, key, value):
    async with maker() as s:
        s.add(models.Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


@pytest.mark.asyncio
async def test_precedence_setting_then_env_then_detected_then_dns(db, monkeypatch):
    calls = {"detect": 0, "dns": 0}

    async def detect():
        calls["detect"] += 1
        return IP

    async def dns():
        calls["dns"] += 1
        return [IP2]
    monkeypatch.setattr(SIP, "_detect", detect)
    monkeypatch.setattr(SIP, "_dns_guess", dns)

    assert await SIP.get_server_ips(force=True) == ([IP], "detected")
    assert calls == {"detect": 1, "dns": 0}

    monkeypatch.setenv("PAYMENT_SERVER_IPS", "9.9.9.9")
    assert await SIP.get_server_ips(force=True) == (["9.9.9.9"], "override")
    await _set(db, 0, "payment_server_ips", "1.1.1.1، 4.4.4.4")            # Setting از env قوی‌تر است
    assert await SIP.get_server_ips(force=True) == (["1.1.1.1", "4.4.4.4"], "override")


@pytest.mark.asyncio
async def test_falls_back_to_dns_guess_then_to_nothing(db, monkeypatch):
    async def no_detect():
        return ""

    async def dns():
        return [IP2]
    monkeypatch.setattr(SIP, "_detect", no_detect)
    monkeypatch.setattr(SIP, "_dns_guess", dns)
    assert await SIP.get_server_ips(force=True) == ([IP2], "dns")

    async def nothing():
        return []
    monkeypatch.setattr(SIP, "_dns_guess", nothing)
    assert await SIP.get_server_ips(force=True) == ([], "")


@pytest.mark.asyncio
async def test_result_is_cached_and_failure_is_retried_sooner(db, monkeypatch):
    n = {"c": 0}

    async def detect():
        n["c"] += 1
        return IP
    monkeypatch.setattr(SIP, "_detect", detect)
    await SIP.get_server_ips(force=True)
    await SIP.get_server_ips()
    await SIP.get_server_ips()
    assert n["c"] == 1                                                    # صفحه را هر بار کند نمی‌کند

    async def nobody():
        return ""

    async def noguess():
        return []
    SIP._CACHE.update(until=0.0)
    monkeypatch.setattr(SIP, "_detect", nobody)
    monkeypatch.setattr(SIP, "_dns_guess", noguess)
    await SIP.get_server_ips()
    ttl = SIP._CACHE["until"] - __import__("time").monotonic()
    assert 0 < ttl <= SIP._FAIL_TTL + 1                                   # شکست فقط ۱ دقیقه کش می‌شود


@pytest.mark.asyncio
async def test_slow_echo_services_do_not_hang_the_settings_page(db, monkeypatch):
    import asyncio

    async def hang():
        await asyncio.sleep(60)
        return IP
    monkeypatch.setattr(SIP, "_detect", hang)
    monkeypatch.setattr(asyncio, "wait_for", _fast_wait_for(asyncio.wait_for))

    async def noguess():
        return []
    monkeypatch.setattr(SIP, "_dns_guess", noguess)
    assert await SIP.get_server_ips(force=True) == ([], "")


def _fast_wait_for(real):
    async def wf(aw, timeout=None):
        return await real(aw, timeout=0.05 if timeout == 6 else timeout)
    return wf


@pytest.mark.asyncio
async def test_detect_uses_ipv4_only_echo_and_rejects_private_answers(monkeypatch):
    seen = {}

    class FakeResp:
        def __init__(self, t):
            self._t = t

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return self._t

    answers = {"https://api.ipify.org": "10.0.0.7\n", "https://ipv4.icanhazip.com": "garbage", "https://ifconfig.me/ip": IP + "\n"}

    class FakeSession:
        def __init__(self, connector=None, timeout=None):
            seen["family"] = getattr(connector, "_family", None) if connector is not None else None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url):
            seen.setdefault("urls", []).append(url)
            return FakeResp(answers[url])

    import socket
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    assert await SIP._detect() == IP                                       # خصوصی و مزخرف رد شد، سومی پذیرفته شد
    assert seen["urls"] == list(SIP._ECHO_URLS)
    assert seen["family"] == socket.AF_INET                                # فقط IPv4: همان آی‌پی که ثبت می‌شود


# ───────────────────────── صفحه‌های زرین‌پال ─────────────────────────
class FakeMsg:
    def __init__(self, log):
        self.log = log

    async def answer(self, text, reply_markup=None, **kw):
        self.log.append(("answer", text, reply_markup))
        return FakeMsg(self.log)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.log.append(("edit", text, reply_markup))
        return self


class FakeCb:
    def __init__(self, log, data="x"):
        self.data, self.message, self.from_user = data, FakeMsg(log), SimpleNamespace(id=1)
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


@pytest_asyncio.fixture
async def zp(db, monkeypatch):
    for k, v in (("zarinpal_enabled", "1"), ("zarinpal_merchant_id", "m" * 36),
                 ("zarinpal_webhook_base", "https://pay.example.ir")):
        await _set(db, 1, k, v)
    await _set(db, 0, "payment_server_ips", IP)
    row = SimpleNamespace(id=1, owner_id=1, username="shop_bot")

    async def owner_ok(cb, bot):
        return row
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    return SimpleNamespace(log=[], row=row)


def _all_text(log):
    return "\n".join(t for _k, t, _kb in log)


@pytest.mark.asyncio
async def test_settings_screen_shows_the_server_ip_and_a_domain_button(zp):
    text, kb = await ZW.zarinpal_admin_text(1)
    assert f"<code>{IP}</code>" in text and "IP سرور" in text
    assert "cdom:menu:zarinpal" in [b.callback_data for r in kb.inline_keyboard for b in r]
    assert CD._cdom_origin_from_cb("cdom:menu:zarinpal") == "zarinpal"
    assert CD._cdom_back_target("zarinpal") == "set:zarinpal"          # «بازگشت» به همان صفحهٔ زرین‌پال برمی‌گردد
    assert CD._cdom_origin_from_cb("cdom:check:zarinpal") == "zarinpal"
    assert CD._cdom_origin_from_cb("cdom:menu") == "crypto"            # رفتار قبلی دست‌نخورده


@pytest.mark.asyncio
async def test_guide_no_longer_claims_a_default_central_domain_and_lists_the_ip(zp):
    cb = FakeCb(zp.log, "zpt:guide")
    await ZW.zarinpal_admin_guide(cb, object())
    text = _all_text(zp.log)
    assert "پیش‌فرض همان دامنهٔ مرکزی" not in text and "نیازی به تنظیم ندارد" not in text
    assert f"<code>{IP}</code>" in text and "آی‌پی سرور" in text and "دامنهٔ اختصاصی ربات" in text
    assert "Terminal is not valid" in text and "سرور شخصی خودت ممکن نیست" in text


@pytest.mark.asyncio
async def test_failed_test_explains_ip_errors_and_prints_the_ip(zp, monkeypatch):
    async def rp(merchant, amount, desc, cb_url, **kw):
        return False, "Terminal is not valid, please check merchant_id or ip address."
    monkeypatch.setattr(platform_zarinpal, "request_payment", rp)
    cb = FakeCb(zp.log, "zpt:test")
    await ZW.zarinpal_admin_test(cb, object())
    text = _all_text(zp.log)
    assert "تست اتصال زرین‌پال ناموفق بود" in text
    assert "آی‌پی سرور یا مرچنت‌ایدی" in text and f"<code>{IP}</code>" in text
    assert "Terminal is not valid" in text


@pytest.mark.asyncio
async def test_failed_test_escapes_html_and_keeps_the_domain_hint(zp, monkeypatch):
    async def rp(*a, **k):
        return False, "callback domain <b>mismatch</b> دامنه"
    monkeypatch.setattr(platform_zarinpal, "request_payment", rp)
    await ZW.zarinpal_admin_test(FakeCb(zp.log, "zpt:test"), object())
    text = _all_text(zp.log)
    assert "&lt;b&gt;mismatch&lt;/b&gt;" in text and "<b>mismatch</b>" not in text    # parse_mode=HTML نمی‌شکند
    assert "دامنهٔ callback فعلی" in text and "expresspanel.ir" not in text
    assert "آی‌پی سرور یا مرچنت‌ایدی" not in text                                       # خطای دامنه ≠ خطای IP
    assert f"<code>{IP}</code>" in text                                                # ولی IP همیشه چاپ می‌شود


@pytest.mark.asyncio
async def test_screen_still_renders_when_ip_cannot_be_found(zp, monkeypatch, db):
    async with db() as s:                                                # override را بردار
        from sqlalchemy import delete
        await s.execute(delete(models.Setting).where(models.Setting.key == "payment_server_ips"))
        await s.commit()

    async def none():
        return ""

    async def noguess():
        return []
    monkeypatch.setattr(SIP, "_detect", none)
    monkeypatch.setattr(SIP, "_dns_guess", noguess)
    SIP._CACHE.update(until=0.0)
    text, _kb = await ZW.zarinpal_admin_text(1)
    assert "خوانده نشد" in text and "درگاه زرین‌پال ربات" in text
