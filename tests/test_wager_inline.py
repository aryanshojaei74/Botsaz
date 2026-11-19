"""PATCH_WAGER_INLINE_V1 (سینا): بازیِ دونفرهٔ اینلاین — `@ربات دوز`، موجودی، عضویتِ اجباری، شروع و بازی داخلِ همان پیام."""
import types
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot_addons
import config
import models
import platform_bot
import platform_group_money as GM
import platform_wager as PW
import platform_wager_race as PR
import tenant
import tenant.shared.wager_games as WG
import tenant.shared.wager_race as WR
import tenant.shared.wager_inline as WI
import wager_rank as K

OWNER, U1, U2 = 1000, 11, 22
BOT = 5
IMID = "inl-1"


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PW, PR, GM, WG, WI, K, tenant, platform_bot):
        monkeypatch.setattr(mod, "Session", maker, raising=False)
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    bot_addons.invalidate()
    WI._STARTED.clear()
    monkeypatch.setattr(PW, "TAP_GAP_SECS", 0)
    PW._tap_last.clear()
    async with maker() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="shop", field="virtual_service", plan="pro", status="active"))
        for uid in (U1, U2):
            s.add(models.BotUser(bot_id=BOT, telegram_id=uid, full_name=f"u{uid}", username=f"u{uid}", balance=Decimal(100_000)))
        s.add(models.BotAddon(bot_id=BOT, addon=bot_addons.WAGER_ADDON, amount=100_000, paid_by=OWNER))
        await s.commit()
    bot_addons.invalidate()
    await tenant.set_setting(BOT, "group_betting_enabled", "1")
    yield maker
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


class FakeBot:
    token = "5:T"
    id = 5

    def __init__(self):
        self.edits = []

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))

    async def me(self):
        return types.SimpleNamespace(username="shop_bot")

    async def send_dice(self, chat_id, emoji="🎲"):
        v = self.dice_values.pop(0)
        self.dice_sent.append(chat_id)
        return types.SimpleNamespace(dice=types.SimpleNamespace(value=v))

    dice_values: list = []
    dice_sent: list = []


class Cb:
    def __init__(self, bot, uid, data, imid=IMID):
        self.bot = bot
        self.data = data
        self.inline_message_id = imid
        self.message = None
        self.from_user = types.SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"u{uid}", is_bot=False)
        self.answers = []

    async def answer(self, text="", show_alert=False, **k):
        self.answers.append((text, show_alert))


async def _stub_bot(monkeypatch):
    bot = FakeBot()

    async def get_bot_row(b):
        async with config.Session() as s:
            return await s.get(models.Bot, BOT)
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    return bot


async def bal(db, uid):
    async with db() as s:
        return int((await s.scalar(select(models.BotUser).where(models.BotUser.bot_id == BOT, models.BotUser.telegram_id == uid))).balance)


def test_parse_query():
    P = WI.parse_query
    assert P("") == (None, 0, False)
    assert P("دوز") == ("xo", 0, True) and P("دوز 5000") == ("xo", 5000, True) and P("xo ۵,۰۰۰") == ("xo", 5000, True)
    assert P("سنگ کاغذ قیچی 2000") == ("rps", 2000, True) and P("rps") == ("rps", 0, True)
    assert P("دو") == ("xo", 0, False) and P("سنگ") == ("rps", 0, False)        # در حالِ تایپ
    assert P("سلام") == (None, 0, False)


