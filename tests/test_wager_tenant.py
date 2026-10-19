"""PATCH_WAGER_TENANT_V1 / PATCH_WAGER_ADDON_V1 — شرط‌بندی در ربات‌های مستأجر + فروش افزونه از ربات اصلی."""
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot_addons
import config
import models
import platform_bot
import platform_group_money as GM
import platform_wager as PW
import platform_wager_sale as SALE
import tenant
import tenant.shared.wager_games as WG
from tenant.shared.group_wallet import _group_feature_allowed

OWNER, U1, U2, U3 = 1000, 11, 22, 33
BOT, BOT2 = 5, 6
CFG = {"min": 1000, "max": 0, "fee_pct": Decimal("0"), "ttl": 300, "move": 180}
FEE10 = {**CFG, "fee_pct": Decimal("10")}


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PW, GM, WG, SALE, tenant, platform_bot):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    bot_addons.invalidate()
    async with maker() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="shop", field="virtual_service", plan="pro", status="active"))
        s.add(models.Bot(id=BOT2, owner_id=OWNER, token="6:T", username="shop2", field="virtual_service", plan="pro", status="active"))
        for b in (BOT, BOT2):
            for uid in (U1, U2, U3):
                s.add(models.BotUser(bot_id=b, telegram_id=uid, full_name=f"u{uid}", username=f"u{uid}", balance=100_000))
        s.add(models.PlatformUser(telegram_id=OWNER, full_name="owner", username="owner", balance=500_000))
        s.add(models.PlatformUser(telegram_id=U1, full_name="u11", username="u11", balance=100_000))
        await s.commit()
    yield maker
    await engine.dispose()


async def bu(db, uid, bot=BOT):
    async with db() as s:
        return int((await s.scalar(select(models.BotUser).where(models.BotUser.bot_id == bot, models.BotUser.telegram_id == uid))).balance)


async def pu(db, uid):
    async with db() as s:
        return int((await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))).balance)


async def wager(db, wid):
    async with db() as s:
        return await s.get(models.PlatformWager, wid)


async def txns(db, bot=BOT):
    async with db() as s:
        return [t.kind for t in (await s.scalars(select(models.WalletTxn).where(models.WalletTxn.bot_id == bot).order_by(models.WalletTxn.id))).all()]


async def new(db, game="xo", amount=10_000, cfg=CFG, creator=U1, bot=BOT, target=None):
    w, err = await PW.create_wager(game, -100, (creator, f"u{creator}", f"u{creator}"), amount, target, cfg, bot_id=bot)
    assert err is None, err
    return w


async def join(w, uid=U2, bot=BOT):
    r = await PW.join_wager(w.id, (uid, f"u{uid}", f"u{uid}"), 180, bot_id=bot)
    assert r.err is None, r.err
    return r.w


async def expire(db, wid, secs=1):
    async with db() as s:
        w = await s.get(models.PlatformWager, wid)
        w.expires_at = datetime.utcnow() - timedelta(seconds=secs)
        await s.commit()


async def activate(db, bot=BOT):
    async with db() as s:
        s.add(models.BotAddon(bot_id=bot, addon=bot_addons.WAGER_ADDON, amount=100_000, paid_by=OWNER))
        await s.commit()
    bot_addons.invalidate()


# ───────── هسته با کیف‌پول BotUser ─────────

@pytest.mark.asyncio
async def test_tenant_wager_uses_bot_wallet_and_ledger_not_platform(db):
    w = await new(db)
    assert w.bot_id == BOT and await bu(db, U1) == 90_000 and await pu(db, U1) == 100_000      # PlatformUser دست‌نخورده
    assert await txns(db) == ["wager_reserve"]
    await join(w)
    assert await bu(db, U2) == 90_000 and await txns(db) == ["wager_reserve", "wager_stake"]
    assert await bu(db, U1, BOT2) == 100_000                                                    # ربات دیگر دست‌نخورده


