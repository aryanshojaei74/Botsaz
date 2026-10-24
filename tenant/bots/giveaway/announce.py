"""
tenant/bots/giveaway/announce.py — ارسال به گروه/کانال/کاربران، تحویلِ جایزه و پایانِ قرعه‌کشی (PATCH_GIVEAWAY_BOT_V1).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from decimal import Decimal

from aiogram import Bot
from aiogram.enums import ButtonStyle
from sqlalchemy import select

from config import Session
from models import (BotUser, Giveaway, GiveawayChat, GiveawayEntry, GiveawayPost, GiveawayPrize, GiveawayWinner,
                    WalletTxn)

from tenant.bots.giveaway import engine as E
from tenant.bots.giveaway.common import (btn, card_text, deep_link, esc, fa, kb, medal, results_text)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BROADCAST_DELAY = 0.05
_MEDIA_SENDERS = {"photo": "send_photo", "video": "send_video", "document": "send_document",
                  "animation": "send_animation", "audio": "send_audio", "voice": "send_voice"}


# ───────────────────────── مقصدها (گروه/کانال) ─────────────────────────

async def saved_chats(bot_id: int) -> list[GiveawayChat]:
    async with Session() as s:
        return list((await s.scalars(select(GiveawayChat).where(GiveawayChat.bot_id == bot_id)
                                     .order_by(GiveawayChat.id))).all())


async def post_markup(bot: Bot, g: Giveaway):
    link = await deep_link(bot, g.id)
    return kb([btn("🎰 شرکت در قرعه‌کشی", url=link, style=ButtonStyle.SUCCESS)])


async def post_announcement(bot: Bot, row, g: Giveaway, chat_ids: list[int] | None = None) -> tuple[int, list[str]]:
    """اعلانِ قرعه‌کشی با دکمهٔ شرکت به مقصدها؛ (تعدادِ موفق، خطاها). پیام‌ها برایِ ادیتِ بعدی ثبت می‌شوند."""
    prizes = await E.prizes_of(g.id)
    n = await E.count_entries(g.id)
    text = card_text(g, prizes, n)
    markup = await post_markup(bot, g)
    targets = chat_ids if chat_ids is not None else [c.chat_id for c in await saved_chats(row.id)]
    ok, errs = 0, []
    for cid in targets:
        try:
            m = await bot.send_message(cid, text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
            async with Session() as s:
                s.add(GiveawayPost(bot_id=row.id, giveaway_id=g.id, chat_id=cid, message_id=m.message_id))
                await s.commit()
            ok += 1
        except Exception as e:      # noqa: BLE001
            errs.append(f"{cid}: {str(e)[:80]}")
    return ok, errs


async def refresh_posts(bot: Bot, g: Giveaway, text: str | None = None, markup="keep") -> int:
    """متنِ اعلان‌هایِ قبلی را در جا ادیت می‌کند (شمارندهٔ شرکت‌کنندگان / نتیجه)."""
    async with Session() as s:
        posts = list((await s.scalars(select(GiveawayPost).where(GiveawayPost.giveaway_id == g.id))).all())
    if not posts:
        return 0
    if text is None:
        text = card_text(g, await E.prizes_of(g.id), await E.count_entries(g.id))
    if markup == "keep":
        markup = await post_markup(bot, g) if g.status == "active" else None
    done = 0
    for p in posts:
        try:
            await bot.edit_message_text(chat_id=p.chat_id, message_id=p.message_id, text=text, reply_markup=markup,
                                        parse_mode="HTML", disable_web_page_preview=True)
            done += 1
        except Exception as e:      # noqa: BLE001
            if "not modified" in str(e).lower():
                done += 1
            else:
                logger.debug("giveaway refresh_posts %s: %s", p.chat_id, e)
    return done


# ───────────────────────── اطلاعیه به کاربران ─────────────────────────

async def user_ids(bot_id: int) -> list[int]:
    async with Session() as s:
        rows = (await s.scalars(select(BotUser.telegram_id).where(
            BotUser.bot_id == bot_id, BotUser.is_banned == False))).all()   # noqa: E712
    return list(rows)


async def broadcast_users(bot: Bot, bot_id: int, text: str, markup=None, exclude: set[int] | None = None,
                          delay: float = BROADCAST_DELAY) -> tuple[int, int]:
    ok = fail = 0
    skip = exclude or set()
    for uid in await user_ids(bot_id):
        if uid in skip:
            continue
        try:
            await bot.send_message(uid, text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
            ok += 1
        except Exception:      # noqa: BLE001 — بلاک‌کرده/غیرفعال
            fail += 1
        await asyncio.sleep(delay)
    return ok, fail


async def announce_start(bot: Bot, row, g: Giveaway) -> tuple[int, int]:
    prizes = await E.prizes_of(g.id)
    text = "🎊 <b>قرعه‌کشیِ جدید شروع شد!</b>\n\n" + card_text(g, prizes, 0)
    return await broadcast_users(bot, row.id, text, await post_markup(bot, g))


# ───────────────────────── تحویلِ جایزه ─────────────────────────

async def credit_wallet(bot_id: int, user_tg: int, amount: int, note: str) -> bool:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == user_tg))
        if u is None:
            return False
        u.balance = Decimal(str(u.balance or 0)) + Decimal(int(amount))
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=int(amount), kind="giveaway_prize", note=note[:200]))
        await s.commit()
    return True


async def deliver_prize(bot: Bot, row, g: Giveaway, w: GiveawayWinner, entry: GiveawayEntry,
                        prize: GiveawayPrize | None) -> tuple[bool, str]:
    """پیامِ تبریک + خودِ جایزه به برنده. (موفق، توضیح). جایزهٔ کیف‌پول خودکار شارژ می‌شود."""
    head = (f"🎉 <b>تبریک! تو برندهٔ {fa(w.rank)}ام قرعه‌کشیِ «{esc(g.title)}» شدی!</b>\n\n"
            f"🪪 شناسهٔ تو: <code>{esc(entry.code)}</code>")
    if prize is None:
        body = head
    elif prize.kind == "wallet" and prize.amount > 0:
        okc = await credit_wallet(row.id, entry.user_tg, prize.amount, f"جایزهٔ قرعه‌کشی #{g.id}")
        body = head + f"\n\n🎁 جایزه: <b>{esc(prize.title)}</b>\n" + (
            f"💰 {fa(f'{prize.amount:,}')} تومان به کیف‌پولت اضافه شد." if okc else "⚠️ شارژِ کیف‌پول ناموفق بود؛ به پشتیبانی پیام بده.")
    else:
        body = head + f"\n\n🎁 جایزه: <b>{esc(prize.title)}</b>"
        if prize.kind in _MEDIA_SENDERS and prize.text:
            body += f"\n\n{prize.text}"
        if prize.kind == "title":
            body += "\n\nبرایِ دریافتِ جایزه به پشتیبانی/مدیرِ ربات پیام بده و شناسهٔ بالا را بگو."
        elif prize.kind == "text" and prize.text:
            body += f"\n\n{prize.text}"
    try:
        sender = _MEDIA_SENDERS.get(prize.kind) if prize else None
        if sender and prize and prize.file_id:
            await getattr(bot, sender)(entry.user_tg, prize.file_id, caption=body[:1024], parse_mode="HTML")
        else:
            await bot.send_message(entry.user_tg, body, parse_mode="HTML")
        await E.mark_delivered(w.id, "ok")
        return True, "تحویل شد"
    except Exception as e:      # noqa: BLE001
        return False, str(e)[:100]


# ───────────────────────── پایانِ قرعه‌کشی ─────────────────────────

async def make_member_check(bot: Bot, row, concurrency: int = 8):
    """None اگر عضویتِ اجباری تعریف نشده؛ وگرنه async (uid)→bool (عضو؟)."""
    from models import ForceJoin
    async with Session() as s:
        has = await s.scalar(select(ForceJoin.id).where(ForceJoin.bot_id == row.id, ForceJoin.is_active == True))  # noqa: E712
    if has is None:
        return None
    from tenant import check_force_join
    sem = asyncio.Semaphore(concurrency)

    async def _check(uid: int):
        async with sem:
            nj = await check_force_join(bot, row.id, uid, bypass_cache=True)
        return not nj
    return _check


async def finish_giveaway(bot: Bot, row, gid: int, notify_admin_chat: int | None = None) -> E.DrawResult:
    """قرعه + تحویلِ جایزه + اعلانِ عمومی (گروه/کانال/کاربران). idempotent (دومین بار not_active)."""
    g0 = await E.get_giveaway(gid, row.id)
    if g0 is None:
        return E.DrawResult(False, "notfound")
    check = await make_member_check(bot, row)
    res = await E.draw(gid, row.id, check)
    if not res.ok:
        return res
    g = await E.get_giveaway(gid, row.id)
    winners = res.winners or []
    # ۱) جایزه‌ها
    delivered = failed = 0
    notes: list[str] = []
    for w, e, p in winners:
        ok, why = await deliver_prize(bot, row, g, w, e, p)
        delivered += 1 if ok else 0
        if not ok:
            failed += 1
            notes.append(f"{medal(w.rank)} {e.user_tg}: {why}")
    # ۲) اعلانِ عمومی (ادیتِ اعلان‌هایِ قبلی؛ اگر نبود، ارسالِ جدید به مقصدها)
    public = results_text(g, [(w, e, p) for w, e, p in winners], res.participants, res.left, public=True)
    edited = await refresh_posts(bot, g, public, None)
    if not edited:
        for c in await saved_chats(row.id):
            try:
                await bot.send_message(c.chat_id, public, parse_mode="HTML")
            except Exception:      # noqa: BLE001
                logger.debug("giveaway: ارسالِ نتیجه به %s نشد", c.chat_id)
    # ۳) به همهٔ کاربران (برندگان قبلاً پیامِ شخصی گرفتند)
    if g.notify_all:
        win_ids = {e.user_tg for _, e, _ in winners}
        asyncio.create_task(_broadcast_results(bot, row, public, win_ids))
    # ۴) گزارش به مدیر
    if notify_admin_chat:
        try:
            await bot.send_message(notify_admin_chat, admin_report(g, winners, res, delivered, failed, notes), parse_mode="HTML")
        except Exception:      # noqa: BLE001
            logger.debug("giveaway: گزارش به مدیر نرفت")
    return res


async def _broadcast_results(bot: Bot, row, text: str, exclude: set[int]) -> None:
    try:
        await broadcast_users(bot, row.id, text, None, exclude)
    except Exception:      # noqa: BLE001
        logger.exception("giveaway: اطلاعیهٔ نتیجه")


def admin_report(g: Giveaway, winners, res, delivered: int, failed: int, notes: list[str]) -> str:
    lines = [f"🏁 <b>قرعه‌کشی «{esc(g.title)}» پایان یافت</b>", "",
             f"👥 شرکت‌کنندگان: {fa(res.participants)} | ❌ لفت‌داده: {fa(res.left)}", ""]
    for w, e, p in winners:
        who = f"{esc(e.name or 'کاربر')}" + (f" (@{esc(e.username)})" if e.username else "")
        lines.append(f"{medal(w.rank)} <a href=\"tg://user?id={e.user_tg}\">{who}</a> — <code>{e.user_tg}</code>"
                     + (f" — {esc(p.title)}" if p else "") + (f" — ☎️ {esc(e.phone)}" if e.phone else ""))
    lines += ["", f"🎁 تحویل‌شده: {fa(delivered)} | ⚠️ ناموفق: {fa(failed)}"]
    if notes:
        lines += ["", "ناموفق‌ها (کاربر ربات را بلاک کرده یا هنوز استارت نکرده):"] + [f"• {esc(n)}" for n in notes[:15]]
    return "\n".join(lines)


async def redeliver_pending(bot: Bot, row, gid: int) -> tuple[int, int]:
    """جایزه‌هایِ تحویل‌نشده را دوباره می‌فرستد."""
    g = await E.get_giveaway(gid, row.id)
    if g is None:
        return 0, 0
    ok = bad = 0
    for w, e, p in await E.winners_of(gid):
        if w.delivered:
            continue
        r, _ = await deliver_prize(bot, row, g, w, e, p)
        ok += 1 if r else 0
        bad += 0 if r else 1
    return ok, bad


# ───────────────────────── سلامتِ عضویتِ اجباری ─────────────────────────
_FJ_HEALTH: dict[int, tuple[float, list[str]]] = {}


async def forcejoin_problems(bot: Bot, bot_id: int) -> list[str]:
    """کانال‌هایِ عضویتِ اجباریِ فعال که ربات نمی‌تواند عضویتشان را بررسی کند (ادمین نیست/کانال پاک شده).
    در این حالت عضویتِ اجباری بی‌صدا غیرفعال می‌ماند تا کاربر قفل نشود — پس مالک باید بداند. ۶۰ثانیه کش."""
    import time as _t
    hit = _FJ_HEALTH.get(bot_id)
    if hit and _t.monotonic() - hit[0] < 60:
        return hit[1]
    from models import ForceJoin
    async with Session() as s:
        fjs = list((await s.scalars(select(ForceJoin).where(ForceJoin.bot_id == bot_id,
                                                           ForceJoin.is_active == True))).all())  # noqa: E712
    out: list[str] = []
    if fjs:
        try:
            me = await bot.me()
        except Exception as e:      # noqa: BLE001
            logger.debug("giveaway: bot.me() ناموفق: %s", e)
            return []
        for fj in fjs:
            title = str(fj.title or fj.chat_id)
            try:
                m = await asyncio.wait_for(bot.get_chat_member(fj.chat_id, me.id), timeout=5.0)
                if m.status not in ("administrator", "creator"):
                    out.append(f"{title} — ربات ادمین نیست")
            except Exception:      # noqa: BLE001
                out.append(f"{title} — ربات به این کانال/گروه دسترسی ندارد")
    _FJ_HEALTH[bot_id] = (_t.monotonic(), out)
    if len(_FJ_HEALTH) > 500:
        _FJ_HEALTH.clear()
    return out
