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

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
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

INLINE_GAMES = ("xo", "rps")
_NAMES = {"xo": ("دوز", "xo", "اکس او", "ایکس او"), "rps": ("سنگ کاغذ قیچی", "rps")}
_TITLE = {"xo": "❌⭕ دوز", "rps": "🪨📄✂️ سنگ کاغذ قیچی"}
_DESC = {"xo": "هرکی یه خط سه‌تایی بسازه برنده‌ست.",
         "rps": "سنگ قیچی رو می‌شکنه، قیچی کاغذ رو می‌بره، کاغذ سنگ رو می‌پیچه. ۳ راندی؛ هرکی زودتر ۲ تا ببره برنده‌ست."}
_AMT = r"([۰-۹0-9,،]+)"
_RX = {g: re.compile(rf"(?:بازی\s+)?(?:{'|'.join(re.escape(n).replace(chr(92) + ' ', r'\s*') for n in names)})(?:\s+{_AMT})?",
                     re.IGNORECASE) for g, names in _NAMES.items()}
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


def _invite_text(game: str, amount: int, name: str, free: bool) -> str:
    money = ("🆓 <b>بازیِ رایگان</b> — جایزهٔ پولی ندارد" if free
             else f"💰 مبلغ ورودی هر نفر: <b>{core._rial(amount)}</b>")
    return (f"🎮 <b>{_h.escape(_TITLE[game])}</b>\n{_DESC[game]}\n\n{money}\n"
            f"👤 میزبان: <b>{_h.escape(name)}</b>\n\nبرای بازی، دکمهٔ زیر را بزن 👇")


def _join_kb(game: str, amount: int, creator: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="➕ پیوستن به بازی", callback_data=f"iwg:j:{game}:{amount}:{creator}", style=ButtonStyle.SUCCESS)]])


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
        for amt in offers:
            if amt and (amt < int(cfg["min"]) or (cfg.get("max") and amt > int(cfg["max"]))):
                continue
            free = amt == 0
            title = _TITLE[g] + (" — رایگان" if free else f" — {core._rial(amt)}")
            results.append(InlineQueryResultArticle(
                id=f"{g}:{amt}", title=title,
                description=(_DESC[g] if free else _DESC[g] + f"  (برایِ مبلغِ دیگر بنویس: {_NAMES[g][0]} ۵۰۰۰)"),
                input_message_content=InputTextMessageContent(
                    message_text=_invite_text(g, amt, name, free), parse_mode="HTML"),
                reply_markup=_join_kb(g, amt, q.from_user.id)))
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
        _, _, game, amt_s, cre_s = cb.data.split(":")
        amount, creator = int(amt_s), int(cre_s)
    except ValueError:
        await cb.answer("دکمه نامعتبر است.", show_alert=True)
        return
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
    miss_c = await check_force_join(bot, row.id, creator)
    miss_j = await check_force_join(bot, row.id, joiner.id)
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
            if game == "xo":
                o = await core.set_option(w.id, creator, "X", bot_id=row.id)
                if o.err:
                    raise RuntimeError(o.err)
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
        text, kb = core.render(r.w)
        await _edit(bot, imid, text, kb)
    await cb.answer("به بازی پیوستی؛ موفق باشی!")