@pytest.mark.asyncio
async def test_tenant_xo_win_pays_bot_wallet_with_fee_and_conserves_money(db):
    async def total():
        async with db() as s:
            return int(await s.scalar(select(func.sum(models.BotUser.balance)).where(models.BotUser.bot_id == BOT)))
    start = await total()
    w = await join(await new(db, cfg=FEE10))
    first = (await wager(db, w.id)).turn_id
    second = U2 if first == U1 else U1
    for uid, cell in ((first, 0), (second, 3), (first, 1), (second, 4), (first, 2)):
        r = await PW.xo_move(w.id, uid, cell, 180, bot_id=BOT)
        assert r.err is None, r.err
    assert r.w.status == "settled" and r.w.winner_id == first
    assert await bu(db, first) == 100_000 - 10_000 + 18_000 and await bu(db, second) == 90_000
    assert await total() == start - 2_000                                                        # فقط کارمزد از چرخه خارج شد
    assert (await txns(db)).count("wager_prize") == 1 and "wager_fee" in await txns(db)


@pytest.mark.asyncio
async def test_scope_isolation_between_platform_and_bots(db):
    w = await new(db)
    for bad in (0, BOT2):
        assert (await PW.join_wager(w.id, (U2, "x", "x"), 180, bot_id=bad)).err
        assert (await PW.cancel_wager(w.id, U1, bot_id=bad)).err
    w = await join(w)
    for bad in (0, BOT2):
        assert (await PW.xo_move(w.id, w.turn_id, 0, 180, bot_id=bad)).err
        assert (await PW.rps_pick(w.id, U1, "r", bot_id=bad)).err
        assert (await PW.dice_claim(w.id, U1, bot_id=bad)).err
    assert (await wager(db, w.id)).status == "playing"
    # سقف ۳ شرط هم‌زمان برای هر ربات جداست
    for _ in range(PW.MAX_ACTIVE - 1):
        await new(db, amount=1000, creator=U3)
    await new(db, amount=1000, creator=U3, bot=BOT2)
    assert await bu(db, U3, BOT2) == 99_000


@pytest.mark.asyncio
async def test_sweeper_is_scoped_and_platform_has_overdue_safety_net(db):
    plat = await PW.create_wager("xo", -1, (U1, "x", "x"), 5000, None, CFG)
    plat = plat[0]
    ten = await new(db, amount=4000)
    await expire(db, plat.id)
    await expire(db, ten.id)
    assert await PW.sweep_expired(None, 0) == 1                                  # فقط ربات اصلی
    assert (await wager(db, ten.id)).status == "pending"
    assert await PW.sweep_expired(None, None, overdue_secs=600) == 0             # هنوز ۱۰ دقیقه نگذشته
    await expire(db, ten.id, secs=700)
    assert await PW.sweep_expired(None, None, overdue_secs=600) == 1             # شبکهٔ اطمینان
    assert (await wager(db, ten.id)).status == "expired" and await bu(db, U1) == 100_000


@pytest.mark.asyncio
async def test_render_uses_tenant_callbacks_and_plain_buttons(db):
    w = await new(db)
    text, kb = PW.render(w)
    btns = [b for row in kb.inline_keyboard for b in row]
    assert [b.callback_data for b in btns] == [f"twg:join:{w.id}", f"twg:cancel:{w.id}"]
    assert all(not getattr(b, "icon_custom_emoji_id", None) for b in btns)
    w = await join(w)
    _, kb = PW.render(w)
    assert all(b.callback_data.startswith("twg:x:") for row in kb.inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_cfg_reads_tenant_settings_and_defaults(db):
    cfg = await PW.load_cfg(BOT)
    assert cfg["min"] == 1000 and cfg["max"] == 0 and cfg["fee_pct"] == 0 and cfg["move"] == 180
    assert not await PW.master_enabled(BOT)
    await tenant.set_setting(BOT, "group_betting_enabled", "1")
    await tenant.set_setting(BOT, "group_betting_fee_enabled", "1")
    await tenant.set_setting(BOT, "group_betting_fee_pct", "7")
    await tenant.set_setting(BOT, "group_betting_min", "5,000")
    await tenant.set_setting(BOT, "wager_game_dice", "off")
    cfg = await PW.load_cfg(BOT)
    assert await PW.master_enabled(BOT) and cfg["fee_pct"] == 7 and cfg["min"] == 5000
    assert not await PW.game_enabled("dice", BOT) and await PW.game_enabled("xo", BOT)
    assert await PW.master_enabled(BOT2) is False                                  # تنظیمات ربات دیگر جداست


# ───────── هندلرهای مستأجر ─────────

def _bot(token="5:T"):
    class B:
        pass
    b = B()
    b.token = token
    b.id = 5
    return b


def _msg(text, uid=U1, reply_to=None, chat_type="supergroup"):
    sent = SimpleNamespace(message_id=900)
    replies = []

    async def reply(t, **kw):
        replies.append(t)
        return sent
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=-100, type=chat_type), sender_chat=None, reply_to_message=reply_to,
                           from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"u{uid}", is_bot=False),
                           reply=reply, bot=None), replies


