"""بازی‌های دونفرهٔ اینلاین (سینا): `@ربات دوز` در هر چتی ⇒ دعوت به بازی ⇒ پیوستنِ حریف ⇒ بازی داخلِ همان پیام.

=== PATCH_WAGER_INLINE_V1 ===
• فقط دوز و سنگ‌کاغذ‌قیچی: داخلِ پیامِ اینلاین فقط دکمه داریم؛ تاس به `send_dice` و چتِ مشخص نیاز دارد (پیامِ اینلاین
  آیدیِ چت ندارد) و مسابقه‌هایِ نوشتاری (سرعت، ریاضی، …) به خواندنِ پیام‌هایِ گروه.
• بدونِ متن: «💰 موجودی» اولین گزینه است، بعد بازی‌ها با توضیح. «دوز» ⇒ رایگان (اگر مالک «بازیِ رایگان» را روشن کرده)،
  «دوز ۵۰۰۰» ⇒ پولی. فقط پیامِ دعوت فرستاده می‌شود؛ هیچ پولی تا لحظهٔ پیوستنِ حریف حرکت نمی‌کند.
• هنگامِ پیوستن: عضویتِ اجباریِ هر دو نفر چک می‌شود؛ پیام با «فلانی ✅ / فلانی ❌» و دکمهٔ کانال‌ها + «✅ عضو شدم»
  ویرایش می‌شود و وقتی هر دو عضو بودند، بازی (همان هستهٔ platform_wager) ساخته و همان پیام به صفحهٔ بازی تبدیل می‌شود.
• حرکت‌ها با همان callbackهایِ twg:* انجام می‌شوند (wager_games: `_show`/`_cb_row` از inline_message_id پشتیبانی می‌کنند).
• شرط: حالتِ اینلاین در @BotFather (/setinline) روشن باشد (از API قابلِ‌روشن‌شدن نیست).
"""
from __future__ import annotations

import asyncio
import html as _h
import json
import re
import time

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
                           InlineQueryResultArticle, InputTextMessageContent)
from sqlalchemy import select

import platform_wager as core
from bot_addons import WAGER_ADDON, addon_active, wager_eligible
from config import Session
from logging_setup import get_logger as _get_logger
from models import BotUser, PlatformWager

logger = _get_logger(__name__)

wager_inline_router = Router(name="wager_inline")

TYPED_GAMES = ("speed", "math", "pattern", "scramble", "emoji")     # PATCH_INLINE_TYPED_V1 (سینا): جواب در پیوی ربات
INLINE_GAMES = ("xo", "rps", "dice", "quiz") + TYPED_GAMES
_NAMES = {"xo": ("دوز", "xo", "اکس او", "ایکس او"), "rps": ("سنگ کاغذ قیچی", "rps"), "dice": ("تاس", "dice"), "quiz": ("کوییز", "کویز", "کوئیز", "quiz"),
          "speed": ("سرعت",), "math": ("ریاضی",), "pattern": ("الگو",), "scramble": ("درهم",),
          "emoji": ("حدس ایموجی", "ایموجی")}
_TITLE = {"xo": "❌⭕ دوز", "rps": "🪨📄✂️ سنگ کاغذ قیچی", "dice": "🎲 تاس", "quiz": "❓ کوییز", "speed": "⚡️ سرعت", "math": "🧮 ریاضی",
          "pattern": "🧩 الگو", "scramble": "🔀 درهم", "emoji": "🤔 حدس ایموجی"}
_DESC = {"xo": "هرکی یه خط سه‌تایی بسازه برنده‌ست.",
         "rps": "سنگ قیچی رو می‌شکنه، قیچی کاغذ رو می‌بره، کاغذ سنگ رو می‌پیچه. ۳ راندی؛ هرکی زودتر ۲ تا ببره برنده‌ست.",
         "dice": "هر نفر یک تاس می‌اندازد؛ عددِ بزرگ‌تر برنده است. پرتاب داخلِ پیویِ ربات انجام می‌شود.",
         "quiz": "یک سؤال با ۴ گزینه؛ هر کس زودتر گزینهٔ درست را بزند برنده است (هر نفر یک فرصت؛ غلط = حذف).",
         "speed": "یک متن نمایش داده می‌شود؛ هر کس زودتر دقیق در پیوی ربات بنویسد برنده است.",
         "math": "یک حساب ساده؛ هر کس زودتر جواب را در پیوی ربات بفرستد برنده است.",
         "pattern": "عدد بعدی دنباله را زودتر از حریف در پیوی ربات بفرست.",
         "scramble": "حروف یک کلمه درهم شده؛ هر کس زودتر کلمه را در پیوی ربات بنویسد برنده است.",
         "emoji": "چند ایموجی؛ هر کس زودتر بگوید چه کلمه‌ای است (در پیوی ربات) برنده است."}
