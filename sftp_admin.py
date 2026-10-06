"""
sftp_admin.py — PATCH_SFTP_ADMIN_PANEL_V1

مدیریتِ سرویسِ «SFTP حرفه‌ای بات‌ساز» از داخلِ پنلِ مدیریتِ بات‌ساز.
دیتابیسِ sftpvip جداست (/opt/sftpvip/sftpvip.db) و مستقیم با sqlite3
خوانده/نوشته می‌شود؛ هیچ وابستگیِ جدیدی به پروژه اضافه نمی‌کند.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import PLATFORM_ADMIN_ID
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

sftp_admin_router = Router()

SFTP_DB = os.getenv("SFTPVIP_DB", "/opt/sftpvip/sftpvip.db")
PAGE = 10


class _SftpFSM(StatesGroup):
    price = State()
    trial = State()
    grant = State()
    block = State()


def _q(sql: str, args: tuple = (), *, write: bool = False):
    if not os.path.exists(SFTP_DB):
        raise FileNotFoundError(SFTP_DB)
    con = sqlite3.connect(SFTP_DB, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(sql, args)
        rows = cur.fetchall()
        if write:
            con.commit()
        return rows
    finally:
        con.close()


async def q(sql: str, args: tuple = (), *, write: bool = False):
    return await asyncio.to_thread(_q, sql, args, write=write)


def _is_admin(uid: int) -> bool:
    return uid == PLATFORM_ADMIN_ID


def _fdate(v) -> str:
    if not v:
        return "نامحدود"
    s = str(v)[:16].replace("T", " ")
    return s


def _menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [hx_button("📊 آمار سرویس", callback_data="sftpv:stats",
                   style=ButtonStyle.PRIMARY)],
        [hx_button("👥 کاربران", callback_data="sftpv:users:0",
                   style=ButtonStyle.PRIMARY)],
        [hx_button("💰 قیمت و روزِ تست", callback_data="sftpv:price",
                   style=ButtonStyle.DANGER)],
        [hx_button("➕ تمدید / اعطای اشتراک", callback_data="sftpv:grant",
                   style=ButtonStyle.SUCCESS)],
        [hx_button("⛔️ مسدود / رفعِ مسدودی", callback_data="sftpv:block",
                   style=ButtonStyle.DANGER)],
        [hx_button("⬅️ بازگشت", callback_data="adm:root",
                   style=ButtonStyle.DANGER)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("⬅️ منویِ SFTP", callback_data="sftpv:menu",
                  style=ButtonStyle.DANGER)]])


async def _show_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        "🗂 <b>مدیریتِ SFTP حرفه‌ای</b>\n\n"
        "سرویسِ مستقلِ دسترسیِ SSH/SFTP — کاربران، اشتراک و قیمت.",
        reply_markup=_menu_kb())


@sftp_admin_router.callback_query(F.data == "adm:sftpvip")
@sftp_admin_router.callback_query(F.data == "sftpv:menu")
async def sftp_menu(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    await state.clear()
    try:
        await _show_menu(cb)
    except Exception:
        await cb.message.answer(
            "🗂 <b>مدیریتِ SFTP حرفه‌ای</b>", reply_markup=_menu_kb())
    await cb.answer()


@sftp_admin_router.callback_query(F.data == "sftpv:stats")
async def sftp_stats(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    try:
        conns = (await q("select count(*) c from sftp_connections"))[0]["c"]
        cusers = (await q(
            "select count(distinct telegram_id) c from sftp_connections"
        ))[0]["c"]
        subs = (await q("select count(*) c from sftp_subscriptions"))[0]["c"]
        blocked = (await q(
            "select count(*) c from sftp_subscriptions where is_blocked=1"
        ))[0]["c"]
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        active = (await q(
            "select count(*) c from sftp_subscriptions "
            "where is_blocked=0 and (expires_at is null or expires_at > ?)",
            (now,)))[0]["c"]
        recent = (await q(
            "select count(*) c from sftp_connections "
            "where last_used_at is not null and last_used_at > ?",
            ((datetime.utcnow() - timedelta(days=7)).strftime(
                "%Y-%m-%d %H:%M:%S"),)))[0]["c"]
    except FileNotFoundError:
        return await cb.answer("دیتابیسِ sftpvip پیدا نشد.", show_alert=True)

    txt = (
        "📊 <b>آمارِ سرویسِ SFTP</b>\n\n"
        f"👥 کاربرانِ دارایِ اتصال: <b>{cusers}</b>\n"
        f"🔌 کلِ اتصال‌ها: <b>{conns}</b>\n"
        f"🎫 کلِ اشتراک‌ها: <b>{subs}</b>\n"
        f"✅ اشتراکِ فعال: <b>{active}</b>\n"
        f"⛔️ مسدود: <b>{blocked}</b>\n"
        f"🕒 اتصالِ استفاده‌شده در ۷ روزِ اخیر: <b>{recent}</b>"
    )
    await cb.message.edit_text(txt, reply_markup=_back_kb())
    await cb.answer()


@sftp_admin_router.callback_query(F.data.startswith("sftpv:users:"))
async def sftp_users(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    try:
        off = int(cb.data.split(":")[2])
    except Exception:
        off = 0
    try:
        total = (await q("select count(*) c from sftp_subscriptions"))[0]["c"]
        rows = await q(
            "select telegram_id, expires_at, is_blocked, note "
            "from sftp_subscriptions order by id desc limit ? offset ?",
            (PAGE, off))
    except FileNotFoundError:
        return await cb.answer("دیتابیسِ sftpvip پیدا نشد.", show_alert=True)

    if not rows:
        return await cb.answer("کاربری نیست.", show_alert=True)

    lines = [f"👥 <b>کاربرانِ SFTP</b> — مجموع {total}\n"]
    for r in rows:
        mark = "⛔️" if r["is_blocked"] else "✅"
        note = f" — {r['note']}" if r["note"] else ""
        lines.append(
            f"{mark} <code>{r['telegram_id']}</code> · "
            f"تا {_fdate(r['expires_at'])}{note}")

    nav = []
    if off > 0:
        nav.append(hx_button("⬅️ قبلی",
                             callback_data=f"sftpv:users:{max(0, off - PAGE)}",
                             style=ButtonStyle.DANGER))
    if off + PAGE < total:
        nav.append(hx_button("بعدی ➡️",
                             callback_data=f"sftpv:users:{off + PAGE}",
                             style=ButtonStyle.DANGER))
    kb = [nav] if nav else []
    kb.append([hx_button("⬅️ منویِ SFTP", callback_data="sftpv:menu",
                         style=ButtonStyle.DANGER)])
    await cb.message.edit_text("\n".join(lines),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@sftp_admin_router.callback_query(F.data == "sftpv:price")
async def sftp_price(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    try:
        rows = await q("select key, value from sftp_settings")
    except FileNotFoundError:
        return await cb.answer("دیتابیسِ sftpvip پیدا نشد.", show_alert=True)
    cur = {r["key"]: r["value"] for r in rows}
    txt = ("💰 <b>تنظیماتِ سرویس</b>\n\n"
           f"قیمتِ ماهانه: <b>{cur.get('price', '—')}</b> تومان\n"
           f"روزهایِ تستِ رایگان: <b>{cur.get('trial_days', '—')}</b>\n\n"
           "عددِ جدیدِ <b>قیمت</b> را بفرست (یا /cancel):")
    await state.set_state(_SftpFSM.price)
    await cb.message.edit_text(txt, reply_markup=_back_kb())
    await cb.answer()


@sftp_admin_router.message(_SftpFSM.price)
async def sftp_price_set(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    t = (msg.text or "").strip()
    if t.startswith("/cancel"):
        await state.clear()
        return await msg.answer("لغو شد.", reply_markup=_back_kb())
    if not t.isdigit():
        return await msg.answer("فقط عدد بفرست.")
    await q("insert into sftp_settings(key, value) values(?, ?) "
            "on conflict(key) do update set value=excluded.value",
            ("price", t), write=True)
    await state.set_state(_SftpFSM.trial)
    await msg.answer(f"✅ قیمت روی <b>{t}</b> تومان تنظیم شد.\n\n"
                     "حالا <b>روزهایِ تستِ رایگان</b> را بفرست (یا /cancel):")


@sftp_admin_router.message(_SftpFSM.trial)
async def sftp_trial_set(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    t = (msg.text or "").strip()
    if t.startswith("/cancel"):
        await state.clear()
        return await msg.answer("لغو شد.", reply_markup=_back_kb())
    if not t.isdigit():
        return await msg.answer("فقط عدد بفرست.")
    await q("insert into sftp_settings(key, value) values(?, ?) "
            "on conflict(key) do update set value=excluded.value",
            ("trial_days", t), write=True)
    await state.clear()
    await msg.answer(f"✅ روزهایِ تست روی <b>{t}</b> تنظیم شد.",
                     reply_markup=_back_kb())


@sftp_admin_router.callback_query(F.data == "sftpv:grant")
async def sftp_grant(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    await state.set_state(_SftpFSM.grant)
    await cb.message.edit_text(
        "➕ <b>تمدید / اعطایِ اشتراک</b>\n\n"
        "به شکلِ <code>آیدی تعدادِ‌روز</code> بفرست.\n"
        "مثال: <code>5293876827 30</code>\n"
        "برایِ نامحدود: <code>5293876827 0</code>\n\n"
        "اگر اشتراک فعال باشد، روزها به آن اضافه می‌شود.",
        reply_markup=_back_kb())
    await cb.answer()


@sftp_admin_router.message(_SftpFSM.grant)
async def sftp_grant_do(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    t = (msg.text or "").strip()
    if t.startswith("/cancel"):
        await state.clear()
        return await msg.answer("لغو شد.", reply_markup=_back_kb())
    parts = t.split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return await msg.answer("قالب درست نیست. مثال: <code>123456 30</code>")
    tid, days = int(parts[0]), int(parts[1])

    rows = await q("select expires_at from sftp_subscriptions "
                   "where telegram_id=?", (tid,))
    if days == 0:
        newexp = None
    else:
        base = datetime.utcnow()
        if rows and rows[0]["expires_at"]:
            try:
                prev = datetime.fromisoformat(str(rows[0]["expires_at"]))
                if prev > base:
                    base = prev
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        newexp = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    if rows:
        await q("update sftp_subscriptions set expires_at=?, is_blocked=0 "
                "where telegram_id=?", (newexp, tid), write=True)
    else:
        await q("insert into sftp_subscriptions"
                "(telegram_id, expires_at, is_blocked) values(?, ?, 0)",
                (tid, newexp), write=True)

    await state.clear()
    logger.info("[SFTP_ADMIN] grant tid=%s days=%s exp=%s", tid, days, newexp)
    await msg.answer(
        f"✅ اشتراکِ <code>{tid}</code> تا <b>{_fdate(newexp)}</b> فعال شد.",
        reply_markup=_back_kb())


@sftp_admin_router.callback_query(F.data == "sftpv:block")
async def sftp_block(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    await state.set_state(_SftpFSM.block)
    await cb.message.edit_text(
        "⛔️ <b>مسدود / رفعِ مسدودی</b>\n\n"
        "آیدیِ عددیِ کاربر را بفرست؛ وضعیتش برعکس می‌شود.",
        reply_markup=_back_kb())
    await cb.answer()


@sftp_admin_router.message(_SftpFSM.block)
async def sftp_block_do(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    t = (msg.text or "").strip()
    if t.startswith("/cancel"):
        await state.clear()
        return await msg.answer("لغو شد.", reply_markup=_back_kb())
    if not t.isdigit():
        return await msg.answer("فقط آیدیِ عددی بفرست.")
    tid = int(t)
    rows = await q("select is_blocked from sftp_subscriptions "
                   "where telegram_id=?", (tid,))
    if not rows:
        await q("insert into sftp_subscriptions"
                "(telegram_id, expires_at, is_blocked) values(?, NULL, 1)",
                (tid,), write=True)
        new = 1
    else:
        new = 0 if rows[0]["is_blocked"] else 1
        await q("update sftp_subscriptions set is_blocked=? "
                "where telegram_id=?", (new, tid), write=True)
    await state.clear()
    logger.info("[SFTP_ADMIN] block tid=%s -> %s", tid, new)
    await msg.answer(
        f"{'⛔️ مسدود شد' if new else '✅ رفعِ مسدودی شد'}: <code>{tid}</code>",
        reply_markup=_back_kb())
