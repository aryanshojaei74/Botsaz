"""PATCH_CHPOST_USERBOT_V1 — اتصالِ اکانت با «ورود با شماره و کد» (+ رمزِ دومرحله‌ای) داخلِ ربات."""
from types import SimpleNamespace as NS

import pytest

import chpost_userbot as UB
import platform_chpost as PC

ADMIN = 424242


def test_parse_code_and_phone():
    assert UB.parse_code("1-2-3-4-5") == "12345" and UB.parse_code("۱ ۲ ۳ ۴ ۵") == "12345" and UB.parse_code("abc") == ""
    assert UB.parse_phone("+98 912 123 4567") == "+989121234567"
    assert UB.parse_phone("09121234567") == "+989121234567"
    assert UB.parse_phone("۰۹۱۲۱۲۳۴۵۶۷") == "+989121234567"
    assert UB.parse_phone("00989121234567") == "+989121234567" and UB.parse_phone("") == ""


def test_friendly_errors_cover_common_failures():
    def E(name, **kw):
        return type(name, (Exception,), {})(), kw
    cases = {"PhoneNumberInvalidError": "نامعتبر", "PhoneCodeInvalidError": "اشتباه", "PhoneCodeExpiredError": "منقضی",
             "PasswordHashInvalidError": "رمزِ دومرحله", "ApiIdInvalidError": "api_id", "SendCodeUnavailableError": "فعلاً"}
    for name, word in cases.items():
        assert word in UB.friendly_error(type(name, (Exception,), {})())
    fw = type("FloodWaitError", (Exception,), {})()
    fw.seconds = 30
    assert "30" in UB.friendly_error(fw) and "تلگرام" in UB.friendly_error(RuntimeError("x"))


@pytest.mark.asyncio
async def test_login_class_flow_with_fake_client(monkeypatch):
    class Sess:
        def save(self):
            return "STRSESSION"

    class Client:
        def __init__(self):
            self.session, self.calls, self.need_pw = Sess(), [], False

        async def connect(self):
            self.calls.append("connect")

        async def disconnect(self):
            self.calls.append("disconnect")

        async def send_code_request(self, phone):
            return NS(phone_code_hash="HASH")

        async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
            if password is not None:
                if password != "pw":
                    raise type("PasswordHashInvalidError", (Exception,), {})()
                return
            if code == "00000":
                raise type("PhoneCodeInvalidError", (Exception,), {})()
            if self.need_pw:
                raise type("SessionPasswordNeededError", (Exception,), {})()

    c = Client()
    monkeypatch.setattr(UB, "_telethon", lambda: (None, lambda *a: c, lambda s=None: None, None))
    lg = UB.Login(1, "h")
    await lg.send_code("+989121234567")
    assert lg.hash == "HASH" and lg.phone == "+989121234567"
    with pytest.raises(UB.UserbotError) as e:
        await lg.sign_in_code("00000")
    assert "کد اشتباه" in str(e.value)
    assert await lg.sign_in_code("12345") == "STRSESSION"
    c.need_pw = True
    assert await lg.sign_in_code("12345") is None
    with pytest.raises(UB.UserbotError):
        await lg.sign_in_password("bad")
    assert await lg.sign_in_password("pw") == "STRSESSION"
    await lg.close()
    assert c.calls[-1] == "disconnect"


# ───────── جریانِ ربات ─────────
class Msg:
    def __init__(self, text=""):
        self.text, self.from_user, self.answers, self.deleted = text, NS(id=ADMIN), [], False

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def delete(self):
        self.deleted = True


class St:
    def __init__(self):
        self.d, self.state = {}, None

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **kw):
        self.d.update(kw)

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.d, self.state = {}, None


class Cb:
    def __init__(self):
        self.from_user, self.message, self.alerts = NS(id=ADMIN), Msg(), []

    async def answer(self, t=None, show_alert=False, **k):
        self.alerts.append(t)


class FakeLogin:
    instances = []

    def __init__(self, api_id, api_hash, need_pw=False):
        self.args, self.need_pw, self.closed, self.phone, self.codes = (api_id, api_hash), need_pw, False, None, []
        FakeLogin.instances.append(self)

    async def send_code(self, phone):
        if phone.endswith("0000"):
            raise UB.UserbotError("شمارهٔ تلفن نامعتبر است.")
        self.phone = phone

    async def sign_in_code(self, code):
        self.codes.append(code)
        if code == "11111":
            raise UB.UserbotError("کد اشتباه است. دوباره بفرست.")
        return None if self.need_pw else "SESSION-A"

    async def sign_in_password(self, pw):
        if pw != "secret":
            raise UB.UserbotError("رمزِ دومرحله‌ای اشتباه است. دوباره بفرست.")
        return "SESSION-B"

    async def close(self):
        self.closed = True


@pytest.fixture
def env(monkeypatch):
    store = {}
    FakeLogin.instances.clear()
    UB._LOGINS.clear()
    monkeypatch.setattr(PC, "PLATFORM_ADMIN_ID", ADMIN)

    async def gp(k, d=""):
        return store.get(k, d)

    async def sp(k, v):
        store[k] = v

    async def home(uid, show_admin=False):
        return None

    async def menu():
        return "m", None

    async def cancel_kb():
        return None
    monkeypatch.setattr(PC, "smart_cancel_kb", cancel_kb)

    async def val(api_id, api_hash, session):
        return f"Ali via {session}"
    monkeypatch.setattr(PC, "get_pset", gp)
    monkeypatch.setattr(PC, "set_pset", sp)
    monkeypatch.setattr(PC, "smart_home_kb", home)
    monkeypatch.setattr(PC, "_chpost_menu", menu)
    monkeypatch.setattr(UB, "validate_session", val)
    monkeypatch.setattr(UB, "default_api", lambda: ("", ""))
    import wallet_crypto
    monkeypatch.setattr(wallet_crypto, "encrypt_secret", lambda x: "ENC(" + x + ")")
    monkeypatch.setattr(wallet_crypto, "decrypt_secret", lambda x: x[4:-1])
    return store


