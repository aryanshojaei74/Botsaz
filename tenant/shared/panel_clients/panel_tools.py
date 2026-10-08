"""
panel_tools.py — ابزارهای مدیریتیِ پنل‌های VPN (=== PATCH_PANEL_TOOLS_V1 ===)

پیشنهادِ تیکت #824:
  ۱) 💾 بکاپِ خودکارِ سرویس‌های کاربران (هر N ساعت برایِ مالک فرستاده می‌شود)
     + بازیابی از همان فایل (سرویس‌ها در دیتابیس دوباره ساخته می‌شوند).
  ۲) 🧷 افزودنِ دستیِ سرویس به کاربر با آیدی عددی (سرویسی که از قبل روی پنل هست).
  ۳) 🩺 تستِ زنده‌بودنِ کانفیگ‌هایِ هر پنل (آیا روی پنل هستند / فعال‌اند؟).
  ۴) ➕ افزودنِ حجم/زمانِ کلی به همهٔ سرویس‌هایِ فعالِ یک پنل.

دکمه‌ها در منویِ هر پنل (vpn_admin) قرار می‌گیرند (tools_buttons).
حلقهٔ بکاپ در worker_tenant.py اسپان می‌شود.
"""
from __future__ import annotations
from html import escape as _esc_html

import asyncio
import json
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import or_, select

from config import Session
from models import Bot as BotRow, PanelAccount, Setting, VpnPanel
from tenant.bots.vpn.vpn_admin import PANEL_LABELS, _bk, _owner, _owner_msg, get_panel, is_panel_key
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

panel_tools_router = Router()

BACKUP_MAGIC = "BOTSAZ_VPN_ACCOUNTS_V1"
_BACKUP_CHOICES = (0, 1, 3, 6, 12, 24)
_MARZBAN_FAMILY = ("marzban", "rebecca", "pasargad", "pasarguard",
                   "marzneshin", "upanel_mizban")


_ENC_HEAD = b"BOTSAZ-ENC1\n"


def _bk_fernet():
    """کلیدِ رمزنگاری از توکنِ رباتِ مادر مشتق می‌شود: فقط سرورِ بات‌ساز
    می‌تواند فایل را باز کند (و چون برایِ همهٔ ربات‌ها یکی است، انتقال به
    رباتِ دیگرِ همین پلتفرم هم ممکن است)."""
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    from config import PLATFORM_BOT_TOKEN
    if not PLATFORM_BOT_TOKEN:
        raise RuntimeError("PLATFORM_BOT_TOKEN تنظیم نشده")
    k = hashlib.sha256(b"botsaz-vpn-backup-v1|" + PLATFORM_BOT_TOKEN.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(k))


def _bk_encrypt(raw: bytes) -> bytes:
    return _ENC_HEAD + _bk_fernet().encrypt(raw)


def _bk_decrypt(blob: bytes) -> bytes:
    if blob.startswith(_ENC_HEAD):
        return _bk_fernet().decrypt(blob[len(_ENC_HEAD):])
    return blob  # فایلِ قدیمیِ بدونِ رمز (JSON ساده)


class PtlFSM(StatesGroup):
    add_uid = State()
    add_email = State()
    bulk_gb = State()
    bulk_days = State()
    restore_file = State()


def tools_buttons(key: str) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(text="🩺 تستِ زنده‌بودنِ کانفیگ‌هایِ این پنل",
                              callback_data=f"ptl:health:{key}")],
        [InlineKeyboardButton(text="➕ افزودنِ حجم/زمان به همهٔ کاربرانِ این پنل",
                              callback_data=f"ptl:bulk:{key}")],
        [InlineKeyboardButton(text="🧷 افزودنِ دستیِ سرویس به یک کاربر (با آیدی عددی)",
                              callback_data=f"ptl:add:{key}")],
        [InlineKeyboardButton(text="💾 بکاپ و بازیابیِ سرویس‌ها",
                              callback_data=f"ptl:bk:{key}")],
    ]


def _panel_back(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="⬅️ بازگشت به منویِ پنل", callback_data=f"tvpn:panel:{key}",
        style=ButtonStyle.DANGER)]])


async def _setting(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return r.value if r else default


async def _set_setting(bot_id: int, key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


async def _active_accounts(bot_id: int, key: str):
    async with Session() as s:
        rows = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.panel_key == key,
            PanelAccount.status == "active"))).all()
        return [(a.id, a.email, a.user_telegram_id, a.volume_gb, a.days) for a in rows]


async def _client_for(bot_id: int, key: str):
    p = await get_panel(bot_id, key)
    if not p or not p.enabled:
        return None
    c = make_panel_client(p)
    await c.login()
    return c


async def _close(c) -> None:
    try:
        await c.close()
    except Exception:
        logger.exception("panel_tools: بستنِ کلاینت")