_AMT = r"([۰-۹0-9,،]+)"
def _alt(names) -> str:
    """نام‌هایِ بازی ⇒ الگوی regex (فاصله ⇒ \\s*). جدا از f-string است چون پایتونِ ۳٫۱۰ بک‌اسلش داخلِ f-string را نمی‌پذیرد."""
    parts = []
    for n in names:
        parts.append(r"\s*".join(re.escape(x) for x in n.split()))
    return "|".join(parts)


_RX = {g: re.compile(r"(?:بازی\s+)?(?:" + _alt(names) + r")(?:\s+" + _AMT + r")?", re.IGNORECASE)
       for g, names in _NAMES.items()}
_locks: dict[str, asyncio.Lock] = {}
_STARTED: dict[str, bool] = {}          # inline_message_id ⇒ بازی ساخته شد (جلویِ دو پیوستنِ هم‌زمان)


def _digits(t: str) -> str:
    return t.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789", "٬،,"))


def parse_query(text: str | None) -> tuple[str | None, int, bool]:
    """(بازی | None, مبلغ، دقیق‌بودنِ تطبیق). «دوز ۵۰۰۰» ⇒ ('xo', 5000, True)؛ «دو» ⇒ پیش‌وندِ نام ⇒ ('xo', 0, False)."""
    t = core._norm(text or "")
    if not t:
        return None, 0, False
    for g, rx in _RX.items():
        m = rx.fullmatch(t)
        if m:
            amt = int(_digits(m.group(1))) if m.group(1) else 0
            return g, amt, True
    first = t.split()[0].lower()
    for g, names in _NAMES.items():
        if any(n.lower().replace(" ", "").startswith(first.replace(" ", "")) for n in names):
            return g, 0, False
    return None, 0, False


async def _ctx(bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not wager_eligible(row.field, row.plan) or not await addon_active(row.id, WAGER_ADDON) \
            or not await core.master_enabled(row.id):
        return None
    return row


def _invite_text(game: str, amount: int, name: str, free: bool, cat: str = "") -> str:
    topic = ""
    if cat:
        import quiz_bank as QB
        topic = f"🏷 موضوع: <b>{_h.escape(QB.CAT_NAME.get(cat, cat))}</b>\n"
    money = ("🆓 <b>بازیِ رایگان</b> — جایزهٔ پولی ندارد" if free
             else f"💰 مبلغ ورودی هر نفر: <b>{core._rial(amount)}</b>")
    return (f"🎮 <b>{_h.escape(_TITLE[game])}</b>\n{_DESC[game]}\n\n{topic}{money}\n"
            f"👤 میزبان: <b>{_h.escape(name)}</b>\n\nبرای بازی، دکمهٔ زیر را بزن 👇")


def _join_kb(game: str, amount: int, creator: int, cat: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ پیوستن به بازی", callback_data=f"iwg:j:{game}:{amount}:{creator}" + (f":{cat}" if cat else ""),
                              style=ButtonStyle.SUCCESS)],
        # PATCH_INLINE_CANCEL_V1 (سینا): میزبان پیش از پیوستن حریف می‌تواند لغو کند (هنوز پولی جابه‌جا نشده)
        [InlineKeyboardButton(text="❌ لغو بازی (فقط میزبان)", callback_data=f"iwg:x:{creator}", style=ButtonStyle.DANGER)]])


async def _balance(bot_id: int, uid: int) -> int:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == uid))
    try:
        return int(u.balance or 0) if u else 0
    except Exception:  # noqa: BLE001
        return 0


def _no_balance_result(rid: str, title: str, need: int, have: int, uname: str | None):
    """PATCH_INLINE_BALANCE_CHECK_V1 (سینا): میزبانِ بی‌موجودی دعوتِ پولی نمی‌فرستد؛ پیامِ «موجودی کافی نیست» با دکمهٔ شارژ."""
    kb = None
    if uname:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="➕ شارژ موجودی در ربات", url=f"https://t.me/{uname}?start=iwc", style=ButtonStyle.SUCCESS)]])
    return InlineQueryResultArticle(
        id=rid, title="⚠️ " + title + " — موجودی کافی نیست",
        description=f"موجودی تو {core._rial(have)}؛ برای این بازی {core._rial(need)} لازم است.",
        input_message_content=InputTextMessageContent(
            message_text=(f"⚠️ <b>موجودی کافی نیست</b>\n\nبرای ساختن این بازی {core._rial(need)} لازم است و موجودی فعلی "
                          f"{core._rial(have)} است.\nاول موجودی را شارژ کن، بعد دوباره بازی را بساز."),
            parse_mode="HTML"),
        reply_markup=kb)


def _topics(game: str, exact: bool) -> list[str]:
    """موضوع‌هایِ پیشنهادیِ کوییز: «کوییز» تایپ شده ⇒ همهٔ موضوع‌ها؛ فهرستِ بدونِ متن ⇒ فقط «اطلاعات عمومی»."""
    if game != "quiz":
        return [""]
    import quiz_bank as QB
    return list(QB.CATS) if exact else ["general"]