def _use_fake(monkeypatch, need_pw=False):
    monkeypatch.setattr(UB, "make_login", lambda i, h: FakeLogin(i, h, need_pw))


@pytest.mark.asyncio
async def test_full_login_flow_without_2fa_and_asks_api_once(env, monkeypatch):
    _use_fake(monkeypatch)
    st, cb = St(), Cb()
    await PC.chpost_ua_login(cb, st)                                     # api ذخیره نیست ⇒ api_id می‌پرسد
    assert st.state == PC.ChpostFSM.ua_api_id and st.d["ua_flow"] == "login"
    await PC.chpost_ua_api_id(Msg("12345"), st)
    await PC.chpost_ua_api_hash(Msg("H" * 32), st)
    assert st.state == PC.ChpostFSM.ua_phone                              # login ⇒ شماره (نه StringSession)
    mp = Msg("+98 912 123 4567")
    await PC.chpost_ua_phone(mp, st)
    assert mp.deleted and st.state == PC.ChpostFSM.ua_code and FakeLogin.instances[0].args == (12345, "H" * 32)
    assert "1-2-3-4-5" in mp.answers[-1]                                  # راهنمایِ جداکنندهٔ رقم‌ها
    mc = Msg("1-2-3-4-5")
    await PC.chpost_ua_code(mc, st)
    assert mc.deleted and FakeLogin.instances[0].codes == ["12345"] and FakeLogin.instances[0].closed
    assert env["chpost_ua_session"] == "ENC(SESSION-A)" and env["chpost_ua_api_hash"] == "ENC(" + "H" * 32 + ")" and st.state is None
    assert any("اکانت متصل شد" in a and "Ali" in a for a in mc.answers)
    # دفعهٔ بعد: api ذخیره است ⇒ مستقیم شماره
    st2, cb2 = St(), Cb()
    await PC.chpost_ua_login(cb2, st2)
    assert st2.state == PC.ChpostFSM.ua_phone and st2.d["ua_api_id"] == "12345"


@pytest.mark.asyncio
async def test_login_with_two_step_password(env, monkeypatch):
    _use_fake(monkeypatch, need_pw=True)
    env["chpost_ua_api_id"], env["chpost_ua_api_hash"] = "7", "ENC(" + "K" * 32 + ")"
    st = St()
    await PC.chpost_ua_login(Cb(), st)
    await PC.chpost_ua_phone(Msg("+989121234567"), st)
    await PC.chpost_ua_code(Msg("22222"), st)
    assert st.state == PC.ChpostFSM.ua_pw and "chpost_ua_session" not in env
    bad = Msg("nope")
    await PC.chpost_ua_pw(bad, st)
    assert bad.deleted and "اشتباه" in bad.answers[-1] and st.state == PC.ChpostFSM.ua_pw      # دوباره می‌شود
    ok = Msg("secret")
    await PC.chpost_ua_pw(ok, st)
    assert ok.deleted and env["chpost_ua_session"] == "ENC(SESSION-B)" and st.state is None


@pytest.mark.asyncio
async def test_login_errors_cancel_expiry_and_guards(env, monkeypatch):
    _use_fake(monkeypatch)
    env["chpost_ua_api_id"], env["chpost_ua_api_hash"] = "7", "ENC(" + "K" * 32 + ")"
    st = St()
    await PC.chpost_ua_login(Cb(), st)
    bad = Msg("+989120000000")                                           # شمارهٔ نامعتبر ⇒ خطا و همان مرحله
    await PC.chpost_ua_phone(bad, st)
    assert "نامعتبر" in bad.answers[-1] and st.state == PC.ChpostFSM.ua_phone
    short = Msg("12")
    await PC.chpost_ua_phone(short, st)
    assert "نامعتبر" in short.answers[-1]
    await PC.chpost_ua_phone(Msg("+989121234567"), st)
    wrong = Msg("11111")                                                  # کدِ اشتباه ⇒ می‌ماند
    await PC.chpost_ua_code(wrong, st)
    assert "کد اشتباه" in wrong.answers[-1] and st.state == PC.ChpostFSM.ua_code
    nodigit = Msg("abc")
    await PC.chpost_ua_code(nodigit, st)
    assert "فقط رقم" in nodigit.answers[-1]
    # مهلت تمام شد
    UB._LOGINS.clear()
    late = Msg("12345")
    await PC.chpost_ua_code(late, st)
    assert "مهلتِ ورود" in late.answers[-1] and st.state is None
    # لغو در میانه ⇒ اتصال بسته می‌شود
    await PC.chpost_ua_login(Cb(), st)
    await PC.chpost_ua_phone(Msg("+989121234567"), st)
    inst = FakeLogin.instances[-1]
    from platform_chpost import BTN_CANCEL
    await PC.chpost_ua_code(Msg(BTN_CANCEL), st)
    assert inst.closed and st.state is None and "chpost_ua_session" not in env
    # غیرِ مدیر
    other = Msg("+989121234567")
    other.from_user = NS(id=1)
    st3 = St()
    await PC.chpost_ua_phone(other, st3)
    assert other.answers == []


def test_menu_offers_login_first_and_session_as_advanced():
    kb = PC._ua_menu_kb(False)
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert datas[0] == "chpost:ua_login" and "chpost:ua_connect" in datas and "chpost:ua_off" not in datas
    assert "chpost:ua_off" in [b.callback_data for r in PC._ua_menu_kb(True).inline_keyboard for b in r]