def _cb(data, uid, edits):
    async def edit_text(t, **kw):
        edits.append((t, kw.get("reply_markup")))

    async def answer(t="", show_alert=False):
        edits.append(("ANSWER", t, show_alert))
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"u{uid}"),
                           message=SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"), message_id=900,
                                                   edit_text=edit_text, answer=None), answer=answer)


async def enable_betting(master=True):
    await tenant.set_setting(BOT, "group_betting_enabled", "1" if master else "0")


@pytest.mark.asyncio
async def test_not_activated_only_owner_is_told(db):
    await enable_betting()
    m, replies = _msg("تاس 5000", uid=U1)
    with pytest.raises(SkipHandler):
        await WG.tw_create(m, _bot())
    assert replies == [] and await bu(db, U1) == 100_000
    m, replies = _msg("تاس 5000", uid=OWNER)
    await WG.tw_create(m, _bot())
    assert replies and "فعال نشده" in replies[0] and "ربات‌های من" in replies[0]


@pytest.mark.asyncio
async def test_ineligible_bot_kind_falls_through(db):
    async with db() as s:
        b = await s.get(models.Bot, BOT)
        b.field = "digital_shop"
        await s.commit()
    tenant._BOT_ROW_CACHE.clear()
    await activate(db)
    await enable_betting()
    m, replies = _msg("xo 5000")
    with pytest.raises(SkipHandler):
        await WG.tw_create(m, _bot())
    assert replies == [] and await bu(db, U1) == 100_000


@pytest.mark.asyncio
async def test_create_requires_master_and_game_switch(db):
    await activate(db)
    m, replies = _msg("xo 5000")
    await WG.tw_create(m, _bot())                                  # کلید اصلی خاموش
    assert "فعال نیست" in replies[0] and await bu(db, U1) == 100_000
    await enable_betting()
    await tenant.set_setting(BOT, "wager_game_xo", "off")
    m, replies = _msg("xo 5000")
    await WG.tw_create(m, _bot())
    assert "ایکس‌او" in replies[0] and "فعال نیست" in replies[0] and await bu(db, U1) == 100_000
    m, replies = _msg("تاس 5000")                                  # بازی دیگر روشن است
    await WG.tw_create(m, _bot())
    assert "شرط‌بندی تاس" in replies[0] and await bu(db, U1) == 95_000


@pytest.mark.asyncio
async def test_anonymous_sender_is_told_why(db):
    await activate(db)
    await enable_betting()
    m, replies = _msg("xo 5000")
    m.sender_chat = SimpleNamespace(id=-1)
    await WG.tw_create(m, _bot())
    assert "ناشناس" in replies[0] and await bu(db, U1) == 100_000


@pytest.mark.asyncio
async def test_full_game_through_tenant_callbacks_and_cross_bot_rejection(db):
    await activate(db)
    await enable_betting()
    m, replies = _msg("اکس او ۲۰,۰۰۰")
    await WG.tw_create(m, _bot())
    assert "20,000" in replies[0] and await bu(db, U1) == 80_000
    async with db() as s:
        row = await s.scalar(select(models.PlatformWager))
        assert row.message_id == 900 and row.bot_id == BOT
        wid = row.id
    edits = []
    await WG.tw_join(_cb(f"twg:join:{wid}", U1, edits), _bot())                      # سازنده نمی‌تواند
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True
    await WG.tw_join(_cb(f"twg:join:{wid}", U2, edits), _bot())
    row = await wager(db, wid)
    assert row.status == "playing" and await bu(db, U2) == 80_000
    # همان کلید callback از ربات دیگر (با توکن دیگر) هیچ اثری ندارد
    other = _bot("6:T")
    await WG.tw_xo(_cb(f"twg:x:{wid}:0", row.turn_id, edits), other)
    assert (await wager(db, wid)).state == row.state
    first = row.turn_id
    second = U2 if first == U1 else U1
    for uid, cell in ((first, 0), (second, 3), (first, 1), (second, 4), (first, 2)):
        await WG.tw_xo(_cb(f"twg:x:{wid}:{cell}", uid, edits), _bot())
    row = await wager(db, wid)
    assert row.status == "settled" and row.winner_id == first
    assert await bu(db, first) == 120_000 and await bu(db, second) == 80_000
    assert await bu(db, U1, BOT2) == 100_000


