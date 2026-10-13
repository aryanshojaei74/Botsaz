"""PATCH_ESCROW_V1 — واسطه‌گری (معامله با واسطه) در رباتِ خدماتِ مجازی."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.shared.escrow as ES


# ── منطقِ خالص ──
def test_fee_calc_all_payers():
    assert ES.calc_fee(1_000_000, 10, "buyer") == {"fee": 100_000, "buyer_pays": 1_100_000, "seller_gets": 1_000_000}
    assert ES.calc_fee(1_000_000, 10, "seller") == {"fee": 100_000, "buyer_pays": 1_000_000, "seller_gets": 900_000}
    assert ES.calc_fee(1_000_001, 10, "split") == {"fee": 100_000, "buyer_pays": 1_050_001, "seller_gets": 950_001}
    assert ES.calc_fee(500_000, 2.5, "buyer")["fee"] == 12_500


def test_parsers_and_roles_and_title():
    assert ES.parse_percent("۱۰٪") == 10 and ES.parse_percent("2.5") == 2.5 and ES.parse_percent("0") is None
    assert ES.parse_percent("51") is None and ES.parse_percent("x") is None
    assert ES.parse_amount("۱,۰۰۰,۰۰۰") == 1_000_000 and ES.parse_amount("-5") is None and ES.parse_amount("0") is None
    d = SimpleNamespace(initiator_role="buyer", initiator_tg=1, counterparty_tg=2, title="یوزرنیم @x", number=12)
    assert ES.roles(d) == (1, 2)
    d.initiator_role = "seller"
    assert ES.roles(d) == (2, 1)
    assert ES.group_title(d) == "یوزرنیم @x | معامله #12"


# ── محیطِ تست ──
class FakeProvider:
    def __init__(self):
        self.created = []

    async def create_group(self, title, about, bot_username, admins):
        self.created.append((title, about, bot_username, admins))
        return -1001234567890


class FakeBot:
    token = "9:T"

    def __init__(self):
        self.sent, self.links, self.pins = [], [], []

    async def get_me(self):
        return SimpleNamespace(username="escbot")

    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text, k.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.sent))

    async def create_chat_invite_link(self, chat, member_limit=None, name=None):
        self.links.append((chat, member_limit, name))
        return SimpleNamespace(invite_link=f"https://t.me/+one{len(self.links)}")

    async def pin_chat_message(self, chat, mid, **k):
        self.pins.append((chat, mid))


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, ES):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=500, token="9:T", username="escbot", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Ali", username="ali"))
        s.add(models.BotUser(id=2, bot_id=9, telegram_id=1002, full_name="Sara", username="sara"))
        await s.commit()
    for k, v in (("escrow_enabled", "1"), ("escrow_fee_percent", "10"), ("escrow_fee_payer", "buyer"),
                 ("escrow_min", "10000"), ("escrow_max", "0"), ("escrow_mediators", "@mediator1,777")):
        await tenant.set_setting(9, k, v)
    prov = FakeProvider()

    async def gp(bot_id):
        return prov
    monkeypatch.setattr(ES, "get_provider", gp)
    yield maker, prov
    await engine.dispose()


class Msg:
    def __init__(self, text="", uid=1001):
        self.text, self.from_user, self.out = text, SimpleNamespace(id=uid, username="u"), []

    async def answer(self, text, **k):
        self.out.append(text)


class Cb:
    def __init__(self, data, uid=1001, bot=None):
        self.data, self.from_user, self.out = data, SimpleNamespace(id=uid, username="u"), []
        outer = self

        class M:
            async def answer(self, text, **k):
                outer.out.append(text)

            async def edit_text(self, text, **k):
                outer.out.append(text)

            async def edit_reply_markup(self, **k):
                pass
        self.message = M()

    async def answer(self, *a, **k):
        pass


class St:
    def __init__(self):
        self.d, self.s = {}, None

    async def update_data(self, **k):
        self.d.update(k)

    async def get_data(self):
        return dict(self.d)

    async def set_state(self, s):
        self.s = s

    async def clear(self):
        self.d = {}
        self.s = None


async def _make_deal(bot, st, role="seller", amount="1,000,000"):
    await ES.esc_new(Cb("esc:new"), bot, st)
    await ES.esc_role(Cb(f"esc:role:{role}"), st)
    await ES.esc_title(Msg("یوزرنیم @cool"), st)
    await ES.esc_amount(Msg(amount), bot, st)
    m = Msg("@sara")
    await ES.esc_party(m, bot, st)
    return m


@pytest.mark.asyncio
async def test_full_deal_flow_creates_group_and_one_time_links(env):
    maker, prov = env
    bot, st = FakeBot(), St()
    m = await _make_deal(bot, st)
    assert "100,000" in m.out[0] and "1,100,000" in m.out[0] and "10" in m.out[0]      # نرخ و جمعِ خریدار
    await ES.esc_send(Cb("esc:send"), bot, st)
    async with maker() as s:
        d = await s.scalar(select(models.EscrowDeal))
    assert d.status == "pending" and d.number == 1 and d.fee_toman == 100_000 and d.initiator_role == "seller"
    assert bot.sent[0][0] == 1002 and "درخواستِ معامله" in bot.sent[0][1]            # درخواست برایِ طرفِ مقابل
    # رد از غیرِ طرفِ مقابل اثری ندارد
    await ES.esc_accept(Cb(f"esc:acc:{d.id}", uid=999), bot)
    async with maker() as s:
        assert (await s.get(models.EscrowDeal, d.id)).status == "pending"
    await ES.esc_accept(Cb(f"esc:acc:{d.id}", uid=1002), bot)
    async with maker() as s:
        d = await s.get(models.EscrowDeal, d.id)
    assert d.status == "group_created" and d.group_chat_id == -1001234567890
    assert prov.created[0][0] == "یوزرنیم @cool | معامله #1" and prov.created[0][3] == ["@mediator1", "777"]
    assert [x[1] for x in bot.links] == [1, 1]                                     # لینک‌هایِ یک‌بارمصرف
    dms = {c: (t, k) for c, t, k in [(x[0], x[1], x[2]) for x in bot.sent] if c in (1001, 1002) and "گروهِ معامله" in t}
    assert set(dms) == {1001, 1002}
    assert dms[1002][1].inline_keyboard[0][0].url.startswith("https://t.me/+one")  # خریدار=۱۰۰۲ (فروشنده ۱۰۰۱)
    assert dms[1001][1].inline_keyboard[0][0].url != dms[1002][1].inline_keyboard[0][0].url
    grp = [x for x in bot.sent if x[0] == -1001234567890][0][1]
    assert "1,100,000" in grp and "1,000,000" in grp and "Sara" in grp and "Ali" in grp
    assert bot.pins and bot.pins[0][0] == -1001234567890
    assert any(x[0] == 500 for x in bot.sent)                                      # به مالک خبر رسید


@pytest.mark.asyncio
async def test_reject_and_validation(env):
    maker, _p = env
    bot, st = FakeBot(), St()
    await ES.esc_new(Cb("esc:new"), bot, st)
    await ES.esc_role(Cb("esc:role:buyer"), st)
    await ES.esc_title(Msg("کالا"), st)
    m = Msg("5000")
    await ES.esc_amount(m, bot, st)
    assert "حداقل" in m.out[0]                                                     # زیرِ حداقل
    await ES.esc_amount(Msg("50000"), bot, st)
    m = Msg("@nobody")
    await ES.esc_party(m, bot, st)
    assert "پیدا نشد" in m.out[0]
    m = Msg("1001")
    await ES.esc_party(m, bot, st)
    assert "خودت" in m.out[0]
    await ES.esc_party(Msg("۱۰۰۲"), bot, st)
    await ES.esc_send(Cb("esc:send"), bot, st)
    async with maker() as s:
        did = (await s.scalar(select(models.EscrowDeal))).id
    await ES.esc_reject(Cb(f"esc:rej:{did}", uid=1002), bot)
    async with maker() as s:
        assert (await s.get(models.EscrowDeal, did)).status == "rejected"
    assert any(x[0] == 1001 and "رد کرد" in x[1] for x in bot.sent)


@pytest.mark.asyncio
async def test_failure_without_account_then_retry_and_close_permissions(env, monkeypatch):
    maker, prov = env

    async def none(bot_id):
        return None
    monkeypatch.setattr(ES, "get_provider", none)
    bot, st = FakeBot(), St()
    await _make_deal(bot, st, role="buyer")
    await ES.esc_send(Cb("esc:send"), bot, st)
    async with maker() as s:
        did = (await s.scalar(select(models.EscrowDeal))).id
    await ES.esc_accept(Cb(f"esc:acc:{did}", uid=1002), bot)
    async with maker() as s:
        d = await s.get(models.EscrowDeal, did)
    assert d.status == "failed" and "اکانتِ واسط" in d.note
    assert any(x[0] == 500 and "ناموفق" in x[1] for x in bot.sent)                 # مالک مطلع شد
    async def ok(bot_id):
        return prov
    monkeypatch.setattr(ES, "get_provider", ok)                                    # بعد از اتصالِ اکانت
    assert await ES.open_deal_group(bot, did)
    # پایانِ معامله: کاربرِ عادی نمی‌تواند، واسطِ ثبت‌شده/مالک می‌تواند
    cb = Cb(f"esc:done:{did}", uid=1001)
    await ES.esc_done(cb, bot)
    async with maker() as s:
        assert (await s.get(models.EscrowDeal, did)).status == "group_created"
    await ES.esc_done(Cb(f"esc:done:{did}", uid=500), bot)                          # مالک
    async with maker() as s:
        assert (await s.get(models.EscrowDeal, did)).status == "completed"


@pytest.mark.asyncio
async def test_admin_gate_blocks_non_virtual_service_and_toggle_needs_account(env):
    maker, _p = env
    async with maker() as s:
        s.add(models.Bot(id=10, owner_id=500, token="10:T", username="vpn", field="vpn_sales",
                         plan="pro", status="active"))
        await s.commit()
    alerts = []

    class C2(Cb):
        async def answer(self, text="", show_alert=False, **k):
            alerts.append(text)
    assert await ES._gate(C2("x", uid=500), SimpleNamespace(token="10:T")) is None     # ربات VPN
    assert alerts and "خدماتِ مجازی" in alerts[0]
    row = await ES._gate(C2("x", uid=500), SimpleNamespace(token="9:T"))
    assert row and row.id == 9
    alerts.clear()
    await tenant.set_setting(9, "escrow_enabled", "0")
    await ES.adm_toggle(C2("esc:adm:toggle", uid=500), SimpleNamespace(token="9:T"))   # بدونِ اکانت روشن نمی‌شود
    assert any("اکانتِ واسط" in a for a in alerts)


class FakeLogin:
    def __init__(self, twofa=False):
        self.twofa, self.closed, self.phone = twofa, False, None

    async def send_code(self, phone):
        self.phone = phone

    async def sign_in_code(self, code):
        assert code == "12345"
        return None if self.twofa else "S" * 60

    async def sign_in_password(self, pw):
        assert pw == "pass"
        return "T" * 60

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("twofa", [False, True])
async def test_in_bot_login_flow(env, monkeypatch, twofa):
    ES._LOGINS.clear()
    lg = FakeLogin(twofa)
    monkeypatch.setattr(ES, "make_login", lambda i, h: lg)

    async def who(self):
        return "Mediator"
    monkeypatch.setattr(ES.TelethonProvider, "whoami", who)
    bot, st = SimpleNamespace(token="9:T"), St()
    await ES.adm_acc_go(Cb("esc:adm:accgo:login", uid=500), bot, st)
    assert st.s == ES.EscAdm.api_id
    await ES.adm_api_id(Msg("12345", uid=500), bot, st)
    await ES.adm_api_hash(Msg("a" * 32, uid=500), bot, st)
    assert st.s == ES.EscAdm.phone
    await ES.adm_phone(Msg("۰۹۱۲۱۲۳۴۵۶۷", uid=500), bot, st)
    assert st.s == ES.EscAdm.code and lg.phone == "+09121234567"
    await ES.adm_code(Msg("1-2-3-4-5", uid=500), bot, st)
    if twofa:
        assert st.s == ES.EscAdm.password
        await ES.adm_password(Msg("pass", uid=500), bot, st)
    assert lg.closed and not ES._LOGINS
    assert await tenant.get_setting(9, "escrow_session", "") == ("T" if twofa else "S") * 60
    assert await tenant.get_setting(9, "escrow_api_id", "") == "12345"


@pytest.mark.asyncio
async def test_login_uses_server_default_api_and_expires(env, monkeypatch):
    ES._LOGINS.clear()
    monkeypatch.setenv("ESCROW_API_ID", "777")
    monkeypatch.setenv("ESCROW_API_HASH", "h" * 32)
    st, bot = St(), SimpleNamespace(token="9:T")
    await ES.adm_acc_go(Cb("esc:adm:accgo:login", uid=500), bot, st)
    assert st.s == ES.EscAdm.phone and st.d["esc_api_id"] == "777"
    # کدِ بدونِ ورودِ فعال → منقضی
    m = Msg("1-2-3-4-5", uid=500)
    await ES.adm_code(m, bot, st)
    assert any("زمانِ ورود" in o for o in m.out)


@pytest.mark.asyncio
async def test_party_ok_buttons_only_for_own_party_and_notify_when_both(env):
    maker, _p = env
    bot, st = FakeBot(), St()
    await _make_deal(bot, st, role="seller")                       # فروشنده=1001، خریدار=1002
    await ES.esc_send(Cb("esc:send"), bot, st)
    async with maker() as s:
        d = await s.scalar(select(models.EscrowDeal))
    await ES.esc_accept(Cb(f"esc:acc:{d.id}", uid=1002), bot)
    group_kb = [x for x in bot.sent if x[0] == -1001234567890][0][2]
    data = [b.callback_data for r in group_kb.inline_keyboard for b in r]
    assert f"esc:ok:{d.id}:b" in data and f"esc:ok:{d.id}:s" in data and f"esc:done:{d.id}" in data
    alerts = []

    class C3(Cb):
        async def answer(self, text="", show_alert=False, **k):
            alerts.append(text)
    buyer_id, seller_id = ES.roles(d)
    await ES.esc_ok(C3(f"esc:ok:{d.id}:b", uid=seller_id), bot)       # فروشنده دکمهٔ خریدار را می‌زند
    assert alerts and "خریدار" in alerts[-1]
    async with maker() as s:
        assert not (await s.get(models.EscrowDeal, d.id)).buyer_ok
    await ES.esc_ok(C3(f"esc:ok:{d.id}:b", uid=buyer_id), bot)
    await ES.esc_ok(C3(f"esc:ok:{d.id}:s", uid=seller_id), bot)
    async with maker() as s:
        d2 = await s.get(models.EscrowDeal, d.id)
        assert d2.buyer_ok and d2.seller_ok and d2.status == "group_created"   # فقط واسط می‌تواند ببندد
    assert any(x[0] == 500 and "هر دو طرف تأیید" in x[1] for x in bot.sent)