# ═══════════════════════ ۳) تستِ سلامت ═══════════════════════
@panel_tools_router.callback_query(F.data.startswith("ptl:health:"))
async def ptl_health(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):
        return await cb.answer("پنل نامعتبر", show_alert=True)
    await cb.answer("⏳ در حالِ بررسی…")
    try:
        await cb.message.edit_text("🩺 در حالِ بررسیِ کانفیگ‌ها روی پنل… (برایِ پنل‌هایِ بزرگ چند دقیقه طول می‌کشد)")
    except Exception:
        pass
    work = await _active_accounts(row.id, key)
    try:
        client = await _client_for(row.id, key)
    except Exception as e:
        logger.warning("ptl_health login: %s", e)
        client = None
    if client is None:
        return await cb.message.edit_text(
            "❌ اتصال/لاگین به پنل انجام نشد. اطلاعات پنل را بررسی کن.",
            reply_markup=_panel_back(key))
    ok = disabled = errs = 0
    missing: list[tuple[str, int]] = []
    sem = asyncio.Semaphore(5)
    consec = {"n": 0}

    async def _one(email, uid):
        nonlocal ok, disabled, errs
        if consec["n"] >= 8:
            errs += 1
            return
        async with sem:
            try:
                u = await asyncio.wait_for(client.get_usage(email), timeout=20)
                consec["n"] = 0
            except Exception:
                consec["n"] += 1
                errs += 1
                return
        if not u:
            missing.append((email, uid))
        elif isinstance(u, dict) and u.get("enable") is False:
            disabled += 1
        else:
            ok += 1

    try:
        await asyncio.gather(*[_one(e, u) for _id, e, u, _g, _d in work[:600]])
    finally:
        await _close(client)
    lines = [f"🩺 <b>نتیجهٔ تست — {PANEL_LABELS.get(_bk(key), key)}</b>",
             f"📋 بررسی‌شده: {min(len(work), 600)} از {len(work)} سرویس فعال",
             f"✅ سالم: {ok}",
             f"⛔️ غیرفعال روی پنل: {disabled}",
             f"❓ روی پنل پیدا نشد: {len(missing)}",
             f"⚠️ خطای ارتباط: {errs}"]
    if missing:
        lines.append("\n<b>پیدا نشدند:</b>")
        for e, uid in missing[:20]:
            lines.append(f"• <code>{_esc_html(str(e))}</code> — کاربر <code>{uid}</code>")
        if len(missing) > 20:
            lines.append(f"… و {len(missing) - 20} مورد دیگر")
    if errs:
        lines.append("\nℹ️ خطاهایِ ارتباط یعنی پنل جواب نداد؛ لزوماً سرویس خراب نیست.")
    await cb.message.edit_text("\n".join(lines), reply_markup=_panel_back(key))


# ═══════════════════════ ۴) افزودنِ حجم/زمانِ کلی ═══════════════════════
@panel_tools_router.callback_query(F.data.startswith("ptl:bulk:"))
async def ptl_bulk_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):
        return await cb.answer("پنل نامعتبر", show_alert=True)
    n = len(await _active_accounts(row.id, key))
    await state.clear()
    await state.update_data(ptl_key=key)
    await state.set_state(PtlFSM.bulk_gb)
    await cb.message.edit_text(
        f"➕ <b>افزودنِ کلی</b> به {n} سرویسِ فعالِ این پنل\n\n"
        "چند <b>گیگ</b> به حجمِ همه اضافه شود؟ (عدد؛ اگر نمی‌خواهی <code>0</code>)\n"
        "سرویس‌هایِ نامحدود حجمِ اضافه نمی‌گیرند.\n\nبرای لغو: /cancel",
        reply_markup=_panel_back(key))
    await cb.answer()


@panel_tools_router.message(PtlFSM.bulk_gb, F.text)
async def ptl_bulk_gb(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    t = (msg.text or "").strip()
    if t == "/cancel":
        await state.clear()
        return await msg.answer("لغو شد.")
    if not t.isdigit() or int(t) > 100000:
        return await msg.answer("⚠️ یک عددِ صحیح (۰ یا بیشتر) بفرست.")
    await state.update_data(ptl_gb=int(t))
    await state.set_state(PtlFSM.bulk_days)
    await msg.answer("چند <b>روز</b> به زمانِ همه اضافه شود؟ (عدد؛ اگر نمی‌خواهی <code>0</code>)")


@panel_tools_router.message(PtlFSM.bulk_days, F.text)
async def ptl_bulk_days(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    t = (msg.text or "").strip()
    if t == "/cancel":
        await state.clear()
        return await msg.answer("لغو شد.")
    if not t.isdigit() or int(t) > 3650:
        return await msg.answer("⚠️ یک عددِ صحیح (۰ تا ۳۶۵۰) بفرست.")
    d = await state.get_data()
    gb, days, key = d.get("ptl_gb", 0), int(t), d.get("ptl_key")
    if not gb and not days:
        await state.clear()
        return await msg.answer("هر دو صفر بود؛ کاری انجام نشد.")
    await state.update_data(ptl_days=days)
    n = len(await _active_accounts(row.id, key))
    await msg.answer(
        f"⚠️ <b>تأیید</b>\n\nبه <b>{n}</b> سرویسِ فعال: +{gb} گیگ و +{days} روز اضافه می‌شود.\n"
        "این کار برگشت‌پذیر نیست. ادامه می‌دهی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، اعمال کن", callback_data="ptl:bulkgo",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"tvpn:panel:{key}",
                                  style=ButtonStyle.DANGER)]]))


