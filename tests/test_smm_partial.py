"""
تست‌هایِ وضعیتِ «نیمه‌تمام» (Partial) در سفارش‌هایِ خدماتِ مجازی (PATCH_SMM_PARTIAL_V1).

گزارشِ ادمین: «وضعیتِ نیمه‌تمامو چرا نداره رباتت؟». قبلاً سفارشِ Partial برایِ همیشه «در حال انجام» می‌ماند
و پولِ مابقی برنمی‌گشت. این تست‌ها روی SQLiteِ درون‌حافظه‌ای و کدِ واقعیِ smm.py اجرا می‌شوند.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import smm
from tenant.shared import text_vars, public_api


# ───────────────────────────── محاسبهٔ مبلغ ─────────────────────────────

@pytest.mark.parametrize("price,qty,remains,expected", [
    (172, 350, 32, 16),          # نمونهٔ خودِ ادمین: ۱۷۲ − ۱۶ = ۱۵۶
    (1000, 1000, 250, 250),
    (1000, 3, 1, 333),           # گردکردنِ معمولی
    (1000, 3, 2, 667),
    (100, 10, 10, 100),          # همه باقی مانده ⇒ کلِ مبلغ
    (100, 10, 99, 100),          # بیشتر از تعداد ⇒ هرگز از کلِ مبلغ بیشتر نمی‌شود
    (100, 10, 0, 0),
    (0, 10, 5, 0),
    (100, 0, 5, 0),
    (None, 10, 5, 0),
])
def test_partial_refund_amount(price, qty, remains, expected):
    assert smm._partial_refund_amount(price, qty, remains) == expected


# ───────────────────────────── پایگاه‌دادهٔ آزمایشی ─────────────────────────────

@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(smm, "Session", maker)
    from tenant.bots.virtual_service import smm_refill      # PATCH_SMM_REFILL_V1: صفحهٔ جزئیات وضعیتِ ریفیل را هم می‌خواند
    monkeypatch.setattr(smm_refill, "Session", maker)
    monkeypatch.setattr(config, "Session", maker)          # text_vars از config.Session می‌خواند
    import tenant
    monkeypatch.setattr(tenant, "Session", maker)          # get_setting (پنل/تنظیمات) از tenant.Session می‌خواند
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def notified(monkeypatch):
    """پیام‌هایِ مشتری را به‌جایِ ارسالِ واقعی ثبت می‌کند."""
    sent: list[tuple[int, int, str]] = []

    async def fake_notify(bot_id, order_pk, text, bot=None):
        sent.append((bot_id, order_pk, text))
    monkeypatch.setattr(smm, "_notify_user", fake_notify)
    return sent


async def _make(maker, *, status="sent", qty=350, price=172, balance=132139, panel_oid="777",
                service_id="12"):
    async with maker() as s:
        bot = models.Bot(owner_id=1, token=f"1:T{status}{qty}{price}", username="shop", field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        u = models.BotUser(bot_id=bot.id, telegram_id=555, full_name="Ali", balance=balance)
        s.add(u)
        o = models.SmmOrder(bot_id=bot.id, user_telegram_id=555, service_id=service_id, service_name="ممبر",
                            link="https://t.me/x", quantity=qty, price_toman=price, status=status,
                            panel_order_id=panel_oid)
        s.add(o)
        await s.commit()
        return bot.id, o.id


async def _state(maker, bot_id, order_id):
    async with maker() as s:
        o = await s.get(models.SmmOrder, order_id)
        u = await s.scalar(select(models.BotUser).where(models.BotUser.bot_id == bot_id))
        txns = (await s.scalars(select(models.WalletTxn).where(models.WalletTxn.bot_id == bot_id))).all()
    return o, u, txns


# ───────────────────────────── _apply_partial ─────────────────────────────

@pytest.mark.asyncio
async def test_panel_partial_refunds_the_share_and_notifies(db, notified):
    bot_id, oid = await _make(db)
    assert await smm._apply_partial(bot_id, oid, 32, source="panel") == "partial"

    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == "partial"
    assert o.remaining == 32 and float(o.refunded_toman) == 16
    assert float(u.balance) == 132139 + 16
    assert len(txns) == 1 and float(txns[0].amount) == 16 and txns[0].kind == "smm_refund"
    assert len(notified) == 1 and notified[0][:2] == (bot_id, oid)


@pytest.mark.asyncio
async def test_running_twice_never_refunds_twice(db, notified):
    bot_id, oid = await _make(db)
    assert await smm._apply_partial(bot_id, oid, 32) == "partial"
    assert await smm._apply_partial(bot_id, oid, 32) == "skip"
    assert await smm._apply_partial(bot_id, oid, 100) == "skip"
    o, u, txns = await _state(db, bot_id, oid)
    assert float(u.balance) == 132139 + 16 and len(txns) == 1 and len(notified) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("final_status", ["completed", "refunded", "canceled", "partial"])
async def test_final_orders_are_untouched(db, notified, final_status):
    bot_id, oid = await _make(db, status=final_status)
    assert await smm._apply_partial(bot_id, oid, 10, source="admin") == "skip"
    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == final_status and float(u.balance) == 132139 and txns == [] and notified == []


@pytest.mark.asyncio
async def test_panel_source_only_from_sent_but_admin_also_from_awaiting_and_failed(db, notified):
    b1, o1 = await _make(db, status="awaiting_admin", qty=100, price=100)
    assert await smm._apply_partial(b1, o1, 10, source="panel") == "skip"
    assert await smm._apply_partial(b1, o1, 10, source="admin") == "partial"
    b2, o2 = await _make(db, status="failed", qty=101, price=100)
    assert await smm._apply_partial(b2, o2, 10, source="admin") == "partial"


@pytest.mark.asyncio
async def test_zero_remaining_means_completed_without_refund(db, notified):
    bot_id, oid = await _make(db)
    assert await smm._apply_partial(bot_id, oid, 0) == "completed"
    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == "completed" and float(u.balance) == 132139 and txns == []
    assert len(notified) == 1


@pytest.mark.asyncio
async def test_all_remaining_means_full_refund(db, notified):
    bot_id, oid = await _make(db, qty=100, price=500)
    assert await smm._apply_partial(bot_id, oid, 100) == "refunded"
    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == "refunded" and float(u.balance) == 132139 + 500


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "x", -1])
async def test_invalid_remaining_is_rejected(db, notified, bad):
    bot_id, oid = await _make(db)
    assert await smm._apply_partial(bot_id, oid, bad) == "skip"
    o, u, _ = await _state(db, bot_id, oid)
    assert o.status == "sent" and float(u.balance) == 132139


@pytest.mark.asyncio
async def test_other_bots_order_cannot_be_changed(db, notified):
    bot_id, oid = await _make(db)
    assert await smm._apply_partial(bot_id + 999, oid, 5) == "skip"


# ───────────────────────────── حلقهٔ استعلامِ پنل ─────────────────────────────

@pytest.mark.asyncio
async def test_panel_status_partial_is_applied_by_the_poll_loop(db, notified):
    bot_id, oid = await _make(db)
    await smm._apply_order_status(bot_id, oid, {"status": "Partial", "remains": "32"})
    o, u, _ = await _state(db, bot_id, oid)
    assert o.status == "partial" and o.panel_status == "partial"
    assert float(u.balance) == 132139 + 16


@pytest.mark.asyncio
async def test_panel_partial_without_remains_stays_sent_and_does_not_touch_money(db, notified):
    bot_id, oid = await _make(db)
    await smm._apply_order_status(bot_id, oid, {"status": "Partial"})
    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == "sent" and float(u.balance) == 132139 and txns == []


@pytest.mark.asyncio
async def test_panel_completed_still_works(db, notified):
    bot_id, oid = await _make(db)
    await smm._apply_order_status(bot_id, oid, {"status": "Completed", "remains": "0"})
    o, _, _ = await _state(db, bot_id, oid)
    assert o.status == "completed"


@pytest.mark.asyncio
async def test_partial_orders_are_no_longer_polled(db, notified, monkeypatch):
    """سفارشِ partial دیگر «sent» نیست ⇒ حلقهٔ استعلام (که فقط sent می‌گیرد) رهایش می‌کند."""
    bot_id, oid = await _make(db)
    await smm._apply_partial(bot_id, oid, 32)
    async with db() as s:
        pending = (await s.scalars(select(models.SmmOrder).where(models.SmmOrder.status == "sent"))).all()
    assert pending == []


# ───────────────────────────── پیام و متغیرها ─────────────────────────────

def test_partial_text_exists_and_uses_only_known_variables():
    from tenant.bots.virtual_service.smm_texts import TEXTS
    label, default = TEXTS["partial"]
    names = text_vars.find_names(default)
    assert names == {"paid_amount", "refund_amount", "final_amount", "new_balance",
                     "completed_qty", "remaining_qty"}
    import re
    used = set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", default))
    assert used == names, f"متغیرِ ناشناس در متنِ پیش‌فرض: {used - names}"


@pytest.mark.asyncio
async def test_default_partial_message_matches_the_admins_example(db, notified):
    """دقیقاً نمونه‌ای که ادمین داد: ۱۷۲ / ۱۶ / ۱۵۶ / ۱۳۲٬۱۵۵ / ۳۱۸ / ۳۲."""
    from tenant.bots.virtual_service.smm_texts import TEXTS
    bot_id, oid = await _make(db, balance=132139)
    await smm._apply_partial(bot_id, oid, 32)
    o, u, _ = await _state(db, bot_id, oid)
    out = await text_vars.render_text(TEXTS["partial"][1], bot_id=bot_id, user=u, order=o)
    assert "مبلغ پرداختی: 172 تومان" in out
    assert "مبلغ بازگشتی: 16 تومان" in out
    assert "مبلغ نهایی سفارش: 156 تومان" in out
    assert "موجودی جدید: 132,155 تومان" in out
    assert "مقدار تکمیل شده: 318" in out
    assert "مقدار باقی مانده: 32" in out
    assert "{{" not in out


@pytest.mark.asyncio
async def test_new_variables_are_sensible_for_other_statuses(db, notified):
    bot_id, oid = await _make(db, status="completed")
    o, u, _ = await _state(db, bot_id, oid)
    out = await text_vars.render_text(
        "{{paid_amount}}|{{refund_amount}}|{{final_amount}}|{{completed_qty}}|{{remaining_qty}}",
        bot_id=bot_id, user=u, order=o)
    assert out == "172|0|172|350|0"
    bot2, oid2 = await _make(db, status="refunded", qty=100, price=500)
    o2, u2, _ = await _state(db, bot2, oid2)
    out2 = await text_vars.render_text(
        "{{refund_amount}}|{{final_amount}}|{{completed_qty}}|{{remaining_qty}}",
        bot_id=bot2, user=u2, order=o2)
    assert out2 == "500|0|0|100"


def test_variables_are_documented_in_the_help_list():
    help_text = text_vars.variables_help_text("order")
    for name in ("paid_amount", "refund_amount", "final_amount", "new_balance",
                 "completed_qty", "remaining_qty"):
        assert f"{{{{{name}}}}}" in help_text


# ───────────────────────────── برچسب‌ها و API ─────────────────────────────

def test_partial_has_labels_everywhere():
    assert "partial" in smm._SMM_STATUS_LABEL and "partial" in smm._VS_STATUS_LABELS
    assert public_api._V2_STATUS_MAP["partial"] == "Partial"


@pytest.mark.asyncio
async def test_public_api_reports_partial_with_net_charge(db, notified):
    bot_id, oid = await _make(db)
    await smm._apply_partial(bot_id, oid, 32)
    o, _, _ = await _state(db, bot_id, oid)
    p = await public_api._v2_order_status_payload(o)
    assert p["status"] == "Partial" and p["remains"] == "32" and p["charge"] == "156.0"


# ───────────────────────────── صفحهٔ جزئیاتِ ادمین ─────────────────────────────

def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_admin_detail_offers_status_buttons_by_state(db, notified):
    bot_id, sent = await _make(db, status="sent")
    _, kb = await smm._smm_vs_order_detail_content(bot_id, sent, "smm:vs:list:1")
    cbs = _callbacks(kb)
    for code in ("done", "cancel", "part"):
        assert any(c.startswith(f"smm:vs:st:{code}:{sent}:") for c in cbs), code
    assert not any(c.startswith("smm:vs:st:run:") for c in cbs)   # sent است؛ «در حال انجام» بی‌معنی

    b2, failed = await _make(db, status="failed", qty=51)
    _, kb = await smm._smm_vs_order_detail_content(b2, failed, "smm:vs:failed:1")
    cbs = _callbacks(kb)
    assert any(c.startswith("smm:vs:retry:") for c in cbs)       # دکمه‌هایِ قبلی حفظ شده‌اند
    assert any(c.startswith("smm:vs:complete:") for c in cbs)
    assert any(c.startswith("smm:vs:refund:") for c in cbs)
    assert any(c.startswith("smm:vs:st:part:") for c in cbs)

    b3, manual = await _make(db, status="awaiting_admin", qty=52, service_id="man_1", panel_oid=None)
    _, kb = await smm._smm_vs_order_detail_content(b3, manual, "smm:vs:list:1")
    assert any(c.startswith("smm:vs:st:run:") for c in _callbacks(kb))

    b4, api = await _make(db, status="awaiting_admin", qty=53)
    _, kb = await smm._smm_vs_order_detail_content(b4, api, "smm:vs:list:1")
    assert not any(c.startswith("smm:vs:st:run:") for c in _callbacks(kb))   # سفارشِ API باید از مسیرِ تأیید برود


@pytest.mark.asyncio
@pytest.mark.parametrize("final_status", ["completed", "refunded", "canceled", "partial"])
async def test_admin_detail_has_no_status_buttons_for_final_orders(db, notified, final_status):
    bot_id, oid = await _make(db, status=final_status)
    _, kb = await smm._smm_vs_order_detail_content(bot_id, oid, "smm:vs:list:1")
    assert not any(c.startswith("smm:vs:st:") for c in _callbacks(kb))


@pytest.mark.asyncio
async def test_admin_detail_shows_refund_lines_for_partial(db, notified):
    bot_id, oid = await _make(db)
    await smm._apply_partial(bot_id, oid, 32)
    text, _ = await smm._smm_vs_order_detail_content(bot_id, oid, "smm:vs:list:1")
    assert "🟠 نیمه‌تمام" in text and "مبلغ بازگشتی" in text and "مبلغ نهایی" in text


@pytest.mark.asyncio
async def test_customer_detail_shows_partial_breakdown(db, notified):
    bot_id, oid = await _make(db)
    await smm._apply_partial(bot_id, oid, 32)
    text, kb = await smm._smm_myorder_detail_content(bot_id, 555, oid, 1)
    assert "تحویل‌شده: 318" in text and "تحویل‌نشده: 32" in text
    assert "مبلغِ بازگشتی به کیف پول: 16 تومان" in text
    assert "مبلغِ نهاییِ سفارش: 156 تومان" in text


# ───────────────────────────── جریانِ تغییرِ وضعیتِ دستی (هندلر واقعی) ─────────────────────────────

class _Msg:
    def __init__(self):
        self.calls = []

    async def edit_text(self, text, **kw):
        self.calls.append(("edit", text))

    async def answer(self, text="", **kw):
        self.calls.append(("answer", text))


class _Cb:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = _Msg()
        self.alerts = []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


class _State:
    def __init__(self):
        self.data, self.cur = {}, None

    async def set_state(self, s): self.cur = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)
    async def clear(self): self.data, self.cur = {}, None


@pytest_asyncio.fixture
async def admin(monkeypatch, db):
    async def fake_owner(cb_or_msg, bot):
        return SimpleNamespace(id=ADMIN["bot_id"], owner_id=1, plan="pro")
    ADMIN.clear()
    monkeypatch.setattr(smm, "_owner", fake_owner)
    return ADMIN

ADMIN: dict = {}


@pytest.mark.asyncio
async def test_done_asks_for_confirmation_then_completes_without_refund(db, notified, admin):
    bot_id, oid = await _make(db)
    admin["bot_id"] = bot_id
    cb = _Cb(f"smm:vs:st:done:{oid}:smm~vs~list~1")
    await smm.smm_vs_setstatus(cb, None, _State())
    assert cb.message.calls and "تکمیل" in cb.message.calls[0][1]
    o, u, _ = await _state(db, bot_id, oid)
    assert o.status == "sent"                                    # هنوز هیچ تغییری نکرده

    cb2 = _Cb(f"smm:vs:st:doneok:{oid}:smm~vs~list~1")
    await smm.smm_vs_setstatus(cb2, None, _State())
    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == "completed" and float(u.balance) == 132139 and txns == []
    assert len(notified) == 1


@pytest.mark.asyncio
async def test_cancel_refunds_the_full_amount_after_confirmation(db, notified, admin):
    bot_id, oid = await _make(db)
    admin["bot_id"] = bot_id
    cb = _Cb(f"smm:vs:st:cancel:{oid}:smm~vs~list~1")
    await smm.smm_vs_setstatus(cb, None, _State())
    assert "روی پنلِ ارائه‌دهنده لغو نمی‌کند" in cb.message.calls[0][1]      # هشدار برایِ سفارشِ API
    assert (await _state(db, bot_id, oid))[0].status == "sent"

    await smm.smm_vs_setstatus(_Cb(f"smm:vs:st:cancelok:{oid}:smm~vs~list~1"), None, _State())
    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == "refunded" and float(u.balance) == 132139 + 172 and len(txns) == 1


@pytest.mark.asyncio
async def test_partial_flow_asks_quantity_validates_confirms_then_applies(db, notified, admin):
    from tenant.bots.virtual_service.smm import VsPartialFSM
    bot_id, oid = await _make(db)
    admin["bot_id"] = bot_id
    state = _State()
    cb = _Cb(f"smm:vs:st:part:{oid}:smm~vs~list~1")
    await smm.smm_vs_setstatus(cb, None, state)
    assert state.cur == VsPartialFSM.qty and state.data["vsp_oid"] == oid

    async def send(text):
        m = SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), replies=[])
        async def _answer(t="", **kw): m.replies.append((t, kw))
        m.answer = _answer
        await smm.smm_vs_partial_qty(m, None, state)
        return m

    for bad in ("abc", "0", "350", "9999"):
        m = await send(bad)
        assert "عددِ معتبر" in m.replies[0][0] and state.cur == VsPartialFSM.qty, bad
    assert (await _state(db, bot_id, oid))[0].status == "sent"    # ورودیِ بد هیچ‌چیز را عوض نکرد

    m = await send("۳۲")                                           # ارقامِ فارسی هم قبول است
    text, kw = m.replies[0]
    assert "بازگشتی" in text and "16" in text and "156" in text
    assert state.cur is None                                       # وضعیتِ انتظار پاک شد
    yes_cb = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row][0]
    assert yes_cb.endswith(":32") and (await _state(db, bot_id, oid))[0].status == "sent"

    await smm.smm_vs_setstatus(_Cb(yes_cb), None, _State())
    o, u, _ = await _state(db, bot_id, oid)
    assert o.status == "partial" and float(o.refunded_toman) == 16 and float(u.balance) == 132139 + 16


@pytest.mark.asyncio
async def test_stale_buttons_do_not_change_final_orders(db, notified, admin):
    bot_id, oid = await _make(db, status="completed")
    admin["bot_id"] = bot_id
    for code in ("doneok", "cancelok", "partok"):
        cb = _Cb(f"smm:vs:st:{code}:{oid}:smm~vs~list~1:5")
        await smm.smm_vs_setstatus(cb, None, _State())
        assert cb.alerts and "قابلِ تغییر نیست" in cb.alerts[0], code
    o, u, txns = await _state(db, bot_id, oid)
    assert o.status == "completed" and float(u.balance) == 132139 and txns == []


@pytest.mark.asyncio
async def test_run_only_for_manual_awaiting_orders(db, notified, admin):
    bot_id, api = await _make(db, status="awaiting_admin", qty=60)
    admin["bot_id"] = bot_id
    cb = _Cb(f"smm:vs:st:run:{api}:smm~vs~list~1")
    await smm.smm_vs_setstatus(cb, None, _State())
    assert (await _state(db, bot_id, api))[0].status == "awaiting_admin"
    assert "فقط برایِ سفارشِ دستی" in cb.alerts[0]

    b2, man = await _make(db, status="awaiting_admin", qty=61, service_id="man_2", panel_oid=None)
    admin["bot_id"] = b2
    await smm.smm_vs_setstatus(_Cb(f"smm:vs:st:run:{man}:smm~vs~list~1"), None, _State())
    assert (await _state(db, b2, man))[0].status == "sent"


def test_status_change_callbacks_fit_telegram_limit():
    worst = "smm:vs:st:partok:9999999:smm~vs~failed~999:99999999"
    assert len(worst.encode()) <= 64


def test_new_callback_prefix_does_not_collide_with_other_handlers():
    import inspect
    src = inspect.getsource(smm)
    assert src.count('F.data.startswith("smm:vs:st:")') == 1
    assert 'F.data.startswith("smm:vs:s")' not in src


@pytest.mark.asyncio
async def test_full_refund_after_partial_only_returns_the_rest(db, notified):
    """PATCH_SMM_PARTIAL_OVERREFUND: لغو/بازگشتِ کامل بعد از نیمه‌تمام نباید کلِ مبلغ را دوباره پس بدهد."""
    bot_id, oid = await _make(db)
    assert await smm._apply_partial(bot_id, oid, 32, source="panel") == "partial"   # ۱۶ تومان برگشت
    assert await smm._refund_order(bot_id, oid, reason="لغو توسطِ ادمین") is True
    o, u, txns = await _state(db, bot_id, oid)
    assert float(u.balance) == 132139 + 172                  # جمعاً دقیقاً همان مبلغِ پرداختی
    assert float(o.refunded_toman) == 172 and o.status == "refunded"
