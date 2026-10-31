"""PATCH_SMS_VERIFY_V1 — تأییدِ شماره با کدِ پیامکی + گیتِ پیش از پرداخت روی شارژها."""
import time
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import sms_verify as SV


def test_norm_mobile_forms():
    for t in ("09123456789", "۰۹۱۲۳۴۵۶۷۸۹", "+989123456789", "989123456789", "0098 912 345 6789", "0912-345-6789"):
        assert SV.norm_mobile(t) == "989123456789", t
    for t in ("9123456789", "0812345678", "123", "", None, "0912345678"):
        assert SV.norm_mobile(t) is None, t
    assert SV.local_mobile("989123456789") == "09123456789"


def test_rate_limits(monkeypatch):
    SV._SENDS.clear()
    SV._NUM_SENDS.clear()
    now = 1000.0
    assert SV.rate_ok(1, 7, "98911", now)[0]
    ok, why = SV.rate_ok(1, 7, "98911", now + 10)
    assert not ok and "ثانیه" in why                                   # فاصلهٔ ۶۰ثانیه
    assert SV.rate_ok(1, 7, "98911", now + 70)[0]
    assert SV.rate_ok(1, 7, "98911", now + 140)[0]
    ok, why = SV.rate_ok(1, 7, "98911", now + 210)
    assert not ok and "نیم ساعت" in why                                # ۳ ارسال در ۳۰ دقیقه
    SV._SENDS.clear()
    for i in range(5):
        assert SV.rate_ok(1, 100 + i, "98922", now + i * 100)[0]       # کاربرانِ مختلف، یک شماره
    ok, why = SV.rate_ok(1, 999, "98922", now + 900)
    assert not ok and "امروز" in why                                    # ۵ ارسال در روز برایِ هر شماره