@wager_inline_router.inline_query()
async def iwg_query(q: InlineQuery, bot: Bot):
    """فهرستِ اینلاین: «💰 موجودی» + بازی‌ها. برایِ رباتی که بازیِ دونفره ندارد، نتیجهٔ خالی (و پاسخِ دادن به کوئری)."""
    row = await _ctx(bot)
    if row is None:
        await q.answer([], cache_time=0, is_personal=True)
        return
    import wager_rank as K
    fc = await K.free_cfg(row.id)
    cfg = await core.load_cfg(row.id)
    game, amount, exact = parse_query(q.query)
    name = q.from_user.full_name or "—"
    results = []
    have = await _balance(row.id, q.from_user.id)
    try:
        uname = await _username(bot)
    except Exception:  # noqa: BLE001
        uname = None
    if not (q.query or "").strip():
        results.append(InlineQueryResultArticle(
            id="bal", title="💰 موجودی", description="نمایشِ موجودیِ کیف پولت (فقط خودت می‌بینی)",
            input_message_content=InputTextMessageContent(message_text="💰 <b>موجودیِ کیف پول</b>\nبرای دیدنِ موجودیِ خودت دکمهٔ زیر را بزن.",
                                                          parse_mode="HTML"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="👁 موجودیِ من", callback_data="iwg:bal", style=ButtonStyle.PRIMARY)]])))
    for g in INLINE_GAMES:
        if game and g != game:
            continue
        if not await core.game_enabled(g, row.id):
            continue
        offers: list[int] = []
        if exact and amount:
            offers = [amount]
        else:
            if fc["on"]:
                offers.append(0)
            if not exact:
                offers.append(int(cfg["min"]))
        for cat in _topics(g, bool(game)):
            for amt in offers:
                if amt and (amt < int(cfg["min"]) or (cfg.get("max") and amt > int(cfg["max"]))):
                    continue
                free = amt == 0
                tn = ""
                if cat:
                    import quiz_bank as QB
                    tn = " — " + QB.CATS.get(cat, cat)
                title = _TITLE[g] + tn + (" — رایگان" if free else f" — {core._rial(amt)}")
                if not free and have < amt:
                    results.append(_no_balance_result(f"{g}:{amt}" + (f":{cat}" if cat else "") + ":nb",
                                                      _TITLE[g] + tn + f" — {core._rial(amt)}", amt, have, uname))
                    continue
                results.append(InlineQueryResultArticle(
                    id=f"{g}:{amt}" + (f":{cat}" if cat else ""), title=title,
                    description=(_DESC[g] if free else _DESC[g] + f"  (برایِ مبلغِ دیگر بنویس: {_NAMES[g][0]} ۵۰۰۰)"),
                    input_message_content=InputTextMessageContent(
                        message_text=_invite_text(g, amt, name, free, cat), parse_mode="HTML"),
                    reply_markup=_join_kb(g, amt, q.from_user.id, cat)))
    await q.answer(results, cache_time=0, is_personal=True)


@wager_inline_router.callback_query(F.data == "iwg:bal")
async def iwg_balance(cb: CallbackQuery, bot: Bot):
    from tenant import get_or_create_user
    row = await _ctx(bot)
    if row is None:
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return
    u = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
    await cb.answer(f"💰 موجودیِ تو: {core._rial(u.balance)}", show_alert=True)


async def _name_of(bot_id: int, uid: int) -> str:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == uid))
    return (u.full_name if u and u.full_name else None) or "میزبان"


def _link(ch: dict) -> str | None:
    cid, invite = str(ch["chat_id"]), ch.get("invite_link")
    if invite:
        return invite
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}"
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}"
    return None


