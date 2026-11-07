"""
تست‌هایِ باگِ «پرداخت تأیید شد ولی کیف‌پول شارژ نشد» (PATCH_PAYER_IDENTITY_V1).

ریشه: در دکمه‌هایِ مبلغِ ثابتِ زرین‌پال/زیبال/حساب‌پی، `cb.message` (= پیامِ خودِ ربات، from_user = ربات)
به‌جایِ کسی که دکمه را زده به تابعِ ساختِ فاکتور داده می‌شد؛ فاکتور و شارژ به یک «کاربرِ ساختگیِ ربات»
(نام/یوزرنیم/آیدیِ خودِ ربات) می‌رفت و مشتری هیچ‌چیز نمی‌گرفت.

این تست‌ها هندلرهایِ *واقعیِ* دکمه را با همان شکلِ دقیقِ آپدیتِ تلگرام اجرا می‌کنند:
  cb.from_user = مشتری (is_bot=False)،  cb.message.from_user = ربات (is_bot=True).
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.gateways import payer_identity as pid

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOT_TG_ID = 8354597347
CUSTOMER = 555001


# ───────────────────────── شبیه‌سازیِ آپدیتِ تلگرام ─────────────────────────

def tg_user(uid, *, is_bot=False, name="Ali", username=None):
    return SimpleNamespace(id=uid, is_bot=is_bot, full_name=name, first_name=name, username=username)


class FakeMessage:
    """پیامی که *ربات* فرستاده است (دکمهٔ اینلاین به آن چسبیده)."""

    def __init__(self, from_user):
        self.from_user = from_user
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text="", **kw):
        self.sent.append((text, kw))

    async def edit_text(self, text="", **kw):
        self.sent.append((text, kw))


class FakeCb:
    def __init__(self, data, presser):
        self.data = data
        self.from_user = presser
        self.message = FakeMessage(tg_user(BOT_TG_ID, is_bot=True, name="Express | خدمات مجازی",
                                           username="ExpressPanel_Bot"))
        self.alerts: list[str] = []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


class FakeState:
    def __init__(self):
        self.cleared = 0

    async def clear(self):
        self.cleared += 1

    async def set_state(self, *a, **k): ...
    async def get_data(self): return {}
    async def update_data(self, **k): ...


class FakeAiogramBot:
    """چیزی که get_bot_row را تغذیه می‌کند؛ get_bot_row جایگزین می‌شود."""
    token = f"{BOT_TG_ID}:AAA"


# ───────────────────────── پایگاه‌دادهٔ آزمایشی ─────────────────────────

@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant
    from tenant.gateways import zarinpal_wallet, zibal_wallet, hesabpay
    for mod in (config, tenant, zarinpal_wallet, zibal_wallet, hesabpay):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._OWN_TG_ID_CACHE.clear()
    async with maker() as s:
        bot = models.Bot(owner_id=1, token=f"{BOT_TG_ID}:AAA", username="ExpressPanel_Bot",
                         field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        s.add(models.BotUser(bot_id=bot.id, telegram_id=CUSTOMER, full_name="Meli", balance=0))
        await s.commit()
        row = SimpleNamespace(id=bot.id, username="ExpressPanel_Bot", token=bot.token, owner_id=1,
                              field="virtual_service", plan="pro", status="active")

    async def fake_get_bot_row(_bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", fake_get_bot_row)
    yield maker, row
    tenant._OWN_TG_ID_CACHE.clear()
    await engine.dispose()


async def _users(maker, bot_id):
    async with maker() as s:
        return (await s.scalars(select(models.BotUser).where(models.BotUser.bot_id == bot_id))).all()


async def _orders(maker, bot_id):
    async with maker() as s:
        return (await s.scalars(select(models.Order).where(models.Order.bot_id == bot_id))).all()


def _patch_zarinpal(monkeypatch, captured):
    from tenant.gateways import zarinpal_wallet as zw

    async def request_payment(merchant, amount, description, callback_url):
        captured["description"] = description
        captured["amount"] = amount
        return True, "A00000000000000000000000000012345678"

    async def configured(_bot_id): return True
    async def setting(_bot_id, key, default=""): return "merchant-uuid" if "merchant" in key else default
    async def callback_base(_bot_id): return "https://example.test"
    async def startpay(_bot_id, authority): return f"https://pay.test/{authority}"
    monkeypatch.setattr(zw.platform_zarinpal, "request_payment", request_payment)
    monkeypatch.setattr(zw, "_configured", configured)
    monkeypatch.setattr(zw, "_setting", setting)
    monkeypatch.setattr(zw, "_callback_base", callback_base)
    monkeypatch.setattr(zw, "startpay_url_for_bot", startpay)


def _patch_zibal(monkeypatch, captured):
    from tenant.gateways import zibal_wallet as zb

    async def request_payment(merchant, amount, description, callback_url):
        captured["description"] = description
        return True, "trackId-123"

    async def configured(_bot_id): return True
    async def setting(_bot_id, key, default=""): return "zibal-merchant" if "merchant" in key else default
    async def callback_base(_bot_id): return "https://example.test"
    monkeypatch.setattr(zb.platform_zibal, "request_payment", request_payment)
    monkeypatch.setattr(zb, "_configured", configured)
    monkeypatch.setattr(zb, "_setting", setting)
    monkeypatch.setattr(zb, "_callback_base", callback_base)
    if hasattr(zb, "startpay_url_for_bot"):
        async def startpay(*a, **k): return "https://pay.test/zb"
        monkeypatch.setattr(zb, "startpay_url_for_bot", startpay)


# ───────────────────────── خودِ helper ─────────────────────────

def test_resolve_payer_uses_explicit_user_over_message_sender():
    msg = SimpleNamespace(from_user=tg_user(BOT_TG_ID, is_bot=True))
    real = tg_user(CUSTOMER)
    assert pid.resolve_payer(msg, real) is real


def test_resolve_payer_falls_back_to_message_user_for_real_user_messages():
    real = tg_user(CUSTOMER)
    assert pid.resolve_payer(SimpleNamespace(from_user=real)) is real


@pytest.mark.parametrize("message,explicit", [
    (SimpleNamespace(from_user=tg_user(BOT_TG_ID, is_bot=True)), None),   # همان باگ: cb.message بدونِ from_user
    (SimpleNamespace(from_user=None), None),
    (SimpleNamespace(from_user=tg_user(CUSTOMER)), tg_user(BOT_TG_ID, is_bot=True)),
    (SimpleNamespace(), None),
])
def test_resolve_payer_refuses_bots_and_unknowns(message, explicit):
    with pytest.raises(pid.PayerIdentityError):
        pid.resolve_payer(message, explicit)


# ───────────────────────── زرین‌پال ─────────────────────────

@pytest.mark.asyncio
async def test_zarinpal_fixed_amount_credits_the_customer_not_the_bot(db, monkeypatch):
    from tenant.gateways import zarinpal_wallet as zw
    maker, row = db
    captured = {}
    _patch_zarinpal(monkeypatch, captured)

    cb = FakeCb("wzp:amt:100000", tg_user(CUSTOMER, name="Meli"))
    await zw.zarinpal_fixed_amount(cb, FakeAiogramBot(), FakeState())

    users = {u.telegram_id: u for u in await _users(maker, row.id)}
    assert BOT_TG_ID not in users, "کاربرِ ساختگیِ ربات ساخته شده — همان باگِ گزارش‌شده"
    orders = await _orders(maker, row.id)
    assert len(orders) == 1 and orders[0].pay_method == "zarinpal_wallet"
    assert orders[0].user_id == users[CUSTOMER].id                 # فاکتور به نامِ مشتری
    assert int(orders[0].amount) == 100_000
    assert cb.message.sent and "فاکتور" in cb.message.sent[0][0]   # پیام به همان چت


@pytest.mark.asyncio
async def test_zarinpal_description_contains_the_numeric_user_id(db, monkeypatch):
    """درخواستِ ادمین: شناسهٔ عددیِ کاربر در توضیحاتِ پنلِ زرین‌پال باشد."""
    from tenant.gateways import zarinpal_wallet as zw
    maker, row = db
    captured = {}
    _patch_zarinpal(monkeypatch, captured)
    await zw.zarinpal_fixed_amount(FakeCb("wzp:amt:50000", tg_user(CUSTOMER)), FakeAiogramBot(), FakeState())
    assert str(CUSTOMER) in captured["description"]
    assert "@ExpressPanel_Bot" in captured["description"]


@pytest.mark.asyncio
async def test_zarinpal_custom_amount_path_still_works(db, monkeypatch):
    """مسیرِ «مبلغ دلخواه»: پیامِ خودِ مشتری است؛ نباید خراب شده باشد."""
    from tenant.gateways import zarinpal_wallet as zw
    maker, row = db
    _patch_zarinpal(monkeypatch, {})
    msg = FakeMessage(tg_user(CUSTOMER))
    msg.text = "۳۰۰۰۰"
    await zw.zarinpal_custom_amount(msg, FakeState(), FakeAiogramBot())
    orders = await _orders(maker, row.id)
    users = {u.telegram_id: u for u in await _users(maker, row.id)}
    assert len(orders) == 1 and orders[0].user_id == users[CUSTOMER].id and int(orders[0].amount) == 30_000
    assert BOT_TG_ID not in users


@pytest.mark.asyncio
async def test_zarinpal_refuses_when_only_the_bots_message_is_available(db, monkeypatch):
    """اگر روزی دوباره کسی cb.message را بدونِ from_user بدهد: هیچ فاکتوری ساخته نمی‌شود."""
    from tenant.gateways import zarinpal_wallet as zw
    maker, row = db
    captured = {}
    _patch_zarinpal(monkeypatch, captured)
    bot_msg = FakeMessage(tg_user(BOT_TG_ID, is_bot=True))
    await zw._create_payment(bot_msg, FakeAiogramBot(), 100_000)          # بدونِ from_user
    assert captured == {}                                                  # حتی به درگاه هم درخواست نرفت
    assert await _orders(maker, row.id) == []
    assert BOT_TG_ID not in {u.telegram_id for u in await _users(maker, row.id)}
    assert bot_msg.sent and "شناسایی نشد" in bot_msg.sent[0][0]


@pytest.mark.asyncio
async def test_customer_balance_is_really_credited_after_settlement(db, monkeypatch):
    """سرتاسر: دکمه ⇒ فاکتور ⇒ تأییدِ درگاه ⇒ *موجودیِ همان مشتری* بالا می‌رود (نه حسابِ ربات)."""
    from tenant.gateways import zarinpal_wallet as zw
    maker, row = db
    _patch_zarinpal(monkeypatch, {})
    async with maker() as s0:                        # _settle_zarinpal_order مرچنت را مستقیم از جدول می‌خواند
        s0.add(models.Setting(bot_id=row.id, key="zarinpal_merchant_id", value="merchant-uuid"))
        await s0.commit()
    await zw.zarinpal_fixed_amount(FakeCb("wzp:amt:100000", tg_user(CUSTOMER)), FakeAiogramBot(), FakeState())
    (order,) = await _orders(maker, row.id)

    async def verify_payment(merchant, amount, authority):
        return True, "92779779201"
    monkeypatch.setattr(zw.platform_zarinpal, "verify_payment", verify_payment)
    monkeypatch.setattr(zw.platform_zarinpal, "pop_card_pan", lambda a: "4598******589210")
    async def fake_after_charge(*a, **k): return None
    import tenant.shared.kyc as kyc
    monkeypatch.setattr(kyc, "after_charge", fake_after_charge)

    status, _msg, amount, bot_id, telegram_id, new_balance, ref = await zw._settle_zarinpal_order(order.id)
    assert status == "credited" and telegram_id == CUSTOMER and amount == 100_000
    users = {u.telegram_id: u for u in await _users(maker, row.id)}
    assert float(users[CUSTOMER].balance) == 100_000
    assert BOT_TG_ID not in users


# ───────────────────────── زیبال ─────────────────────────

@pytest.mark.asyncio
async def test_zibal_fixed_amount_credits_the_customer_not_the_bot(db, monkeypatch):
    from tenant.gateways import zibal_wallet as zb
    maker, row = db
    captured = {}
    _patch_zibal(monkeypatch, captured)
    cb = FakeCb("wzb:amt:100000", tg_user(CUSTOMER))
    await zb.zibal_fixed_amount(cb, FakeAiogramBot(), FakeState())
    users = {u.telegram_id: u for u in await _users(maker, row.id)}
    assert BOT_TG_ID not in users
    orders = await _orders(maker, row.id)
    assert len(orders) == 1 and orders[0].user_id == users[CUSTOMER].id
    assert str(CUSTOMER) in captured["description"]


@pytest.mark.asyncio
async def test_zibal_refuses_bot_identity(db, monkeypatch):
    from tenant.gateways import zibal_wallet as zb
    maker, row = db
    captured = {}
    _patch_zibal(monkeypatch, captured)
    await zb._create_payment(FakeMessage(tg_user(BOT_TG_ID, is_bot=True)), FakeAiogramBot(), 100_000)
    assert captured == {} and await _orders(maker, row.id) == []


# ───────────────────────── حساب‌پی ─────────────────────────

def _patch_hesabpay(monkeypatch):
    from decimal import Decimal
    from tenant.gateways import hesabpay as hp

    async def available(_bot_id): return True
    async def rate(_bot_id): return Decimal("2000")
    async def setting(_bot_id, key, default=""): return "sandbox"
    async def create_session(_bot_id, charge):
        raise RuntimeError("stop-here")       # ردِ فاکتور را همین‌جا می‌گیریم؛ ردیفِ Charge از قبل ساخته شده
    monkeypatch.setattr(hp, "is_available", available)
    monkeypatch.setattr(hp, "_rate", rate)
    monkeypatch.setattr(hp, "_setting", setting)
    monkeypatch.setattr(hp, "_create_session", create_session)
    return hp


@pytest.mark.asyncio
async def test_hesabpay_fixed_amount_records_the_customer(db, monkeypatch):
    maker, row = db
    hp = _patch_hesabpay(monkeypatch)
    cb = FakeCb("hb:amount:100000", tg_user(CUSTOMER))
    await hp.hesabpay_amount(cb, FakeAiogramBot(), FakeState())
    async with maker() as s:
        charges = (await s.scalars(select(models.HesabPayCharge))).all()
    assert len(charges) == 1
    assert charges[0].user_telegram_id == CUSTOMER, "شارژ به نامِ خودِ ربات ثبت شد"
    assert BOT_TG_ID not in {u.telegram_id for u in await _users(maker, row.id)}


@pytest.mark.asyncio
async def test_hesabpay_refuses_bot_identity(db, monkeypatch):
    maker, row = db
    hp = _patch_hesabpay(monkeypatch)
    msg = FakeMessage(tg_user(BOT_TG_ID, is_bot=True))
    await hp._start_amount(msg, FakeAiogramBot(), FakeState(), 100_000)
    async with maker() as s:
        assert (await s.scalars(select(models.HesabPayCharge))).all() == []
    assert msg.sent and "شناسایی نشد" in msg.sent[0][0]


# ───────────────────────── نگهبانِ سراسری get_or_create_user ─────────────────────────

@pytest.mark.asyncio
async def test_get_or_create_user_refuses_the_bots_own_id(db):
    import tenant
    maker, row = db
    with pytest.raises(pid.PayerIdentityError):
        await tenant.get_or_create_user(row.id, BOT_TG_ID, "Express | خدمات مجازی", "ExpressPanel_Bot")
    assert BOT_TG_ID not in {u.telegram_id for u in await _users(maker, row.id)}


@pytest.mark.asyncio
async def test_get_or_create_user_still_works_for_real_users(db):
    import tenant
    maker, row = db
    u = await tenant.get_or_create_user(row.id, 999_001, "New Guy", "newguy")
    assert u.telegram_id == 999_001
    again = await tenant.get_or_create_user(row.id, CUSTOMER, "Meli", "")
    assert again.telegram_id == CUSTOMER


@pytest.mark.asyncio
async def test_guard_does_not_break_when_the_bot_row_is_missing(db):
    import tenant
    maker, row = db
    u = await tenant.get_or_create_user(row.id + 999, 4242, "X", "")   # رباتی که وجود ندارد: نگهبان بی‌اثر
    assert u is not None


# ───────────────────────── رگرسیونِ ایستا: الگوی باگ دوباره اضافه نشود ─────────────────────────

# فراخوانی‌هایی که cb.message را به تابعی می‌دهند که .from_user می‌خواند و *هنوز بررسی/رفع نشده‌اند*
# (گزارشِ اسکن؛ VPN نمایشِ پلن‌ها و بازارچهٔ پلتفرم). هر مورد جدید باید یا رفع شود یا این‌جا با دلیل بیاید.
_KNOWN_UNFIXED = {
    ("tenant/bots/vpn/vpn_user.py", "show_bundle_all_services"),
    ("tenant/bots/vpn/vpn_user.py", "show_vpn_plans"),
    ("tenant/bots/vpn/vpn_user.py", "show_bundle_hub"),
    ("platform_market.py", "_big_charge_gate"),
}


def _scan_cb_message_into_from_user_functions():
    CB = {"cb", "call", "query", "callback", "c", "cq"}
    defs = {}
    files = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and "__pycache__" not in p.parts
             and not p.name.startswith(("check_", "diag_")) and "backfill" not in p.name]
    trees = {}
    for p in files:
        try:
            t = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        trees[p] = t
        for n in ast.walk(t):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = [a.arg for a in n.args.args]
                uses = {sub.value.id for sub in ast.walk(n)
                        if isinstance(sub, ast.Attribute) and sub.attr == "from_user"
                        and isinstance(sub.value, ast.Name) and sub.value.id in params}
                if uses:
                    defs.setdefault(n.name, []).append((p, params, uses, n))
    hits = set()
    for p, t in trees.items():
        for n in ast.walk(t):
            if not isinstance(n, ast.Call):
                continue
            fname = n.func.id if isinstance(n.func, ast.Name) else (n.func.attr if isinstance(n.func, ast.Attribute) else None)
            for dp, params, uses, dn in defs.get(fname, []):
                def is_cbm(a):
                    return (isinstance(a, ast.Attribute) and a.attr == "message"
                            and isinstance(a.value, ast.Name) and a.value.id in CB)
                for i, a in enumerate(n.args):
                    if is_cbm(a) and i < len(params) and params[i] in uses:
                        # اگر تابع پارامترِ from_user/uid صریح می‌گیرد و فراخوانی آن را داده، مشکلی نیست
                        supplied = {kw.arg for kw in n.keywords}
                        if "from_user" in supplied and "from_user" in [x.arg for x in dn.args.args + dn.args.kwonlyargs]:
                            continue
                        hits.add((str(p.relative_to(ROOT)), fname))
    return hits


def test_no_new_call_passes_cb_message_into_a_from_user_reader():
    """اگر این تست شکست خورد: یک هندلرِ callback، پیامِ خودِ ربات را به تابعی داده که کاربر را از
    `message.from_user` می‌خواند (همان باگِ شارژِ کیف‌پول). `from_user=cb.from_user` را صریح بدهید."""
    unexpected = _scan_cb_message_into_from_user_functions() - _KNOWN_UNFIXED
    assert not unexpected, f"الگوی خطرناکِ جدید: {sorted(unexpected)}"


def test_the_three_wallet_gateways_no_longer_hit_the_pattern():
    hits = _scan_cb_message_into_from_user_functions()
    for f in ("tenant/gateways/zarinpal_wallet.py", "tenant/gateways/zibal_wallet.py",
              "tenant/gateways/hesabpay.py"):
        assert not [h for h in hits if h[0] == f], f