@pytest.mark.asyncio
async def test_send_code_requests_per_provider():
    calls = []

    async def http_ok(method, url, **kw):
        calls.append((method, url, kw))
        return (200, {"return": {"status": 200}}) if "kavenegar" in url else (200, {"status": 1, "message": "ok"})
    kv = {"provider": "kavenegar", "key": "KEY", "template": "verify", "param": "CODE"}
    assert (await SV.send_code(kv, "09123456789", "12345", http=http_ok))[0] is True
    m, url, kw = calls[-1]
    assert m == "GET" and url == "https://api.kavenegar.com/v1/KEY/verify/lookup.json" and kw["params"] == {
        "receptor": "09123456789", "token": "12345", "template": "verify"}
    si = {"provider": "smsir", "key": "K2", "template": "100200", "param": "CODE"}
    assert (await SV.send_code(si, "09123456789", "54321", http=http_ok))[0] is True
    m, url, kw = calls[-1]
    assert m == "POST" and url.endswith("/v1/send/verify") and kw["headers"]["X-API-KEY"] == "K2"
    assert kw["json"] == {"mobile": "09123456789", "templateId": 100200, "parameters": [{"name": "CODE", "value": "54321"}]}

    async def http_bad(method, url, **kw):
        return 200, {"return": {"status": 411}}
    ok, err = await SV.send_code(kv, "0912", "1", http=http_bad)
    assert ok is False and err

    async def http_boom(method, url, **kw):
        raise RuntimeError("net")
    assert (await SV.send_code(kv, "0912", "1", http=http_boom))[0] is False


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, SV):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    SV._SENDS.clear()
    SV._NUM_SENDS.clear()
    row = NS(id=3, owner_id=500, field="vpn_sales", plan="pro", username="b")
    async with maker() as s:
        s.add(models.Bot(id=3, owner_id=500, token="3:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=3, telegram_id=1001, full_name="Ali", username="ali"))
        s.add(models.BotUser(id=2, bot_id=3, telegram_id=1002, full_name="Sara", username="sara",
                             phone_verified=True, phone_number="989121111111"))
        await s.commit()

    async def gbr(bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    yield maker, row
    await engine.dispose()


class _St:
    def __init__(self):
        self.d, self.s = {}, None

    async def set_state(self, s):
        self.s = s

    async def update_data(self, **k):
        self.d.update(k)

    async def get_data(self):
        return dict(self.d)

    async def clear(self):
        self.d, self.s = {}, None


class _Msg:
    def __init__(self, text, uid=1001):
        self.text, self.from_user, self.out = text, NS(id=uid, full_name="Ali", username="ali"), []

    async def answer(self, text, **k):
        self.out.append(text)


@pytest.mark.asyncio
async def test_sms_flow_code_check_limits_and_duplicate_number(env, monkeypatch):
    maker, row = env
    for k, v in (("phone_verify_method", "sms"), ("sms_provider", "kavenegar"), ("sms_api_key", "K"), ("sms_template", "t")):
        await tenant.set_setting(3, k, v)
    sent = []

    async def fake_send(c, mobile, code, http=None):
        sent.append((mobile, code))
        return True, ""
    monkeypatch.setattr(SV, "send_code", fake_send)
    done = []

    async def fake_ok(msg, bot, state, row_, phone):
        done.append(phone)
        return True
    monkeypatch.setattr(tenant, "_phone_verified_ok", fake_ok)
    st = _St()
    assert await SV.sms_mode(3) is True
    # شمارهٔ نامعتبر / شمارهٔ تاییدشدهٔ حسابِ دیگر
    m = _Msg("123")
    await SV.got_number(m, None, st)
    assert "معتبر نیست" in m.out[-1] and not sent
    m = _Msg("09121111111")
    await SV.got_number(m, None, st)
    assert "حسابِ دیگری" in m.out[-1] and not sent
    # شمارهٔ درست ⇒ کد فرستاده می‌شود
    m = _Msg("۰۹۱۲۳۴۵۶۷۸۹")
    await SV.got_number(m, None, st)
    assert sent[-1][0] == "09123456789" and st.s is SV.SmsVerifyFSM.code
    code = sent[-1][1]
    # کدِ غلط ⇒ شمارنده؛ کدِ درست ⇒ تأیید
    m = _Msg("00000" if code != "00000" else "11111")
    await SV.got_code(m, None, st)
    assert "درست نیست" in m.out[-1] and not done
    m = _Msg(code)
    await SV.got_code(m, None, st)
    assert done == ["989123456789"] and st.s is None
    # انقضا
    st2 = _St()
    SV._SENDS.clear()
    await SV.got_number(_Msg("09124444444"), None, st2)
    st2.d["sv_exp"] = time.time() - 1
    m = _Msg(sent[-1][1])
    await SV.got_code(m, None, st2)
    assert "منقضی" in m.out[-1] and len(done) == 1
    # ۵ تلاشِ غلط ⇒ بسته می‌شود
    st3 = _St()
    SV._SENDS.clear()
    await SV.got_number(_Msg("09125555555"), None, st3)
    real = sent[-1][1]
    wrong = "99999" if real != "99999" else "88888"
    for _ in range(SV.MAX_TRIES):
        m = _Msg(wrong)
        await SV.got_code(m, None, st3)
    assert "زیاد" in m.out[-1] and st3.s is None


@pytest.mark.asyncio
async def test_contact_mode_unchanged_and_sms_needs_config(env):
    assert await SV.sms_mode(3) is False                         # پیش‌فرض: مخاطبِ تلگرام
    await tenant.set_setting(3, "phone_verify_method", "sms")
    tenant._SETTING_CACHE.clear()
    assert await SV.sms_mode(3) is False                         # کلید/قالب نیست ⇒ مسیرِ قدیمی
    m = _Msg("x")
    assert await SV.sms_prompt(m, 3) is False and not m.out


class _Cb:
    def __init__(self, data, uid=1001):
        self.data, self.from_user, self.alerts = data, NS(id=uid), []
        self.message = NS(chat=NS(type="private"), out=[])

        async def ans(text, **k):
            self.message.out.append(text)
        self.message.answer = ans

    async def answer(self, text="", **k):
        self.alerts.append(text)


@pytest.mark.asyncio
async def test_pay_gate_middleware_covers_charge_start_only_when_enabled(env):
    maker, row = env
    reached = []

    async def handler(event, data):
        reached.append(event.data)
        return "ran"
    data = {"bot": NS(token="3:T")}
    for cbd in ("w:charge", "w:zarinpal", "wcamt:5000", "vsc:o:77", "w:apay:vpn:1"):
        # گیت خاموش (پیش‌فرض) ⇒ همه رد می‌شوند
        assert await tenant.tenant_pay_phone_gate_mw(handler, _Cb(cbd), data) == "ran"
    assert len(reached) == 5
    reached.clear()
    await tenant.set_setting(3, "phone_verify_pay", "on")
    tenant._SETTING_CACHE.clear()
    for cbd in ("w:charge", "w:zarinpal", "wcamt:5000", "vsc:o:77", "w:apay:vpn:1"):
        cb = _Cb(cbd)
        assert await tenant.tenant_pay_phone_gate_mw(handler, cb, data) is None
        assert cb.message.out and "شماره" in cb.message.out[-1] and "تایید" in cb.alerts[-1]
    assert not reached
    # callbackِ غیرِ پرداخت و مالک معاف‌اند
    assert await tenant.tenant_pay_phone_gate_mw(handler, _Cb("w:history"), data) == "ran"
    assert await tenant.tenant_pay_phone_gate_mw(handler, _Cb("w:charge", uid=500), data) == "ran"
    # کاربرِ تاییدشده رد می‌شود
    assert await tenant.tenant_pay_phone_gate_mw(handler, _Cb("w:charge", uid=1002), data) == "ran"


@pytest.mark.asyncio
async def test_owner_menu_view_reflects_config_and_masks_key(env):
    text, kb = await SV._menu_view(3)
    assert "ناقص" in text and "مخاطبِ تلگرام" in text
    for k, v in (("sms_api_key", "ABCDEFGHIJKLMN"), ("sms_template", "verify"), ("phone_verify_method", "sms")):
        await tenant.set_setting(3, k, v)
    tenant._SETTING_CACHE.clear()
    text, kb = await SV._menu_view(3)
    assert "آماده" in text and "ABCDEFGHIJKLMN" not in text and "ABC…LMN" in text
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {"psms:method", "psms:prov", "psms:key", "psms:tpl", "psms:test", "tadm:sec:finance"} <= set(cbs)