async def _edit(bot: Bot, imid: str, text: str, kb) -> None:
    try:
        await bot.edit_message_text(text, inline_message_id=imid, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001 — بدون تغییر/پیامِ پاک‌شده
        if "not modified" not in str(e).lower():
            logger.info("wager inline: edit skipped: %s", e)


@wager_inline_router.callback_query(F.data.startswith("iwg:j:"))
async def iwg_join(cb: CallbackQuery, bot: Bot):
    from tenant import check_force_join, get_or_create_user
    imid = cb.inline_message_id
    if not imid:
        await cb.answer("این دکمه فقط در پیامِ اینلاین کار می‌کند.", show_alert=True)
        return
    row = await _ctx(bot)
    if row is None:
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return
    try:
        parts = cb.data.split(":")
        game, amount, creator = parts[2], int(parts[3]), int(parts[4])
        cat = parts[5] if len(parts) > 5 else ""
    except (ValueError, IndexError):
        await cb.answer("دکمه نامعتبر است.", show_alert=True)
        return
    if game == "quiz":
        import quiz_bank as QB
        if cat not in QB.CATS:
            cat = "general"
    if game not in INLINE_GAMES or not await core.game_enabled(game, row.id):
        await cb.answer("این بازی فعال نیست.", show_alert=True)
        return
    joiner = cb.from_user
    if joiner.id == creator:
        await cb.answer("منتظر حریف باش؛ میزبان نمی‌تواند خودش به بازی بپیوندد.", show_alert=True)
        return
    free = amount == 0
    cfg = await core.load_cfg(row.id)
    import wager_rank as K
    fc = await K.free_cfg(row.id)
    if free and not fc["on"]:
        await cb.answer("بازیِ رایگان فعال نیست.", show_alert=True)
        return
    if not free and (amount < int(cfg["min"]) or (cfg.get("max") and amount > int(cfg["max"]))):
        await cb.answer("مبلغِ این بازی مجاز نیست.", show_alert=True)
        return
    cre_name = await _name_of(row.id, creator)
    miss_c = await fj_missing(bot, row.id, creator)          # PATCH_INLINE_FJ_V1: کانال‌های مخصوص بازی اینلاین
    miss_j = await fj_missing(bot, row.id, joiner.id)
    if miss_c or miss_j:
        chans: dict = {}
        for ch in (miss_c or []) + (miss_j or []):
            chans.setdefault(ch["chat_id"], ch)
        rows = []
        for ch in chans.values():
            url = _link(ch)
            btn = {"text": f"📢 {ch['title']}"}
            if url:
                btn["url"] = url
            else:
                btn["callback_data"] = "twg:noop"
            rows.append([InlineKeyboardButton(**btn)])
        rows.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data=cb.data, style=ButtonStyle.SUCCESS)])
        text = ("⚠️ برای شروعِ بازی هر دو نفر باید در کانال‌هایِ زیر عضو باشند:\n\n"
                f"{_h.escape(cre_name)}: {'❌' if miss_c else '✅'}\n"
                f"{_h.escape(joiner.full_name or '—')}: {'❌' if miss_j else '✅'}\n\n"
                "بعد از عضویت، «✅ عضو شدم» را بزن.")
        await _edit(bot, imid, text, InlineKeyboardMarkup(inline_keyboard=rows))
        await cb.answer("اول در کانال‌ها عضو شو.", show_alert=True)
        return
    lock = _locks.setdefault(imid, asyncio.Lock())
    async with lock:
        if _STARTED.get(imid):
            await cb.answer("این بازی قبلاً شروع شده.", show_alert=True)
            return
        async with Session() as s:
            cre_u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == creator))
        if cre_u is None:
            await cb.answer("میزبان هنوز ربات را استارت نکرده؛ از او بخواه یک بار /start بزند.", show_alert=True)
            return
        await get_or_create_user(row.id, joiner.id, joiner.full_name or "", joiner.username or "")
        ctuple = (creator, cre_u.full_name or "", cre_u.username or "")
        if free:
            w, err = await core.create_free_wager(game, 0, ctuple, None, cfg["ttl"], fc["fee"], fc["limit"], row.id)
        else:
            w, err = await core.create_wager(game, 0, ctuple, amount, None, cfg, bot_id=row.id)
        if err:
            await cb.answer(err.replace("<b>", "").replace("</b>", ""), show_alert=True)
            return
        try:
            if game in ("xo", "dice", "quiz"):                           # دوز: X؛ تاس: عدد بزرگ‌تر؛ کوییز: موضوع
                o = await core.set_option(w.id, creator, {"xo": "X", "dice": "hi"}.get(game, cat), bot_id=row.id)
                if o.err:
                    raise RuntimeError(o.err)
            if game == "quiz" or game in TYPED_GAMES:
                import platform_wager_race as R
                r = await R.join_race(w.id, (joiner.id, joiner.full_name or "", joiner.username or ""), row.id)
            else:
                r = await core.join_wager(w.id, (joiner.id, joiner.full_name or "", joiner.username or ""), cfg["move"],
                                          bot_id=row.id, sim_secs=cfg["move_sim"])
            if r.err:
                async with core._wlock(w.id):
                    await core.cancel_wager(w.id, creator, row.id)         # مبلغِ رزروشدهٔ میزبان برمی‌گردد
                await cb.answer(r.err, show_alert=True)
                return
        except Exception:  # noqa: BLE001
            logger.exception("wager inline: start failed (bot=%s)", row.id)
            async with core._wlock(w.id):
                await core.cancel_wager(w.id, creator, row.id)
            await cb.answer("ساخت بازی با خطا روبه‌رو شد؛ دوباره امتحان کن.", show_alert=True)
            return
        try:
            async with Session() as s:
                wr = await s.get(PlatformWager, w.id)
                st = json.loads(wr.state or "{}") if wr.state else {}
                st["imid"] = imid                       # برایِ ویرایشِ پیام هنگامِ انقضا (بی‌چت‌آیدی)
                wr.state = json.dumps(st)
                await s.commit()
        except Exception:  # noqa: BLE001 — فقط ویرایشِ پیامِ انقضا از دست می‌رود؛ بازی و پول سالم‌اند
            logger.exception("wager inline #%s: saving the inline id failed", w.id)
        _STARTED[imid] = True
        if len(_STARTED) > 5000:
            _STARTED.clear()
        if game == "quiz" or game in TYPED_GAMES:           # مسابقه‌ها «زنده» در حافظه ثبت می‌شوند؛ مهلت هم با تایمر
            import platform_wager_race as R
            qst = core._st(r.w)
            R.LIVE[r.w.id] = {"wid": r.w.id, "chat": 0, "bot": row.id, "kind": r.w.game,
                              "players": {r.w.creator_id, r.w.opponent_id}, "answer": qst.get("a"), "n": qst.get("n"),
                              "st": qst, "alts": qst.get("alts"), "until": time.monotonic() + R.SECS[r.w.game] + 5}
            asyncio.create_task(R._expire_later(bot, r.w.id, R.SECS[r.w.game]))
        text, kb = core.render(r.w)
        kb = await inline_kb(bot, r.w, kb)
        await _edit(bot, imid, text, kb)
    await cb.answer("به بازی پیوستی؛ موفق باشی!")