@pytest.mark.asyncio
async def test_tenant_dice_and_rps_callbacks(db, monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(WG.asyncio, "sleep", nosleep)
    await activate(db)
    await enable_betting()
    m, _ = _msg("تاس 2000")
    await WG.tw_create(m, _bot())
    wid = (await _last(db)).id
    edits = []
    await WG.tw_join(_cb(f"twg:join:{wid}", U2, edits), _bot())
    rolls = iter([2, 6])

    class TBot:
        token = "5:T"
        id = 5

        async def send_dice(self, chat_id, emoji):
            return SimpleNamespace(dice=SimpleNamespace(value=next(rolls)))
    await WG.tw_dice(_cb(f"twg:d:{wid}", U1, edits), TBot())
    await WG.tw_dice(_cb(f"twg:d:{wid}", U2, edits), TBot())
    row = await wager(db, wid)
    assert row.status == "settled" and row.winner_id == U2 and await bu(db, U2) == 102_000
    m, _ = _msg("سنگ کاغذ قیچی 3000", uid=U3)
    await WG.tw_create(m, _bot())
    wid = (await _last(db)).id
    await WG.tw_join(_cb(f"twg:join:{wid}", U1, edits), _bot())
    await WG.tw_rps(_cb(f"twg:r:{wid}:r", U3, edits), _bot())
    await WG.tw_rps(_cb(f"twg:r:{wid}:s", U1, edits), _bot())
    assert (await wager(db, wid)).winner_id == U3


async def _last(db):
    async with db() as s:
        return await s.scalar(select(models.PlatformWager).order_by(models.PlatformWager.id.desc()))


@pytest.mark.asyncio
async def test_owner_settings_screen_and_game_toggle(db):
    from tenant.shared.group_betting import _bet_settings_view
    text, kb = await _bet_settings_view(BOT)
    assert "فعال نشده" in text and not any(b.callback_data.startswith("twg:set:") for r in kb.inline_keyboard for b in r)
    await activate(db)
    text, kb = await _bet_settings_view(BOT)
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {f"twg:set:{g}" for g in PW.GAMES} <= set(datas) and "gset:bet_toggle" in datas and datas[-1] == "tadm:groupsettings"
    edits = []
    await WG.tw_game_toggle(_cb("twg:set:xo", U1, edits), _bot())                   # کاربر عادی
    assert await PW.game_enabled("xo", BOT)
    await WG.tw_game_toggle(_cb("twg:set:xo", OWNER, edits), _bot())
    assert not await PW.game_enabled("xo", BOT)
    await WG.tw_game_toggle(_cb("twg:set:xo", OWNER, edits), _bot())
    assert await PW.game_enabled("xo", BOT)


@pytest.mark.asyncio
async def test_expiry_loop_settles_and_edits_with_the_bots_own_token(db, monkeypatch):
    calls = []

    class FakeBot:
        def __init__(self, token):
            self.token = token
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            return None

        async def edit_message_text(self, text, chat_id, message_id, **kw):
            calls.append((self.token, chat_id, message_id, text))
    monkeypatch.setattr(WG, "Bot", FakeBot)
    w = await new(db, amount=3000)
    async with db() as s:
        row = await s.get(models.PlatformWager, w.id)
        row.message_id = 77
        await s.commit()
    plat = (await PW.create_wager("xo", -1, (U1, "x", "x"), 2000, None, CFG))[0]
    await expire(db, w.id)
    await expire(db, plat.id)
    assert await WG.expire_wagers_once() == 1                                      # فقط مستأجر؛ ربات اصلی کار خودش را می‌کند
    assert calls and calls[0][0] == "5:T" and calls[0][2] == 77 and "شرط‌بندی" in calls[0][3]
    assert (await wager(db, w.id)).status == "expired" and await bu(db, U1) == 100_000
    assert (await wager(db, plat.id)).status == "pending"
    assert await WG.expire_wagers_once() == 0


# ───────── فروش افزونه از ربات اصلی ─────────

def test_eligibility_parity_with_group_wallet_rule():
    for field in ("virtual_service", "vpn_sales", "digital_shop", "booking", None):
        for plan in ("free", "growth", "pro", "partner", None):
            row = SimpleNamespace(field=field, plan=plan)
            assert bot_addons.wager_eligible(field, plan) == _group_feature_allowed(row), (field, plan)


@pytest.fixture
def sale_env(db, monkeypatch):
    store = {}

    async def get_pset(key, default=""):
        return store.get(key, default)

    async def set_pset(key, value):
        store[key] = value
    monkeypatch.setattr(platform_bot, "get_pset", get_pset)
    monkeypatch.setattr(platform_bot, "set_pset", set_pset)
    monkeypatch.setattr(SALE, "PLATFORM_ADMIN_ID", 1)
    insufficient = []

    async def fake_insufficient(cb, need, note=""):
        insufficient.append(need)
    monkeypatch.setattr(platform_bot, "_insufficient_balance", fake_insufficient)
    sent = []

    class PBot:
        async def send_message(self, uid, text, **kw):
            sent.append((uid, text))
    return SimpleNamespace(store=store, insufficient=insufficient, sent=sent, pbot=PBot())


def _pcb(data, uid, edits, pbot=None):
    cb = _cb(data, uid, edits)
    cb.bot = pbot

    async def answer_msg(t, **kw):
        edits.append(("MSG", t))
    cb.message.answer = answer_msg
    return cb


async def addons(db):
    async with db() as s:
        return (await s.scalars(select(models.BotAddon))).all()


@pytest.mark.asyncio
async def test_buy_charges_once_and_activates(db, sale_env):
    edits = []
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", OWNER, edits, sale_env.pbot))
    assert await pu(db, OWNER) == 400_000                                          # ۱۰۰٬۰۰۰ پیش‌فرض
    rows = await addons(db)
    assert len(rows) == 1 and rows[0].bot_id == BOT and int(rows[0].amount) == 100_000 and rows[0].source == "wallet"
    async with db() as s:
        t = await s.scalar(select(models.PlatformTxn).where(models.PlatformTxn.kind == "purchase"))
        assert int(t.amount) == -100_000 and "@shop" in t.note
    assert await bot_addons.addon_active(BOT) and not await bot_addons.addon_active(BOT2)
    assert any(e[0] == "MSG" and "فعال شد" in e[1] for e in edits)
    assert sale_env.sent and "shop" in sale_env.sent[0][1]                          # مدیر اصلی خبردار شد
    edits.clear()
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", OWNER, edits, sale_env.pbot))        # کلیک دوباره: بدون کسر دوباره
    assert await pu(db, OWNER) == 400_000 and len(await addons(db)) == 1
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True


@pytest.mark.asyncio
async def test_buy_custom_price_and_zero_price(db, sale_env):
    sale_env.store["wager_sale_price"] = "250000"
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", OWNER, [], sale_env.pbot))
    assert await pu(db, OWNER) == 250_000
    sale_env.store["wager_sale_price"] = "0"
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT2}", OWNER, [], sale_env.pbot))
    assert await pu(db, OWNER) == 250_000
    assert {r.bot_id: r.source for r in await addons(db)} == {BOT: "wallet", BOT2: "free"}