@panel_tools_router.callback_query(F.data == "ptl:bulkgo")
async def ptl_bulk_go(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    d = await state.get_data()
    key, gb, days = d.get("ptl_key"), int(d.get("ptl_gb", 0)), int(d.get("ptl_days", 0))
    await state.clear()
    if not key or (not gb and not days):
        return await cb.answer("منقضی شد؛ دوباره شروع کن.", show_alert=True)
    await cb.answer("شروع شد…")
    await cb.message.edit_text("⏳ در حالِ اعمال روی پنل… پایان که رسید خبرت می‌کنم.")
    chat_id = cb.message.chat.id
    asyncio.create_task(_bulk_run(bot, chat_id, row.id, key, gb, days))


async def _bulk_run(bot: Bot, chat_id: int, bot_id: int, key: str, gb: int, days: int):
    ok = fail = 0
    try:
        work = await _active_accounts(bot_id, key)
        client = await _client_for(bot_id, key)
        if client is None:
            return await bot.send_message(chat_id, "❌ اتصال به پنل انجام نشد.")
        try:
            consec = 0
            for aid, email, _uid, vol, _dd in work:
                if consec >= 5:
                    fail += 1
                    continue
                try:
                    if days:
                        await client.renew(email, days)
                    if gb and vol:  # vol==0 یعنی نامحدود
                        await client.add_traffic(email, gb)
                    async with Session() as s:
                        a = await s.get(PanelAccount, aid)
                        if a:
                            if days:
                                base = a.expires_at if a.expires_at and a.expires_at > datetime.utcnow() else datetime.utcnow()
                                a.expires_at = base + timedelta(days=days)
                                a.days = (a.days or 0) + days
                                a.expiry_notice = None
                                a.expiry_warn_count = 0
                            if gb and a.volume_gb:
                                a.volume_gb = (a.volume_gb or 0) + gb
                            await s.commit()
                    ok += 1
                    consec = 0
                except Exception as e:
                    fail += 1
                    consec += 1
                    logger.warning("ptl bulk %s: %s", email, str(e)[:150])
                await asyncio.sleep(0.15)
        finally:
            await _close(client)
        await bot.send_message(
            chat_id,
            f"✅ <b>افزودنِ کلی تمام شد</b>\n\nموفق: {ok}\nناموفق: {fail}\n"
            f"مقدار: +{gb} گیگ / +{days} روز",
            reply_markup=_panel_back(key))
    except Exception:
        logger.exception("ptl bulk run")
        try:
            await bot.send_message(chat_id, f"⚠️ عملیات با خطا قطع شد (موفق: {ok}، ناموفق: {fail}).")
        except Exception:
            logger.info("ارسالِ پیامِ خطایِ عملیاتِ گروهی ممکن نشد")


# ═══════════════════════ ۲) افزودنِ دستیِ سرویس ═══════════════════════
@panel_tools_router.callback_query(F.data.startswith("ptl:add:"))
async def ptl_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):
        return await cb.answer("پنل نامعتبر", show_alert=True)
    await state.clear()
    await state.update_data(ptl_key=key)
    await state.set_state(PtlFSM.add_uid)
    await cb.message.edit_text(
        "🧷 <b>افزودنِ دستیِ سرویس</b>\n\n"
        "سرویسی که <b>از قبل روی پنل ساخته شده</b> را به یک کاربرِ ربات وصل می‌کنی "
        "تا در «سرویس‌هایِ من» ببیند و بتواند تمدید کند.\n\n"
        "۱) <b>آیدی عددیِ تلگرامِ کاربر</b> را بفرست.\n(لغو: /cancel)",
        reply_markup=_panel_back(key))
    await cb.answer()