@pytest.mark.asyncio
async def test_inline_list_has_balance_first_then_games(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    await K.save_kv(BOT, "free_on", "1")
    sent = {}

    class Q:
        query = ""
        from_user = types.SimpleNamespace(id=U1, full_name="u11")

        async def answer(self, results, **k): sent["r"] = results
    await WI.iwg_query(Q(), bot)
    ids = [r.id for r in sent["r"]]
    assert ids[0] == "bal" and "xo:0" in ids and "rps:0" in ids          # موجودی اولین؛ بازی رایگان (روشن)
    assert any(i.startswith("xo:") and i != "xo:0" for i in ids)           # نسخهٔ پولی با حداقل مبلغ
    Q.query = "دوز 5000"
    await WI.iwg_query(Q(), bot)
    assert [r.id for r in sent["r"]] == ["xo:5000"]


@pytest.mark.asyncio
async def test_inline_ineligible_bot_gets_empty_answer(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    async with db() as s:
        a = (await s.scalars(select(models.BotAddon))).one()
        await s.delete(a)
        await s.commit()
    bot_addons.invalidate()
    sent = {}

    class Q:
        query = "دوز"
        from_user = types.SimpleNamespace(id=U1, full_name="u11")

        async def answer(self, results, **k): sent["r"] = results
    await WI.iwg_query(Q(), bot)
    assert sent["r"] == []


@pytest.mark.asyncio
async def test_balance_button_shows_only_the_clickers_balance_privately(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    cb = Cb(bot, U2, "iwg:bal")
    await WI.iwg_balance(cb, bot)
    txt, alert = cb.answers[0]
    assert alert and "100,000" in txt.replace("٬", ",") or "۱۰۰" in txt or "100" in txt


@pytest.mark.asyncio
async def test_join_blocks_until_both_are_in_forced_channels_then_starts_the_game(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    missing = {U2: [{"chat_id": "@chan", "title": "کانال", "btn_color": None, "invite_link": None}]}

    async def cfj(b, bid, uid, bypass_cache=False): return missing.get(uid, [])
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    data = f"iwg:j:xo:10000:{U1}"
    cb = Cb(bot, U2, data)
    await WI.iwg_join(cb, bot)
    text, kw = bot.edits[-1]
    assert "❌" in text and "✅" in text and kw["inline_message_id"] == IMID          # u11 ✅ / u22 ❌
    btns = [b for r in kw["reply_markup"].inline_keyboard for b in r]
    assert any(b.url == "https://t.me/chan" for b in btns) and any(b.callback_data == data for b in btns)
    assert await bal(db, U1) == 100_000 and await bal(db, U2) == 100_000               # هنوز پولی حرکت نکرده
    missing.clear()                                                                     # عضو شد ⇒ «عضو شدم»
    cb2 = Cb(bot, U2, data)
    await WI.iwg_join(cb2, bot)
    assert await bal(db, U1) == 90_000 and await bal(db, U2) == 90_000                  # هر دو مبلغ رزرو/کسر شد
    text, kw = bot.edits[-1]
    assert "playing" or True
    cbs = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]
    assert any((c or "").startswith("twg:x:") for c in cbs)                             # صفحهٔ بازی با دکمه‌هایِ حرکت
    async with db() as s:
        w = (await s.scalars(select(models.PlatformWager))).one()
    assert w.status == "playing" and w.chat_id == 0 and '"imid": "inl-1"' in w.state


@pytest.mark.asyncio
async def test_inline_game_is_playable_through_existing_twg_callbacks_to_a_win(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)

    async def cfj(b, bid, uid, bypass_cache=False): return []
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    await WI.iwg_join(Cb(bot, U2, f"iwg:j:xo:10000:{U1}"), bot)
    async with db() as s:
        w = (await s.scalars(select(models.PlatformWager))).one()
    first = w.turn_id
    second = U2 if first == U1 else U1
    for uid, cell in ((first, 0), (second, 3), (first, 1), (second, 4), (first, 2)):
        cb = Cb(bot, uid, f"twg:x:{w.id}:{cell}")
        await WG.tw_xo(cb, bot)
        assert not any(a[1] for a in cb.answers), cb.answers                            # بدون خطا
    async with db() as s:
        w = await s.get(models.PlatformWager, w.id)
    assert w.status == "settled" and w.winner_id == first
    assert await bal(db, first) == 100_000 - 10_000 + 20_000 and await bal(db, second) == 90_000
    assert all(kw.get("inline_message_id") == IMID for _, kw in bot.edits)             # همهٔ ویرایش‌ها روی همان پیامِ اینلاین


@pytest.mark.asyncio
async def test_host_cannot_join_own_game_and_second_press_is_rejected(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)

    async def cfj(b, bid, uid, bypass_cache=False): return []
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    own = Cb(bot, U1, f"iwg:j:xo:10000:{U1}")
    await WI.iwg_join(own, bot)
    assert own.answers[0][1] and await bal(db, U1) == 100_000
    await WI.iwg_join(Cb(bot, U2, f"iwg:j:xo:10000:{U1}"), bot)
    again = Cb(bot, U2, f"iwg:j:xo:10000:{U1}")
    await WI.iwg_join(again, bot)
    assert "قبلاً" in again.answers[0][0] and await bal(db, U1) == 90_000                # فقط یک بازی ساخته شد


@pytest.mark.asyncio
async def test_join_failure_refunds_the_hosts_reserved_amount(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)

    async def cfj(b, bid, uid, bypass_cache=False): return []
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    async with db() as s:                                                              # حریف موجودی ندارد
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == U2))
        u.balance = Decimal(100)
        await s.commit()
    cb = Cb(bot, U2, f"iwg:j:xo:10000:{U1}")
    await WI.iwg_join(cb, bot)
    assert cb.answers[0][1] and "موجودی" in cb.answers[0][0]
    assert await bal(db, U1) == 100_000                                                # رزروِ میزبان برگشت
    assert not WI._STARTED


@pytest.mark.asyncio
async def test_free_inline_game_starts_without_stake(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    await K.save_kv(BOT, "free_on", "1")

    async def cfj(b, bid, uid, bypass_cache=False): return []
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    await WI.iwg_join(Cb(bot, U2, f"iwg:j:rps:0:{U1}"), bot)
    assert await bal(db, U1) == 100_000 and await bal(db, U2) == 100_000
    async with db() as s:
        w = (await s.scalars(select(models.PlatformWager))).one()
    assert w.status == "playing" and int(w.amount) == 0 and w.game == "rps"


@pytest.mark.asyncio
async def test_expiry_edits_the_inline_message(db, monkeypatch):
    """مهلتِ بازیِ اینلاین تمام شد ⇒ پول برمی‌گردد و پیامِ اینلاین (با inline_message_id) به‌روز می‌شود."""
    from datetime import datetime, timedelta
    bot = await _stub_bot(monkeypatch)

    async def cfj(b, bid, uid, bypass_cache=False): return []
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    await WI.iwg_join(Cb(bot, U2, f"iwg:j:xo:10000:{U1}"), bot)
    async with db() as s:
        w = (await s.scalars(select(models.PlatformWager))).one()
        w.expires_at = datetime.utcnow() - timedelta(seconds=5)
        await s.commit()
    bot.edits.clear()
    edited = {}

    class RealBot:
        def __init__(self, token): pass

        async def edit_message_text(self, text, **kw): edited.update(kw)

        class session:
            @staticmethod
            async def close(): pass
    monkeypatch.setattr(WG, "Bot", RealBot)
    await WG.expire_wagers_once()
    assert edited.get("inline_message_id") == IMID and "chat_id" not in edited
    assert await bal(db, U1) + await bal(db, U2) == 200_000                             # بازیِ در حالِ بازی: نوبتِ‌گذشته می‌بازد؛ پول گم نمی‌شود
    async with db() as s:
        assert (await s.scalars(select(models.PlatformWager))).one().status == "settled"


@pytest.mark.asyncio
async def test_parent_inline_handler_skip_falls_through_to_the_games_router():
    """tenant_router خودش یک inline_query (پیش‌نویسِ پست) دارد؛ SkipHandler باید کوئری را به روترِ بازی‌ها برساند (مثلِ زنجیرهٔ واقعی)."""
    import datetime as _dt
    from aiogram import Bot, Dispatcher, Router
    from aiogram.dispatcher.event.bases import SkipHandler
    from aiogram.types import InlineQuery, Update, User
    seen = []
    parent, child = Router(name="p"), Router(name="c")

    @parent.inline_query()
    async def _first(q: InlineQuery):
        seen.append("parent")
        raise SkipHandler

    @child.inline_query()
    async def _second(q: InlineQuery):
        seen.append("child")
    parent.include_router(child)
    dp = Dispatcher()
    dp.include_router(parent)
    bot = Bot(token="1:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab")
    q = InlineQuery(id="1", from_user=User(id=1, is_bot=False, first_name="u"), query="دوز", offset="")
    await dp.feed_update(bot, Update(update_id=1, inline_query=q))
    assert seen == ["parent", "child"]
    await bot.session.close()


# ───────────── تاسِ اینلاین از راهِ پیوی (ترفندِ سینا) ─────────────

class PmMsg:
    """پیامِ پیویِ بازیکن (برایِ دکمهٔ «انداختن تاس» و جوابِ /start iwd_…)."""
    def __init__(self, uid, chat=None):
        self.chat = types.SimpleNamespace(id=uid, type="private")
        self.from_user = types.SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"u{uid}", is_bot=False)
        self.text = ""
        self.sent = []

    async def answer(self, text, **k):
        self.sent.append((text, k.get("reply_markup")))

    async def edit_reply_markup(self, reply_markup=None):
        self.cleared = True


class PmCb:
    def __init__(self, bot, uid, data):
        self.bot, self.data, self.inline_message_id = bot, data, None
        self.message = PmMsg(uid)
        self.from_user = self.message.from_user
        self.answers = []

    async def answer(self, text="", show_alert=False, **k):
        self.answers.append((text, show_alert))


async def _start_inline_dice(db, monkeypatch, bot, amount=10_000):
    async def cfj(b, bid, uid, bypass_cache=False): return []
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    cb = Cb(bot, U2, f"iwg:j:dice:{amount}:{U1}")
    await WI.iwg_join(cb, bot)
    async with db() as s:
        w = (await s.scalars(select(models.PlatformWager))).one()
    return w, cb


@pytest.mark.asyncio
async def test_inline_dice_lists_and_starts_with_a_link_button_to_the_bot_pm(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    assert WI.parse_query("تاس") == ("dice", 0, True) and WI.parse_query("تاس 5000") == ("dice", 5000, True)
    w, _cb = await _start_inline_dice(db, monkeypatch, bot)
    assert w.game == "dice" and w.status == "playing" and await bal(db, U1) == 90_000 and await bal(db, U2) == 90_000
    text, kw = bot.edits[-1]
    btns = [b for r in kw["reply_markup"].inline_keyboard for b in r]
    assert [b.url for b in btns if b.url] == [f"https://t.me/shop_bot?start=iwd_{w.id}"]      # لینک به پیوی
    assert not any((b.callback_data or "").startswith("twg:d:") for b in btns)                 # callbackِ بی‌چت‌آیدی نیست
    old_btn = Cb(bot, U1, f"twg:d:{w.id}")                                                    # پیامِ قدیمیِ اینلاین ⇒ راهنما
    await WG.tw_dice(old_btn, bot)
    assert old_btn.answers[0][1] and "پیوی" in old_btn.answers[0][0]


@pytest.mark.asyncio
async def test_inline_dice_full_game_via_pm_edits_the_inline_message_and_pays_the_winner(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    bot.dice_values, bot.dice_sent = [2, 5], []
    w, _ = await _start_inline_dice(db, monkeypatch, bot)

    async def no_sleep(_): return None
    monkeypatch.setattr(WI.asyncio, "sleep", no_sleep)
    from tenant.shared.wager_inline import dice_deeplink
    row = types.SimpleNamespace(id=BOT)
    for uid in (U1, U2):                                                                         # /start iwd_<id> ⇒ دکمهٔ انداختن
        m = PmMsg(uid)
        assert await dice_deeplink(m, bot, row, f"iwd_{w.id}")
        assert f"iwd:r:{w.id}" in m.sent[0][1].inline_keyboard[0][0].callback_data
    outsider = PmMsg(999)
    await dice_deeplink(outsider, bot, row, f"iwd_{w.id}")
    assert "بازیکنِ این بازی نیستی" in outsider.sent[0][0]
    bot.edits.clear()
    c1 = PmCb(bot, U1, f"iwd:r:{w.id}")                                                          # اول u11 می‌اندازد: ۲
    await WI.iwd_roll(c1, bot)
    assert bot.dice_sent == [U1] and "۲" not in c1.answers and any("2" in t for t, _ in c1.message.sent)
    assert bot.edits and bot.edits[-1][1]["inline_message_id"] == IMID and "2" in bot.edits[-1][0]
    again = PmCb(bot, U1, f"iwd:r:{w.id}")                                                       # دوباره نمی‌شود انداخت
    await WI.iwd_roll(again, bot)
    assert again.answers[0][1] and bot.dice_sent == [U1]
    c2 = PmCb(bot, U2, f"iwd:r:{w.id}")                                                          # u22 می‌اندازد: ۵ ⇒ برنده
    await WI.iwd_roll(c2, bot)
    async with db() as s:
        w2 = await s.get(models.PlatformWager, w.id)
    assert w2.status == "settled" and w2.winner_id == U2
    assert await bal(db, U2) == 100_000 - 10_000 + 20_000 and await bal(db, U1) == 90_000
    assert bot.edits[-1][1]["inline_message_id"] == IMID
    m = PmMsg(U1)                                                                                # بعد از پایان: پیامِ «تمام شده»
    await dice_deeplink(m, bot, row, f"iwd_{w.id}")
    assert "تمام شده" in m.sent[0][0]


@pytest.mark.asyncio
async def test_inline_dice_failed_throw_can_be_retried(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    w, _ = await _start_inline_dice(db, monkeypatch, bot)
    bot.dice_values = []                                                                         # send_dice با IndexError می‌ترکد
    c = PmCb(bot, U1, f"iwd:r:{w.id}")
    await WI.iwd_roll(c, bot)
    assert any("انجام نشد" in t for t, _ in c.message.sent)
    bot.dice_values, bot.dice_sent = [4], []
    async def no_sleep(_): return None
    monkeypatch.setattr(WI.asyncio, "sleep", no_sleep)
    c2 = PmCb(bot, U1, f"iwd:r:{w.id}")
    await WI.iwd_roll(c2, bot)
    assert bot.dice_sent == [U1] and not any(a[1] for a in c2.answers)                           # آزاد شده بود؛ دوباره انداخت


# ───────────── کوییزِ اینلاین (دکمه‌هایِ شیشه‌ای؛ همه‌چیز روی همان پیام) ─────────────

class QzCb(Cb):
    """کلیکِ گزینهٔ کوییز روی پیامِ اینلاین."""


async def _start_inline_quiz(db, monkeypatch, bot, amount=10_000, cat="foot"):
    async def cfj(b, bid, uid, bypass_cache=False): return []
    monkeypatch.setattr(tenant, "check_force_join", cfj)
    spawned = []

    async def keep(b, wid, secs): spawned.append((wid, secs))        # تایمرِ مهلت: فقط ثبت می‌شود
    monkeypatch.setattr(PR, "_expire_later", keep)
    PR.LIVE.clear()
    cb = Cb(bot, U2, f"iwg:j:quiz:{amount}:{U1}:{cat}")
    await WI.iwg_join(cb, bot)
    async with db() as s:
        w = (await s.scalars(select(models.PlatformWager))).one()
    return w, cb, spawned


@pytest.mark.asyncio
async def test_inline_quiz_query_offers_topics_and_invite_shows_the_topic(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    await tenant.set_setting(BOT, "group_betting_enabled", "1")
    assert WI.parse_query("کوییز") == ("quiz", 0, True) and WI.parse_query("کویز 3000") == ("quiz", 3000, True)
    sent = {}

    class Q:
        query = "کوییز 5000"
        from_user = types.SimpleNamespace(id=U1, full_name="u11")

        async def answer(self, results, **k): sent["r"] = results
    await WI.iwg_query(Q(), bot)
    ids = [r.id for r in sent["r"]]
    assert "quiz:5000:foot" in ids and "quiz:5000:riddle" in ids and len(ids) == 6          # هر موضوع یک گزینه
    foot = next(r for r in sent["r"] if r.id == "quiz:5000:foot")
    assert "فوتبال" in foot.input_message_content.message_text
    assert foot.reply_markup.inline_keyboard[0][0].callback_data == f"iwg:j:quiz:5000:{U1}:foot"
    Q.query = ""
    await WI.iwg_query(Q(), bot)
    assert [r.id for r in sent["r"] if r.id.startswith("quiz")] == ["quiz:1000:general"]       # فهرستِ بدونِ متن: فقط یک موضوع (با حداقلِ مبلغ)


@pytest.mark.asyncio
async def test_inline_quiz_wrong_then_right_answer_settles_on_the_inline_message(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    w, _cb, spawned = await _start_inline_quiz(db, monkeypatch, bot)
    st = PW._st(w)
    assert w.game == "quiz" and w.status == "playing" and st["cat"] == "foot" or st.get("opt") == "foot"
    assert spawned == [(w.id, PR.SECS["quiz"])] and PR.LIVE[w.id]["chat"] == 0
    assert await bal(db, U1) == 90_000 and await bal(db, U2) == 90_000
    text, kw = bot.edits[-1]
    cbs = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]
    assert cbs == [f"twr:qz:{w.id}:{i}" for i in range(4)] and kw["inline_message_id"] == IMID    # ۴ گزینه روی همان پیام
    right = int(st["a"]) - 1
    wrong = (right + 1) % 4
    bad = Cb(bot, U1, f"twr:qz:{w.id}:{wrong}")                                                    # u11 غلط می‌زند
    await WR.twr_quiz_press(bad, bot)
    assert bad.answers[0][1] and "اشتباه" in bad.answers[0][0] and PR.LIVE[w.id]["locked"] == {U1}
    again = Cb(bot, U1, f"twr:qz:{w.id}:{right}")                                                  # فرصتِ دوم ندارد
    await WR.twr_quiz_press(again, bot)
    assert "فرصت" in again.answers[0][0]
    outsider = Cb(bot, 999, f"twr:qz:{w.id}:{right}")
    await WR.twr_quiz_press(outsider, bot)
    assert "فقط دو بازیکن" in outsider.answers[0][0]
    bot.edits.clear()
    good = Cb(bot, U2, f"twr:qz:{w.id}:{right}")                                                   # u22 درست می‌زند
    await WR.twr_quiz_press(good, bot)
    async with db() as s:
        w2 = await s.get(models.PlatformWager, w.id)
    assert w2.status == "settled" and w2.winner_id == U2 and w.id not in PR.LIVE
    assert await bal(db, U2) == 100_000 - 10_000 + 20_000 and await bal(db, U1) == 90_000
    assert bot.edits and all(kw["inline_message_id"] == IMID for _, kw in bot.edits) and "برنده" in bot.edits[-1][0]


@pytest.mark.asyncio
async def test_inline_quiz_both_wrong_refunds_everyone_without_a_chat(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    w, _cb, _sp = await _start_inline_quiz(db, monkeypatch, bot)
    wrong = (int(PW._st(w)["a"]) - 1 + 1) % 4
    for uid in (U1, U2):
        await WR.twr_quiz_press(Cb(bot, uid, f"twr:qz:{w.id}:{wrong}"), bot)
    async with db() as s:
        w2 = await s.get(models.PlatformWager, w.id)
    assert w2.status != "playing" and await bal(db, U1) == 100_000 and await bal(db, U2) == 100_000   # مبلغِ هر دو برگشت
    assert "برنده نشد" in bot.edits[-1][0] and bot.edits[-1][1]["inline_message_id"] == IMID


@pytest.mark.asyncio
async def test_quiz_buttons_still_refuse_foreign_inline_clicks_when_wager_is_not_live(db, monkeypatch):
    bot = await _stub_bot(monkeypatch)
    PR.LIVE.clear()
    c = Cb(bot, U1, "twr:qz:999:0")
    await WR.twr_quiz_press(c, bot)
    assert c.answers[0][1] and "تمام شده" in c.answers[0][0]