@pytest.mark.asyncio
async def test_buy_insufficient_balance_and_guards(db, sale_env):
    async with db() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == OWNER))
        u.balance = 50_000
        await s.commit()
    edits = []
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", OWNER, edits, sale_env.pbot))
    assert sale_env.insufficient == [100_000] and not await addons(db) and await pu(db, OWNER) == 50_000
    # غیر مالک
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", U1, edits, sale_env.pbot))
    assert not await addons(db) and await pu(db, U1) == 100_000
    # نوع ناسازگار
    async with db() as s:
        b = await s.get(models.Bot, BOT)
        b.field = "digital_shop"
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == OWNER))
        u.balance = 500_000
        await s.commit()
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", OWNER, edits, sale_env.pbot))
    assert not await addons(db) and await pu(db, OWNER) == 500_000
    # فروش خاموش
    sale_env.store["wager_sale_enabled"] = "off"
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT2}", OWNER, edits, sale_env.pbot))
    assert not await addons(db) and await pu(db, OWNER) == 500_000


@pytest.mark.asyncio
async def test_platform_admin_activates_for_free(db, sale_env, monkeypatch):
    monkeypatch.setattr(SALE, "PLATFORM_ADMIN_ID", OWNER)
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", OWNER, [], sale_env.pbot))
    rows = await addons(db)
    assert len(rows) == 1 and rows[0].source == "free" and await pu(db, OWNER) == 500_000
    assert sale_env.sent == []