@panel_tools_router.message(PtlFSM.add_uid, F.text)
async def ptl_add_uid(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    t = (msg.text or "").strip()
    if t == "/cancel":
        await state.clear()
        return await msg.answer("لغو شد.")
    if not t.isdigit():
        return await msg.answer("⚠️ آیدی عددی بفرست (فقط رقم).")
    await state.update_data(ptl_uid=int(t))
    await state.set_state(PtlFSM.add_email)
    await msg.answer("۲) <b>نامِ کاربری (email)</b> سرویس را همان‌طور که روی پنل هست بفرست:")


async def _find_account_on_panel(client, email: str):
    """=== PATCH_PANEL_TOOLS_FIND_V1 === پیدا کردنِ سرویس روی پنل با چند روش:
    نامِ دقیق ← حروفِ کوچک ← جست‌وجو در لیستِ کاربرانِ پنل (بدونِ حساسیت به حروف).
    خروجی: (نامِ واقعیِ پنل، اطلاعاتِ کلاینت، مصرف، نام‌هایِ شبیه)."""
    email = (email or "").strip()
    names = [email] + ([email.lower()] if email.lower() != email else [])
    for n in names:
        cur = usage = None
        try:
            cur = await client.get_client(n)
        except Exception:
            logger.info("ptl find get_client(%r) failed", n)
        try:
            usage = await client.get_usage(n)
        except Exception:
            logger.info("ptl find get_usage(%r) failed", n)
        if cur or usage:
            return n, cur, usage, []
    try:
        items = await client.list_clients() or []
    except Exception:
        logger.info("ptl find list_clients failed")
        return email, None, None, []
    low, similar = email.lower(), []
    for it in items:
        if not isinstance(it, dict):
            continue
        nm = str(it.get("email") or it.get("username") or it.get("name") or "").strip()
        if not nm:
            continue
        if nm.lower() == low:
            cur = usage = None
            try:
                cur = await client.get_client(nm)
            except Exception:
                logger.info("ptl find get_client(%r) failed", nm)
            try:
                usage = await client.get_usage(nm)
            except Exception:
                logger.info("ptl find get_usage(%r) failed", nm)
            return nm, (cur or it), usage, []
        if low and (low in nm.lower() or nm.lower() in low):
            similar.append(nm)
    return email, None, None, similar[:6]


# === PATCH_MANUAL_LINK_LIMITS_V1 ===
def panel_limits(cur: dict | None, usage: dict | None) -> tuple[int, int | None, int, datetime | None]:
    """حجم و انقضایِ یک سرویسِ موجود روی پنل، از هر دو شکلِ پاسخ:
    x-ui (total/totalGB بایت، expiryTime میلی‌ثانیه؛ منفی = «از اولین اتصال») و
    خانوادهٔ مرزبان/یوپنل (data_limit بایت، expire ثانیه یا ISO).
    خروجی: (گیگ، مگ برایِ زیرِ ۱ گیگ یا None، روزِ باقی‌مانده، تاریخِ انقضا یا None).
    ۰ فقط وقتی برمی‌گردد که پنل واقعاً نامحدود/بی‌انقضا باشد."""
    from tenant.shared.panel_clients.marzban_panel import _expire_to_epoch
    cur = cur if isinstance(cur, dict) else {}
    usage = usage if isinstance(usage, dict) else {}

    def _int(v) -> int:
        try:
            return int(float(v or 0))
        except (TypeError, ValueError):
            return 0
    total = _int(usage.get("total")) or _int(cur.get("totalGB")) or _int(cur.get("total")) \
        or _int(cur.get("data_limit"))
    gb, mb = 0, None
    if total > 0:
        if total < 1024 ** 3:
            mb = max(1, int(round(total / (1024 ** 2))))
        else:
            gb = int(round(total / (1024 ** 3)))
    exp_ms = _int(usage.get("expiryTime")) or _int(cur.get("expiryTime"))
    if not exp_ms and cur.get("expire"):
        try:
            exp_ms = _expire_to_epoch(cur.get("expire")) * 1000
        except (TypeError, ValueError):
            exp_ms = 0
    expires, days = None, 0
    if exp_ms > 0:
        expires = datetime.utcfromtimestamp(exp_ms / 1000)
        left = (expires - datetime.utcnow()).total_seconds()
        days = int(left // 86400) + (1 if left % 86400 else 0) if left > 0 else 0
    elif exp_ms < 0:          # x-ui: «شروع از اولین اتصال» — مدت، نه تاریخ
        days = max(1, int(abs(exp_ms) // 86_400_000))
    return gb, mb, days, expires


async def repair_manual_accounts(limit: int = 500) -> int:
    """سرویس‌هایِ دستیِ قبلی که با باگِ خواندن «نامحدود / بی‌انقضا» (۰/۰) ثبت شدند را
    از رویِ پنل اصلاح می‌کند. فقط ردیف‌هایِ ۰/۰ِ «سرویس دستی» دست می‌خورند."""
    async with Session() as s:
        rows = (await s.scalars(select(PanelAccount).where(
            PanelAccount.plan_title == "سرویس دستی", PanelAccount.status == "active",
            PanelAccount.volume_gb == 0, PanelAccount.volume_mb.is_(None),
            PanelAccount.days == 0, PanelAccount.expires_at.is_(None)).limit(limit))).all()
        items = [(r.id, r.bot_id, r.panel_key, r.email) for r in rows]
    fixed = 0
    clients: dict = {}
    try:
        for aid, bid, key, email in items:
            ck = (bid, key)
            if ck not in clients:
                try:
                    clients[ck] = await _client_for(bid, key)
                except Exception as e:
                    logger.info("repair manual: پنلِ %s/%s در دسترس نیست: %s", bid, key, e)
                    clients[ck] = None
            c = clients[ck]
            if c is None:
                continue
            try:
                cur = await c.get_client(email)
                usage = await c.get_usage(email) if cur else None
            except Exception as e:
                logger.info("repair manual: خواندنِ %s ناموفق: %s", email, e)
                continue
            if not cur:
                continue
            gb, mb, days, expires = panel_limits(cur, usage)
            if not (gb or mb or days or expires):
                continue          # واقعاً نامحدود/بی‌انقضاست
            async with Session() as s:
                a = await s.get(PanelAccount, aid)
                if a and a.volume_gb == 0 and a.volume_mb is None and a.days == 0 and a.expires_at is None:
                    a.volume_gb, a.volume_mb, a.days, a.expires_at = gb, mb, days, expires
                    await s.commit()
                    fixed += 1
    finally:
        for c in clients.values():
            if c is not None:
                await _close(c)
    if fixed:
        logger.info("repair manual: %s سرویسِ دستی اصلاح شد", fixed)
    return fixed


async def repair_manual_accounts_once() -> None:
    """یک‌بار پس از استقرار (پرچم در تنظیماتِ سطحِ پلتفرم)."""
    import asyncio as _aio
    await _aio.sleep(90)
    flag = "repair_manual_limits_v1"
    try:
        async with Session() as s:
            done = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == flag))
        if done:
            return
        await repair_manual_accounts()
        async with Session() as s:
            s.add(Setting(bot_id=0, key=flag, value="1"))
            await s.commit()
    except Exception:
        logger.exception("repair manual: اصلاحِ یک‌باره ناموفق بود")


@panel_tools_router.message(PtlFSM.add_email, F.text)
async def ptl_add_email(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    email = (msg.text or "").strip()
    if email == "/cancel":
        await state.clear()
        return await msg.answer("لغو شد.")
    d = await state.get_data()
    key, uid = d.get("ptl_key"), d.get("ptl_uid")
    await state.clear()
    if not key or not uid:
        return await msg.answer("منقضی شد؛ دوباره شروع کن.")
    async with Session() as s:
        dup = await s.scalar(select(PanelAccount).where(
            PanelAccount.bot_id == row.id, PanelAccount.email == email,
            PanelAccount.panel_key == key))
    if dup:
        return await msg.answer(
            f"⚠️ این سرویس قبلاً ثبت شده (کاربر <code>{dup.user_telegram_id}</code>).",
            reply_markup=_panel_back(key))
    wait = await msg.answer("⏳ در حالِ خواندنِ سرویس از پنل…")
    try:
        client = await _client_for(row.id, key)
        if client is None:
            return await wait.edit_text("❌ اتصال به پنل انجام نشد.", reply_markup=_panel_back(key))
        try:
            email, cur, usage, similar = await _find_account_on_panel(client, email)
            if not cur and not usage:
                _sim = ("\n\nنام‌هایِ شبیه روی پنل:\n" + "\n".join(f"• <code>{x}</code>" for x in similar)
                        if similar else "")
                return await wait.edit_text(
                    "❌ سرویسی با این نام روی پنل پیدا نشد.\n"
                    "• نامِ کاربری را دقیقاً همان‌طور که در «لیستِ کاربران/کلاینت‌هایِ پنل» هست بفرست.\n"
                    "• مطمئن شو همین پنل (نه پنلِ دیگرِ ربات) را باز کرده‌ای." + _sim,
                    reply_markup=_panel_back(key), parse_mode="HTML")
            cur = cur if isinstance(cur, dict) else {}
            usage = usage if isinstance(usage, dict) else {}
            if _bk(key) in _MARZBAN_FAMILY:
                sub_link = cur.get("sub_link") or ""
                sub_id = cur.get("subId") or ""
            else:
                sub_id = cur.get("subId") or ""
                if hasattr(client, "ensure_sub_base"):
                    await client.ensure_sub_base()   # PATCH_SUB_BASE_DISCOVER_V1
                sub_link = client.sub_link(sub_id) if sub_id else ""
        finally:
            await _close(client)
        async with Session() as s:   # نامِ واقعیِ پنل ممکن است با نامِ تایپ‌شده فرق (حروف) داشته باشد
            dup2 = await s.scalar(select(PanelAccount).where(
                PanelAccount.bot_id == row.id, PanelAccount.email == email,
                PanelAccount.panel_key == key))
        if dup2:
            return await wait.edit_text(
                f"⚠️ این سرویس قبلاً ثبت شده (کاربر <code>{dup2.user_telegram_id}</code>).",
                reply_markup=_panel_back(key), parse_mode="HTML")
        gb, mb, days, expires = panel_limits(cur, usage)
        from tenant.bots.vpn.vpn_user import new_svc_code
        async with Session() as s:
            acc = PanelAccount(
                bot_id=row.id, user_telegram_id=int(uid), email=email, panel_key=key,
                uuid=str(cur.get("id") or cur.get("uuid") or "") or None,
                sub_id=sub_id or None, sub_link=sub_link or None,
                volume_gb=gb, volume_mb=mb, days=days, plan_title="سرویس دستی", status="active",
                svc_code=await new_svc_code(s, row.id), expires_at=expires)
            s.add(acc)
            await s.commit()
            aid = acc.id
        try:
            await bot.send_message(int(uid), "✅ یک سرویسِ VPN به حسابت اضافه شد.\n"
                                   "از «📡 سرویس‌هایِ من» می‌توانی ببینی و مدیریتش کنی.")
        except Exception:
            logger.info("اطلاعِ سرویسِ تازه به کاربر %s نرسید", uid)
        await wait.edit_text(
            f"✅ سرویسِ <code>{email}</code> به کاربرِ <code>{uid}</code> وصل شد.\n"
            f"حجم: {(f'{mb} مگابایت' if mb else (f'{gb} گیگ' if gb else 'نامحدود'))} | "
            f"مدتِ باقی‌مانده: {(f'{days} روز' if days else 'بی‌انقضا')}",
            reply_markup=_panel_back(key))
    except Exception as e:
        logger.exception("ptl add manual")
        await wait.edit_text(f"❌ خطا: {str(e)[:150]}", reply_markup=_panel_back(key))


# ═══════════════════════ ۱) بکاپ و بازیابی ═══════════════════════
async def _collect_usage(bot_id: int, accounts: list[tuple[str, str]]) -> dict:
    """=== PATCH_BACKUP_REMAINING_V1 === مصرفِ زندهٔ هر سرویس از خودِ پنل:
    {(email, panel_key): (used_bytes, expiry_ms)}. هر پنلی که جواب ندهد رد می‌شود
    (بکاپ بدونِ مصرفِ آن پنل ساخته می‌شود، نه اینکه شکست بخورد)."""
    out: dict = {}
    by_panel: dict[str, list[str]] = {}
    for email, pk in accounts:
        by_panel.setdefault(pk or "", []).append(email)
    for pk, emails in by_panel.items():
        p = await get_panel(bot_id, pk) if pk else None
        if not p or not p.enabled:
            continue
        client = None
        try:
            client = make_panel_client(p)
            await asyncio.wait_for(client.login(), timeout=40)
            sem = asyncio.Semaphore(5)
            fails = {"n": 0}

            async def _one(em):
                if fails["n"] >= 6:
                    return
                async with sem:
                    try:
                        u = await asyncio.wait_for(client.get_usage(em), timeout=15)
                        fails["n"] = 0
                    except Exception:
                        fails["n"] += 1
                        return
                if isinstance(u, dict):
                    out[(em, pk)] = (int(u.get("up") or 0) + int(u.get("down") or 0),
                                     int(u.get("expiryTime") or 0))

            await asyncio.wait_for(asyncio.gather(*[_one(e) for e in emails]), timeout=900)
        except Exception as e:
            logger.warning("backup usage panel %s: %s", pk, str(e)[:120])
        finally:
            if client is not None:
                await _close(client)
    return out


async def _build_accounts_backup(bot_id: int, with_usage: bool = True, panel_key: str | None = None) -> bytes:
    """=== PATCH_BACKUP_PER_PANEL_V1 === panel_key داده شود ⇒ فقط سرویس‌هایِ همان پنل (برایِ هر پنل یک فایلِ جدا)."""
    async with Session() as s:
        q = select(PanelAccount).where(PanelAccount.bot_id == bot_id)
        if panel_key is not None:
            q = q.where(or_(PanelAccount.panel_key == panel_key, PanelAccount.panel_key.is_(None))
                        if panel_key == "" else PanelAccount.panel_key == panel_key)
        rows = (await s.scalars(q)).all()
        usage = {}
        if with_usage:
            usage = await _collect_usage(
                bot_id, [(a.email, a.panel_key or "") for a in rows if (a.status or "active") == "active"])
        accs = [{
            "user_telegram_id": a.user_telegram_id, "email": a.email, "panel_key": a.panel_key,
            "uuid": a.uuid, "sub_id": a.sub_id, "sub_link": a.sub_link,
            "volume_gb": a.volume_gb, "volume_mb": a.volume_mb, "days": a.days,
            "plan_title": a.plan_title, "svc_code": a.svc_code, "status": a.status,
            "paid_toman": a.paid_toman,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            # مصرف و انقضایِ زندهٔ پنل در لحظهٔ بکاپ (None = خوانده نشد)
            "used_bytes": (usage.get((a.email, a.panel_key or "")) or (None, None))[0],
            "live_expiry_ms": (usage.get((a.email, a.panel_key or "")) or (None, None))[1],
        } for a in rows]
    data = {"magic": BACKUP_MAGIC, "bot_id": bot_id, "panel_key": panel_key,
            "created": datetime.utcnow().isoformat(), "accounts": accs}
    return _bk_encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _bk_filename(bot_id: int, panel_key: str) -> str:
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_-]+", "_", panel_key or "panel").strip("_") or "panel"
    return f"vpn_accounts_{bot_id}_{safe}_{datetime.utcnow():%Y%m%d_%H%M}.botsaz"


def _bk_count(accounts_blob: bytes) -> int:
    try:
        return len(json.loads(_bk_decrypt(accounts_blob).decode("utf-8"))["accounts"])
    except Exception:
        return 0


async def _bk_menu_text(bot_id: int) -> str:
    hrs = int(await _setting(bot_id, "vpn_backup_hours", "0") or 0)
    last = await _setting(bot_id, "vpn_backup_last", "")
    return ("💾 <b>بکاپ و بازیابیِ سرویس‌هایِ کاربران</b>\n\n"
            f"⏱ بکاپِ خودکار: <b>{'خاموش' if not hrs else f'هر {hrs} ساعت'}</b>\n"
            f"🕒 آخرین بکاپ: {last or '—'}\n\n"
            "برایِ هر پنل یک فایلِ جدا برایِ مالکِ ربات فرستاده می‌شود. با «بازیابی» سرویس‌ها از فایل "
            "در ربات دوباره ثبت می‌شوند (مثلاً بعد از انتقال به رباتِ جدید).\n"
            "🔐 فایل رمزنگاری‌شده (.botsaz) است و فقط رباتِ بات‌ساز می‌تواند بازش کند.")


@panel_tools_router.callback_query(F.data.startswith("ptl:bk:"))
async def ptl_bk_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    hrs = int(await _setting(row.id, "vpn_backup_hours", "0") or 0)
    rows = [[InlineKeyboardButton(text="📤 همین الان بکاپ بگیر", callback_data=f"ptl:bknow:{key}",
                                  style=ButtonStyle.SUCCESS)]]
    opts = []
    for h in _BACKUP_CHOICES:
        lbl = "خاموش" if h == 0 else f"{h}س"
        opts.append(InlineKeyboardButton(
            text=("✅ " if h == hrs else "") + lbl, callback_data=f"ptl:bkh:{key}:{h}"))
    rows.append(opts[:3])
    rows.append(opts[3:])
    rows.append([InlineKeyboardButton(text="📥 بازیابی از فایل", callback_data=f"ptl:bkr:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"tvpn:panel:{key}",
                                      style=ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text(await _bk_menu_text(row.id),
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass
    await cb.answer()


@panel_tools_router.callback_query(F.data.startswith("ptl:bkh:"))
async def ptl_bk_hours(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, h = cb.data.split(":")
    h = int(h)
    if h not in _BACKUP_CHOICES:
        return await cb.answer()
    await _set_setting(row.id, "vpn_backup_hours", str(h))
    await cb.answer("خاموش شد." if h == 0 else f"✅ هر {h} ساعت بکاپ می‌فرستم.", show_alert=True)
    # CallbackQuery غیرقابلِ‌تغییر است (قبلاً اینجا ValidationError می‌داد و منو تازه نمی‌شد)
    await ptl_bk_menu(cb.model_copy(update={"data": f"ptl:bk:{key}"}), bot)


@panel_tools_router.callback_query(F.data.startswith("ptl:bknow:"))
async def ptl_bk_now(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    await cb.answer("⏳ در حالِ خواندنِ مصرفِ سرویس‌ها از پنل…", show_alert=False)
    try:
        await cb.message.answer("⏳ در حالِ ساختِ بکاپ (مصرفِ فعلیِ هر سرویس هم از پنل خوانده می‌شود)… "
                                "برایِ پنل‌هایِ بزرگ ممکن است چند دقیقه طول بکشد.")
    except Exception:
        pass
    # === PATCH_BACKUP_PER_PANEL_V1 === فقط همین پنل؛ قبلاً با چند پنل همهٔ سرویس‌ها در یک فایل ریخته می‌شد
    key = cb.data.split(":", 2)[2]
    raw = await _build_accounts_backup(row.id, panel_key=key)
    n = _bk_count(raw)
    if not n:
        await cb.message.answer("ℹ️ این پنل هنوز سرویسی ندارد؛ بکاپی ساخته نشد.")
        return
    from tenant.bots.vpn.vpn_admin import panel_label
    await cb.message.answer_document(
        BufferedInputFile(raw, filename=_bk_filename(row.id, key)),
        caption=f"💾 بکاپِ پنلِ «{await panel_label(row.id, key)}» — {n} سرویس")
    await _set_setting(row.id, "vpn_backup_last", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))


@panel_tools_router.callback_query(F.data.startswith("ptl:bkr:"))
async def ptl_bk_restore_mode(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    label = PANEL_LABELS.get(_bk(key), key)
    await cb.message.edit_text(
        "📥 <b>بازیابی از فایلِ بکاپ</b>\n\nحالتِ بازیابی را انتخاب کن:\n\n"
        "🤖 <b>فقط در ربات</b>: سرویس‌ها فقط در «سرویس‌هایِ من» کاربران ثبت می‌شوند "
        "(روی پنل چیزی ساخته نمی‌شود).\n\n"
        f"🛰 <b>در ربات + ساخت روی پنل</b>: علاوه بر ربات، سرویس‌هایِ مربوط به «{label}» "
        "که روی این پنل نیستند با همان uuid و ساب‌آیدی دوباره ساخته می‌شوند. "
        "سرویس‌هایی که روی پنل هستند رد می‌شوند و منقضی‌ها ساخته نمی‌شوند. "
        "سرویس‌ها با <b>حجم و زمانِ باقی‌مانده</b> (طبقِ مصرفِ لحظهٔ بکاپ) ساخته می‌شوند؛ اگر در بکاپ مصرف ثبت نشده باشد، با حجمِ کل.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 فقط در ربات", callback_data=f"ptl:bkm:{key}:db")],
            [InlineKeyboardButton(text="🛰 در ربات + ساخت روی پنل", callback_data=f"ptl:bkm:{key}:panel",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"ptl:bk:{key}",
                                  style=ButtonStyle.DANGER)]]))
    await cb.answer()


@panel_tools_router.callback_query(F.data.startswith("ptl:bkm:"))
async def ptl_bk_restore_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, mode = cb.data.split(":")
    await state.clear()
    await state.update_data(ptl_key=key, ptl_mode=mode)
    await state.set_state(PtlFSM.restore_file)
    await cb.message.edit_text(
        "📥 فایلِ بکاپِ سرویس‌ها (.botsaz) را بفرست.\n"
        "چیزی پاک نمی‌شود؛ موارد تکراری رد می‌شوند.\n(لغو: /cancel)",
        reply_markup=_panel_back(key))
    await cb.answer()


@panel_tools_router.message(PtlFSM.restore_file, F.document)
async def ptl_bk_restore_file(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    d = await state.get_data()
    key, mode = d.get("ptl_key"), d.get("ptl_mode", "db")
    await state.clear()
    try:
        f = await bot.get_file(msg.document.file_id)
        buf = await bot.download_file(f.file_path)
        data = json.loads(_bk_decrypt(buf.read()).decode("utf-8"))
        assert data.get("magic") == BACKUP_MAGIC
        accs = data["accounts"]
    except Exception:
        return await msg.answer("❌ فایل معتبر نیست (باید همان فایلِ .botsaz بکاپِ سرویس‌ها باشد؛ فایلِ دست‌خورده یا رمزِ دیگر قابلِ‌خواندن نیست).")
    await msg.answer(f"⏳ بازیابی {len(accs)} سرویس شروع شد… پایان که رسید خبرت می‌کنم.")
    asyncio.create_task(_restore_run(bot, msg.chat.id, row.id, key, mode, accs))


def _remaining_days_ms(ms: int) -> int:
    left = ms / 1000 - datetime.utcnow().timestamp()
    return 0 if left <= 0 else int(left // 86400) + 1


def _remaining_days(exp_iso: str | None) -> int | None:
    """None=بی‌انقضا، 0=منقضی‌شده، وگرنه روزِ باقی‌مانده (رو به بالا)."""
    if not exp_iso:
        return None
    left = (datetime.fromisoformat(exp_iso) - datetime.utcnow()).total_seconds()
    if left <= 0:
        return 0
    return int(left // 86400) + 1


async def _restore_run(bot: Bot, chat_id: int, bot_id: int, key: str, mode: str, accs: list):
    added = skipped = 0
    p_ok = p_exist = p_exp = p_fail = p_link = p_rem = p_empty = 0
    try:
        client = None
        if mode == "panel":
            try:
                client = await _client_for(bot_id, key)
            except Exception as e:
                logger.warning("restore panel login: %s", e)
            if client is None:
                await bot.send_message(chat_id, "❌ اتصال به پنل انجام نشد؛ بازیابی روی پنل انجام نمی‌شود "
                                                "(می‌توانی «فقط در ربات» را بزنی).")
                return
        try:
            consec = 0
            for a in accs:
                email = a.get("email")
                if not email:
                    skipped += 1
                    continue
                async with Session() as s:
                    acc = await s.scalar(select(PanelAccount).where(
                        PanelAccount.bot_id == bot_id, PanelAccount.email == email,
                        PanelAccount.panel_key == a.get("panel_key")))
                    if acc is None:
                        exp = a.get("expires_at")
                        acc = PanelAccount(
                            bot_id=bot_id, user_telegram_id=int(a["user_telegram_id"]), email=email,
                            panel_key=a.get("panel_key"), uuid=a.get("uuid"), sub_id=a.get("sub_id"),
                            sub_link=a.get("sub_link"), volume_gb=int(a.get("volume_gb") or 0),
                            volume_mb=a.get("volume_mb"), days=int(a.get("days") or 0),
                            plan_title=a.get("plan_title"), svc_code=a.get("svc_code"),
                            status=a.get("status") or "active", paid_toman=a.get("paid_toman"),
                            expires_at=datetime.fromisoformat(exp) if exp else None)
                        s.add(acc)
                        await s.commit()
                        added += 1
                    else:
                        skipped += 1
                    acc_id = acc.id
                if client is None or a.get("panel_key") != key or (a.get("status") or "active") != "active":
                    continue
                if consec >= 5:
                    p_fail += 1
                    continue
                # === PATCH_BACKUP_REMAINING_V1 === انقضا: اولویت با مقدارِ زندهٔ پنل در لحظهٔ بکاپ
                _lm = a.get("live_expiry_ms")
                if _lm is not None:
                    if _lm == 0:
                        rem = None            # بی‌انقضا
                    elif _lm < 0:
                        rem = max(1, int(abs(_lm) // 86400000))   # «از اولین اتصال»: همان مدت
                    else:
                        rem = _remaining_days_ms(_lm)
                else:
                    rem = _remaining_days(a.get("expires_at"))
                if rem == 0:
                    p_exp += 1
                    continue
                try:
                    if await client.get_client(email):
                        p_exist += 1
                        consec = 0
                        continue
                    kw = dict(uuid_str=a.get("uuid") or None, sub_id=a.get("sub_id") or None)
                    _total = int(a.get("volume_mb") or 0) * 1024 * 1024 or int(a.get("volume_gb") or 0) * 1024 ** 3
                    _used = a.get("used_bytes")
                    if _total and _used is not None:
                        # حجمِ باقی‌مانده (نه حجمِ کل)؛ اگر تمام شده بود حداقلِ ۱ مگ تا بعداً قابلِ تمدید باشد
                        _left = _total - int(_used)
                        if _left <= 0:
                            p_empty += 1
                        res = await client.create_account(
                            email, 0, rem or 0, mb=max(1, _left // (1024 * 1024)), **kw)
                        p_rem += 1
                    elif a.get("volume_mb"):
                        res = await client.create_account(email, 0, rem or 0, mb=int(a["volume_mb"]), **kw)
                    else:
                        res = await client.create_account(email, int(a.get("volume_gb") or 0), rem or 0, **kw)
                    consec = 0
                    p_ok += 1
                    new_link = (res or {}).get("sub_link") or ""
                    async with Session() as s:
                        r2 = await s.get(PanelAccount, acc_id)
                        if r2:
                            if res.get("uuid"):
                                r2.uuid = res["uuid"]
                            if res.get("sub_id"):
                                r2.sub_id = res["sub_id"]
                            if new_link and new_link != (r2.sub_link or ""):
                                r2.sub_link = new_link
                                p_link += 1
                            await s.commit()
                except Exception as e:
                    consec += 1
                    p_fail += 1
                    logger.warning("restore panel %s: %s", email, str(e)[:150])
                await asyncio.sleep(0.15)
        finally:
            if client is not None:
                await _close(client)
        txt = f"✅ <b>بازیابی تمام شد</b>\n\n🤖 ربات: افزوده {added} | تکراری {skipped}"
        if mode == "panel":
            txt += (f"\n🛰 پنل: ساخته‌شده {p_ok} | از قبل بود {p_exist} | منقضی (ساخته نشد) {p_exp} | ناموفق {p_fail}")
            if p_rem:
                txt += f"\n📊 با حجمِ <b>باقی‌مانده</b> ساخته شد: {p_rem}" + (f" (حجمِ {p_empty} سرویس تمام شده بود → ۱ مگ)" if p_empty else "")
            if p_link:
                txt += (f"\n\n⚠️ لینکِ اشتراکِ {p_link} سرویس عوض شد (دامنه/نوعِ پنل فرق داشت). "
                        "از «🔁 بازسازی لینک‌ها» برایِ خبردادن به کاربران استفاده کن.")
        await bot.send_message(chat_id, txt, reply_markup=_panel_back(key))
    except Exception:
        logger.exception("ptl restore run")
        try:
            await bot.send_message(chat_id, "⚠️ بازیابی با خطا قطع شد؛ لاگ را ببین.")
        except Exception:
            logger.info("ارسالِ پیامِ خطایِ بازیابی ممکن نشد")


@panel_tools_router.message(PtlFSM.restore_file, F.text)
async def ptl_bk_restore_cancel(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    await state.clear()
    await msg.answer("لغو شد.")


# ─────────── حلقهٔ بکاپِ خودکار (در worker_tenant اسپان می‌شود) ───────────
async def vpn_backup_once() -> None:
    async with Session() as s:
        cfg = (await s.scalars(select(Setting).where(
            Setting.key == "vpn_backup_hours"))).all()
        due_list = [(c.bot_id, int(c.value)) for c in cfg if (c.value or "").isdigit() and int(c.value) > 0]
    for bot_id, hrs in due_list:
        last = await _setting(bot_id, "vpn_backup_last", "")
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M UTC") if last else None
        except ValueError:
            last_dt = None
        if last_dt and datetime.utcnow() - last_dt < timedelta(hours=hrs):
            continue
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if not brow or brow.status != "active" or not brow.owner_id:
            continue
        try:
            # === PATCH_BACKUP_PER_PANEL_V1 === برایِ هر پنل یک فایلِ جدا (قبلاً همه با هم در یک فایل می‌آمد)
            async with Session() as s:
                keys = sorted({k or "" for k in (await s.scalars(select(PanelAccount.panel_key).where(
                    PanelAccount.bot_id == bot_id))).all()})
            from tenant.bots.vpn.vpn_admin import panel_label
            sent_any = False
            tb = Bot(token=brow.token, default=DefaultBotProperties(parse_mode="HTML"))
            try:
                for pk in keys:
                    raw = await _build_accounts_backup(bot_id, panel_key=pk)
                    _n = _bk_count(raw)
                    if not _n:
                        continue
                    await tb.send_document(
                        brow.owner_id,
                        BufferedInputFile(raw, filename=_bk_filename(bot_id, pk)),
                        caption=f"💾 بکاپِ خودکارِ پنلِ «{await panel_label(bot_id, pk) if pk else 'بدونِ پنل'}» — {_n} سرویس")
                    sent_any = True
                    await asyncio.sleep(0.5)
            finally:
                await tb.session.close()
            if sent_any or not keys:
                await _set_setting(bot_id, "vpn_backup_last", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
        except Exception as e:
            logger.warning("vpn_backup bot %s: %s", bot_id, str(e)[:150])
        await asyncio.sleep(1)


async def vpn_backup_loop(interval_sec: int = 600) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await vpn_backup_once()
        except Exception:
            logger.exception("vpn_backup_loop error")
        await asyncio.sleep(interval_sec)