# ───────────────────────── تاس از راهِ پیویِ ربات (ترفندِ سینا) ─────────────────────────
# پیامِ اینلاین آیدیِ چت ندارد و ربات نمی‌تواند در آن تاس بفرستد. پس زیرِ پیام یک دکمهٔ «لینک» می‌آید که بازیکن را به
# پیویِ ربات می‌برد (`/start iwd_<شناسهٔ بازی>`)؛ آنجا دکمهٔ «🎲 انداختن تاس» می‌آید، ربات تاس را در پیوی می‌فرستد،
# نتیجه را ثبت می‌کند و همان پیامِ اینلاین را (با inline_message_id) ویرایش می‌کند.
_UNAME: dict[str, str] = {}


async def _username(bot: Bot) -> str | None:
    tok = getattr(bot, "token", "") or ""
    if tok not in _UNAME:
        me = await bot.me()
        if not me.username:
            return None
        _UNAME[tok] = me.username
    return _UNAME[tok]


async def inline_kb(bot: Bot, w: PlatformWager, kb):
    """کیبوردِ بازیِ اینلاین: دکمهٔ «پرتاب تاس» (callback) ⇒ دکمهٔ لینک به پیویِ ربات. بقیهٔ بازی‌ها دست‌نخورده."""
    if w.game in TYPED_GAMES and w.status == "playing":          # PATCH_INLINE_TYPED_V1
        try:
            uname = await _username(bot)
        except Exception:  # noqa: BLE001
            uname = None
        if uname:
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="✍️ ارسال جواب (در پیوی ربات)", url=f"https://t.me/{uname}?start=iwt_{w.id}", style=ButtonStyle.SUCCESS)]])
        return kb
    if kb is None or w.game != "dice" or w.status != "playing":
        return kb
    try:
        uname = await _username(bot)
    except Exception:  # noqa: BLE001 — بدونِ نامِ کاربریِ ربات لینک نمی‌شود؛ همان کیبورد می‌ماند
        logger.exception("wager inline: bot username lookup failed")
        return kb
    if not uname:
        return kb
    rows = []
    for r in kb.inline_keyboard:
        rows.append([InlineKeyboardButton(text="🎲 انداختن تاس (در پیویِ ربات)", url=f"https://t.me/{uname}?start=iwd_{w.id}",
                                          style=ButtonStyle.SUCCESS)
                     if (b.callback_data or "").startswith("twg:d:") else b for b in r])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _roll_kb(wid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🎲 انداختن تاس", callback_data=f"iwd:r:{wid}", style=ButtonStyle.SUCCESS)]])


async def dice_deeplink(msg, bot: Bot, row, payload: str) -> bool:
    """`/start iwd_<id>` در پیوی. True ⇒ پیام مصرف شد."""
    from tenant import get_or_create_user
    try:
        wid = int(payload[4:])
    except ValueError:
        return False
    if await _ctx(bot) is None:
        await msg.answer("این قابلیت فعال نیست.")
        return True
    u = await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    if getattr(u, "is_banned", False):
        await msg.answer("شما از این ربات مسدود شده‌اید.")
        return True
    async with Session() as s:
        w = await s.get(PlatformWager, wid)
    uid = msg.from_user.id
    if not w or (w.bot_id or 0) != row.id or w.game != "dice":
        await msg.answer("❌ این بازی پیدا نشد.")
    elif uid not in (w.creator_id, w.opponent_id):
        await msg.answer("❌ تو بازیکنِ این بازی نیستی.")
    elif w.status != "playing":
        await msg.answer("⌛ این بازی تمام شده است؛ نتیجه را در همان پیامِ بازی ببین.")
    elif str((core._st(w).get("rolls") or {}).get(str(uid), "")):
        await msg.answer(f"🎲 تاسِ تو را انداخته‌ای: <b>{core._st(w)['rolls'][str(uid)]}</b>\nمنتظرِ حریف بمان؛ نتیجه در پیامِ بازی اعلام می‌شود.",
                         parse_mode="HTML")
    else:
        await msg.answer(f"🎲 <b>نوبتِ پرتابِ تو</b>\n\n{core._rule_line(w)}\n\nدکمهٔ زیر را بزن؛ ربات همین‌جا برایت تاس می‌اندازد.",
                         reply_markup=_roll_kb(wid), parse_mode="HTML")
    return True


@wager_inline_router.callback_query(F.data.startswith("iwd:r:"))
async def iwd_roll(cb: CallbackQuery, bot: Bot):
    row = await _ctx(bot)
    if row is None or cb.message is None:
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return
    try:
        wid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer("دکمه نامعتبر است.", show_alert=True)
        return
    uid = cb.from_user.id
    async with core._wlock(wid):
        r = await core.dice_claim(wid, uid, bot_id=row.id)
    if r.err:
        await cb.answer(r.err, show_alert=True)
        return
    await cb.answer("🎲")
    try:
        sent = await bot.send_dice(cb.message.chat.id, emoji="🎲")
        value = int(sent.dice.value)
    except Exception:  # noqa: BLE001 — پرتاب نشد: دوباره اجازه بده
        logger.exception("wager inline dice #%s: send_dice failed", wid)
        await core.dice_release(wid, uid, bot_id=row.id)
        await cb.message.answer("❌ پرتاب تاس انجام نشد؛ دوباره امتحان کن.", reply_markup=_roll_kb(wid))
        return
    async with core._wlock(wid):
        r = await core.dice_record(wid, uid, value, bot_id=row.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)       # دکمه یک‌بارمصرف است
    except Exception as e:  # noqa: BLE001
        logger.info("wager inline dice #%s: keyboard removal skipped: %s", wid, e)
    if r.w is None:
        return
    imid = core._st(r.w).get("imid")
    done = r.w.status != "playing"
    await asyncio.sleep(4)                                          # بگذار انیمیشنِ تاس تمام شود
    try:
        await cb.message.answer(f"🎲 تاسِ تو: <b>{value}</b>\n" + ("نتیجه در پیامِ بازی اعلام شد." if done
                                else "منتظرِ حریف بمان؛ نتیجه در پیامِ بازی اعلام می‌شود."), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.info("wager inline dice #%s: pm notice skipped: %s", wid, e)
    if imid:
        text, kb = core.render(r.w)
        kb = (await core.result_kb(r.w)) or await inline_kb(bot, r.w, kb)
        await _edit(bot, imid, text, kb)


# ───────────────────────── لغو دعوت و شارژ موجودی ─────────────────────────

@wager_inline_router.callback_query(F.data.startswith("iwg:x:"))
async def iwg_cancel(cb: CallbackQuery, bot: Bot):
    """PATCH_INLINE_CANCEL_V1: فقط میزبان، فقط پیش از شروع بازی (هنوز مبلغی رزرو نشده)."""
    imid = cb.inline_message_id
    try:
        creator = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer("دکمه نامعتبر است.", show_alert=True)
        return
    if not imid:
        await cb.answer()
        return
    if cb.from_user.id != creator:
        await cb.answer("فقط میزبان می‌تواند این بازی را لغو کند.", show_alert=True)
        return
    lock = _locks.setdefault(imid, asyncio.Lock())
    async with lock:
        if _STARTED.get(imid):
            await cb.answer("بازی شروع شده و دیگر لغو نمی‌شود.", show_alert=True)
            return
        _STARTED[imid] = True                 # پیوستنِ هم‌زمان دیگر بازی نمی‌سازد
        await _edit(bot, imid, "❌ <b>بازی توسط میزبان لغو شد.</b>\nمبلغی از کسی کم نشده بود.", None)
    await cb.answer("بازی لغو شد.")


async def wallet_deeplink(msg, bot: Bot, row) -> bool:
    """`/start iwc` (دکمهٔ «شارژ موجودی» در پیامِ «موجودی کافی نیست») ⇒ صفحهٔ کیف پول."""
    from tenant import get_or_create_user, show_wallet
    u = await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    await show_wallet(msg, row.id, u)
    return True


# ───────────────────────── بازی‌های تایپی از راه پیوی (PATCH_INLINE_TYPED_V1، ترفند سینا) ─────────────────────────
# پیام اینلاین چت ندارد که جواب در آن نوشته شود. سؤال روی همان پیام اینلاین دیده می‌شود؛ دکمهٔ «✍️ ارسال جواب» بازیکن را به پیوی
# ربات می‌برد (`/start iwt_<id>`) و از آن به بعد هر پیام متنیِ او در پیوی تا پایان بازی جواب حساب می‌شود. اولین جوابِ درست برنده است.
_PM_RACE: dict[tuple[str, int], int] = {}          # (توکن ربات، آیدی کاربر) → شناسهٔ بازی


async def typed_deeplink(msg, bot: Bot, row, payload: str) -> bool:
    import platform_wager_race as R
    try:
        wid = int(payload[4:])
    except ValueError:
        return False
    async with Session() as s:
        w = await s.get(PlatformWager, wid)
    uid = msg.from_user.id
    if not w or (w.bot_id or 0) != row.id or w.game not in TYPED_GAMES:
        await msg.answer("❌ این بازی پیدا نشد.")
    elif uid not in (w.creator_id, w.opponent_id):
        await msg.answer("❌ تو بازیکن این بازی نیستی.")
    elif w.status != "playing" or wid not in R.LIVE:
        await msg.answer("⌛ این بازی تمام شده است؛ نتیجه را در همان پیام بازی ببین.")
    else:
        _PM_RACE[(getattr(bot, "token", ""), uid)] = wid
        text, _kb = R.render_race(w)
        await msg.answer("✍️ <b>جوابت را همین‌جا بفرست</b> (اولین جواب درست برنده است)\n\n" + text, parse_mode="HTML")
    return True


def _expected(lv: dict) -> set[str]:
    out = {str(lv.get("answer") or "")}
    out |= {str(a) for a in (lv.get("alts") or [])}
    return {a for a in out if a}


async def pm_race_answer(msg, bot: Bot) -> bool:
    """پیام متنی در پیوی: اگر کاربر در یک بازی تایپی اینلاین است، جواب حساب می‌شود. True ⇒ پیام مصرف شد."""
    key = (getattr(bot, "token", ""), msg.from_user.id if msg.from_user else 0)
    wid = _PM_RACE.get(key)
    if wid is None:
        return False
    import platform_wager_race as R
    lv = R.LIVE.get(wid)
    if not lv or msg.from_user.id not in lv.get("players", ()):
        _PM_RACE.pop(key, None)
        return False                    # بازی تمام شده ⇒ پیام برای بقیهٔ ربات آزاد است
    if (msg.text or "").startswith("/"):
        return False
    if R.norm_answer(msg.text) not in _expected(lv):
        await msg.answer("❌ درست نبود؛ دوباره امتحان کن.")
        return True
    w = await R.settle_to(wid, msg.from_user.id)
    if w is None:
        if not await R._still_active(wid):
            R.LIVE.pop(wid, None)
            _PM_RACE.pop(key, None)
        await msg.answer("⌛ این بازی دیگر فعال نیست.")
        return True
    R.LIVE.pop(wid, None)
    for k in [k for k, v in _PM_RACE.items() if v == wid]:
        _PM_RACE.pop(k, None)
    await R._edit(bot, w)
    await msg.answer("🏁 درست بود؛ <b>برنده شدی!</b>\n" + R.prize_line(w), parse_mode="HTML")
    return True


# ───────────────────────── عضویت اجباری مخصوص بازی اینلاین (PATCH_INLINE_FJ_V1، سینا) ─────────────────────────
# حالت‌ها: «bot» = همان کانال‌های عضویت اجباری ربات (رفتار قبلی، پیش‌فرض) · «custom» = فهرست جدا که مالک تنظیم می‌کند ·
# «off» = بازی اینلاین عضویت نمی‌خواهد. مالک و ادمین‌ها معاف‌اند (مثل عضویت اجباری اصلی).
FJ_KEY = "wager_inline_fj"
FJ_MODES = ("bot", "custom", "off")
FJ_MODE_FA = {"bot": "همان کانال‌های ربات", "custom": "کانال‌های جدا", "off": "خاموش (بدون عضویت)"}
FJ_MAX = 10


class IwFjFSM(StatesGroup):
    add = State()


async def fj_cfg(bot_id: int) -> dict:
    from tenant import get_setting
    try:
        d = json.loads((await get_setting(bot_id, FJ_KEY, "")) or "{}")
    except ValueError:
        d = {}
    mode = d.get("mode") if d.get("mode") in FJ_MODES else "bot"
    ch = [c for c in (d.get("ch") or []) if isinstance(c, dict) and c.get("chat_id")][:FJ_MAX]
    return {"mode": mode, "ch": ch}


async def fj_save(bot_id: int, cfg: dict) -> None:
    from tenant import set_setting
    await set_setting(bot_id, FJ_KEY, json.dumps({"mode": cfg["mode"], "ch": cfg["ch"][:FJ_MAX]}, ensure_ascii=False))


async def fj_missing(bot: Bot, bot_id: int, uid: int) -> list[dict]:
    """کانال‌هایی که این کاربر برای بازی اینلاین هنوز عضو نشده (همان قالب check_force_join)."""
    from tenant import check_force_join, get_bot_row, is_owner_or_admin
    cfg = await fj_cfg(bot_id)
    if cfg["mode"] == "bot":
        return await check_force_join(bot, bot_id, uid)
    if cfg["mode"] == "off" or not cfg["ch"]:
        return []
    row = await get_bot_row(bot)
    if row and await is_owner_or_admin(bot_id, uid, row.owner_id):
        return []

    async def one(c):
        try:
            m = await asyncio.wait_for(bot.get_chat_member(c["chat_id"], uid), timeout=6)
            return None if m.status not in ("left", "kicked") else c
        except Exception as e:  # noqa: BLE001 — کانال قابل بررسی نیست (ربات ادمین نیست، …) ⇒ کاربر قفل نشود
            logger.info("inline fj: کانال %s قابل بررسی نیست: %s", c.get("chat_id"), e)
            return None
    res = await asyncio.gather(*(one(c) for c in cfg["ch"]))
    return [{"chat_id": c["chat_id"], "title": c.get("title") or c["chat_id"], "invite_link": c.get("invite_link"),
             "btn_color": None} for c in res if c]


async def _fj_owner(event, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, event.from_user.id, row.owner_id):
        return None
    return row


async def _fj_view(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cfg = await fj_cfg(bot_id)
    lines = ["📢 <b>عضویت اجباری بازی اینلاین</b>", "",
             "پیش از شروع بازی اینلاین (@ربات دوز و …) هر دو بازیکن باید عضو این کانال‌ها باشند.", "",
             f"حالت: <b>{FJ_MODE_FA[cfg['mode']]}</b>"]
    if cfg["mode"] == "custom":
        lines.append("")
        lines += ([f"• {_h.escape(str(c.get('title') or c['chat_id']))}" for c in cfg["ch"]]
                  or ["هنوز کانالی اضافه نشده (تا اضافه نکنی، عضویت خواسته نمی‌شود)."])
        lines += ["", "ربات باید در هر کانال ادمین باشد تا بتواند عضویت را بررسی کند."]
    rows = [[InlineKeyboardButton(text=f"🔁 حالت: {FJ_MODE_FA[cfg['mode']]} (تغییر)", callback_data="iwfj:mode")]]
    if cfg["mode"] == "custom":
        for i, c in enumerate(cfg["ch"]):
            rows.append([InlineKeyboardButton(text=f"🗑 {str(c.get('title') or c['chat_id'])[:40]}", callback_data=f"iwfj:del:{i}",
                                              style=ButtonStyle.DANGER)])
        if len(cfg["ch"]) < FJ_MAX:
            rows.append([InlineKeyboardButton(text="➕ افزودن کانال", callback_data="iwfj:add", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="twrk:menu", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _fj_show(target, bot_id: int) -> None:
    text, kb = await _fj_view(bot_id)
    m = getattr(target, "message", None) or target
    try:
        if getattr(target, "message", None) is not None:
            await m.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
    except Exception:  # noqa: BLE001
        logger.debug("inline fj: ویرایش صفحه ممکن نشد", exc_info=True)
    await m.answer(text, reply_markup=kb, parse_mode="HTML")


@wager_inline_router.callback_query(F.data.in_({"iwfj:menu", "iwfj:mode", "iwfj:add"}) | F.data.startswith("iwfj:del:"))
async def iwfj_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _fj_owner(cb, bot)
    if not row:
        await cb.answer("فقط مالک یا ادمین ربات", show_alert=True)
        return
    cfg = await fj_cfg(row.id)
    if cb.data == "iwfj:mode":
        cfg["mode"] = FJ_MODES[(FJ_MODES.index(cfg["mode"]) + 1) % len(FJ_MODES)]
        await fj_save(row.id, cfg)
    elif cb.data == "iwfj:add":
        await state.set_state(IwFjFSM.add)
        await cb.message.answer("آیدی کانال را بفرست (مثلاً <code>@mychannel</code> یا <code>-1001234567890</code>).\n"
                                "اول ربات را در آن کانال ادمین کن. انصراف: /cancel", parse_mode="HTML")
        await cb.answer()
        return
    elif cb.data.startswith("iwfj:del:"):
        try:
            cfg["ch"].pop(int(cb.data.split(":")[2]))
        except (ValueError, IndexError):
            pass
        await fj_save(row.id, cfg)
    await _fj_show(cb, row.id)
    await cb.answer()


@wager_inline_router.message(IwFjFSM.add)
async def iwfj_add_input(msg, bot: Bot, state: FSMContext):
    row = await _fj_owner(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو", "❌ لغو"):
        await state.clear()
        await _fj_show(msg, row.id)
        return
    cid = raw.replace("https://t.me/", "@").split("?")[0]
    if cid and not cid.startswith(("@", "-")):
        cid = "@" + cid
    try:
        chat = await bot.get_chat(cid)
        me = await bot.get_chat_member(chat.id, (await bot.me()).id)
        if me.status not in ("administrator", "creator"):
            await msg.answer("❌ ربات در این کانال ادمین نیست؛ اول ادمینش کن و دوباره بفرست.")
            return
    except Exception as e:  # noqa: BLE001
        await msg.answer(f"❌ این کانال پیدا نشد یا ربات به آن دسترسی ندارد ({_h.escape(str(e)[:80])}). دوباره بفرست یا /cancel.",
                         parse_mode="HTML")
        return
    link = f"https://t.me/{chat.username}" if getattr(chat, "username", None) else getattr(chat, "invite_link", None)
    cfg = await fj_cfg(row.id)
    if any(str(c["chat_id"]) == str(chat.id) for c in cfg["ch"]):
        await msg.answer("این کانال از قبل در فهرست است.")
    else:
        cfg["ch"].append({"chat_id": str(chat.id), "title": chat.title or str(chat.id), "invite_link": link})
        cfg["mode"] = "custom"
        await fj_save(row.id, cfg)
        await msg.answer(f"✅ «{_h.escape(chat.title or str(chat.id))}» اضافه شد.", parse_mode="HTML")
    await state.clear()
    await _fj_show(msg, row.id)