@pytest.mark.asyncio
async def test_archived_bot_cannot_buy(db, sale_env):
    async with db() as s:
        b = await s.get(models.Bot, BOT)
        b.archived_at = datetime.utcnow()
        await s.commit()
    await SALE.wgr_buy(_pcb(f"wgr:buy:{BOT}", OWNER, [], sale_env.pbot))
    assert not await addons(db) and await pu(db, OWNER) == 500_000


@pytest.mark.asyncio
async def test_menu_texts_and_my_bot_button(db, sale_env):
    edits = []
    await SALE.wgr_menu(_pcb(f"wgr:menu:{BOT}", OWNER, edits))
    text, kb = edits[0]
    assert "100,000 تومان" in text and "مسئولیت قانونی" in text
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == [f"wgr:buy:{BOT}", f"mb:{BOT}"]
    btn = await SALE.mybot_button(BOT, "virtual_service", "pro")
    assert btn is not None and btn.callback_data == f"wgr:menu:{BOT}" and "✅" not in btn.text
    assert await SALE.mybot_button(BOT, "digital_shop", "pro") is None                  # نوع ناسازگار
    sale_env.store["wager_sale_enabled"] = "off"
    assert await SALE.mybot_button(BOT, "virtual_service", "pro") is None               # فروش خاموش و فعال‌نشده
    await activate(db)
    btn = await SALE.mybot_button(BOT, "virtual_service", "pro")
    assert btn is not None and "✅" in btn.text                                           # فعال‌شده همیشه دیده می‌شود
    edits.clear()
    await SALE.wgr_menu(_pcb(f"wgr:menu:{BOT}", OWNER, edits))
    assert "فعال است" in edits[0][0] and [b.callback_data for r in edits[0][1].inline_keyboard for b in r] == [f"mb:{BOT}"]
    edits.clear()
    await SALE.wgr_menu(_pcb(f"wgr:menu:{BOT}", U1, edits))                              # غیر مالک
    assert edits[-1][0] == "ANSWER"


@pytest.mark.asyncio
async def test_admin_sale_settings_in_hub(db, sale_env, monkeypatch):
    monkeypatch.setattr(GM, "PLATFORM_ADMIN_ID", 1)
    monkeypatch.setattr(PW, "_pset", platform_bot.get_pset)
    monkeypatch.setattr(GM, "_pset", platform_bot.get_pset)
    text, kb = await PW.hub_screen()
    assert "فروش به ربات‌های مشتری" in text and "100,000 تومان" in text and "ربات‌های فعال‌شده: 0" in text
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "wgrs:price" in datas and "wgrs:toggle" in datas and datas[-1] == "adm:back"
    edits = []
    await SALE.wgrs_toggle(_pcb("wgrs:toggle", 999, edits))                              # غیر مدیر
    assert "wager_sale_enabled" not in sale_env.store
    await SALE.wgrs_toggle(_pcb("wgrs:toggle", 1, edits))
    assert sale_env.store["wager_sale_enabled"] == "off" and "خاموش" in edits[-2][0]
    # قیمت: ورودی نامعتبر / معتبر
    state = SimpleNamespace(cleared=False)

    async def clear():
        state.cleared = True
    state.clear = clear
    replies = []

    async def answer(t, **kw):
        replies.append(t)
    m = SimpleNamespace(from_user=SimpleNamespace(id=1), text="abc", answer=answer)
    await SALE.wgrs_price_save(m, state)
    assert not state.cleared and "معتبر" in replies[0]
    m.text = "۱۵۰,۰۰۰"
    await SALE.wgrs_price_save(m, state)
    assert state.cleared and sale_env.store["wager_sale_price"] == "150000"
    assert await SALE.sale_price() == 150_000
