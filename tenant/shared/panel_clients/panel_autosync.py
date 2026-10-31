"""
panel_autosync.py — همگام‌سازیِ خودکارِ اینباندها و ساب‌لینکِ کاربران
(=== PATCH_PANEL_AUTOSYNC_V1 ===).

همان کاری که دکمهٔ دستیِ «🔄 بروزرسانی اینباندها و ساب‌لینک کاربران»
(tvpn:refresh_subs در vpn_admin.py) می‌کند، ولی خودکار و بی‌صدا هر ۲ ساعت.
مالکِ ربات از منویِ خودِ پنل روشن/خاموشش می‌کند (VpnPanel.auto_sync).

چرا دوباره‌نویسیِ منطق و نه صداکردنِ همان هندلر: آن هندلر به یک
CallbackQuery وابسته است و در هر مرحله به مالک پیام می‌دهد؛ اینجا نه
پیامی هست و نه ادمینی که منتظر باشد. منطقِ داده‌ای اما دقیقاً همان است.

اجرا: حلقهٔ panel_autosync_loop در worker_tenant.py اسپان می‌شود (نه در
پروسهٔ HTTP — طبقِ PATCH_BG_LOOPS_MOVED_TO_WORKER_V1، چون با پنل‌های کندِ
بیرونی کار می‌کند).

به کاربران هیچ پیامی داده نمی‌شود؛ برای خبردادنِ لینکِ جدید به مشتری‌ها
همچنان panel_relink.py (دکمهٔ «بازسازیِ لینک‌ها») مسیرِ درست است.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton
from sqlalchemy import select

from config import Session
from models import PanelAccount, VpnPanel
from tenant.bots.vpn.vpn_admin import PANEL_LABELS, _bk, _owner, get_panel, is_panel_key
from tenant.shared.panel_clients import panel_inbounds as PI  # === PATCH_PANEL_INBOUND_PICKER_V1 ===
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

panel_autosync_router = Router()

# فاصلهٔ اجرا برایِ هر پنل (ساعت) و فاصلهٔ بیدارشدنِ خودِ حلقه (ثانیه).
AUTOSYNC_EVERY_HOURS = 2
_LOOP_INTERVAL_SEC = 900          # هر ۱۵ دقیقه نگاه می‌کند نوبتِ کدام پنل رسیده
_BETWEEN_PANELS_SLEEP = 2.0       # فشار نیاوردن به پنل‌ها/دیتابیس
_BETWEEN_ACCOUNTS_SLEEP = 0.05

# خانوادهٔ مرزبان: لینکِ ساب را خودِ پنل برمی‌گرداند و از روی sub_id قابلِ
# بازسازی نیست (همان تفکیکی که در vpn_admin.vpn_refresh_subs هست).
_MARZBAN_FAMILY = ("marzban", "rebecca", "pasargad", "pasarguard",
                   "marzneshin", "upanel_mizban", "rickpanel", "solidlayer")


# ───────────────────────── دکمهٔ منوی پنل ─────────────────────────
def autosync_button(key: str, panel: VpnPanel | None) -> list[InlineKeyboardButton] | None:
    """ردیفِ دکمهٔ روشن/خاموشِ همگام‌سازیِ خودکار برایِ منویِ همین پنل."""
    if not panel:
        return None
    on = bool(getattr(panel, "auto_sync", False))
    return [InlineKeyboardButton(
        text=(f"🕒 بروزرسانی خودکار (هر {AUTOSYNC_EVERY_HOURS} ساعت): ✅ روشن" if on
              else f"🕒 بروزرسانی خودکار (هر {AUTOSYNC_EVERY_HOURS} ساعت): ⛔️ خاموش"),
        callback_data=f"tvpn:autosync:{key}",
        # لیبل شرطی است، پس رنگ هم باید با آن بچرخد.
        style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))]


@panel_autosync_router.callback_query(F.data.startswith("tvpn:autosync:"))
async def autosync_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    panel = await get_panel(row.id, key)
    if not panel:
        await cb.answer("اول تنظیمات پنل را ثبت کن.", show_alert=True)
        return
    async with Session() as s:
        p = await s.get(VpnPanel, panel.id, with_for_update=True)
        if not p:
            await cb.answer("پنل یافت نشد", show_alert=True)
            return
        p.auto_sync = not bool(p.auto_sync)
        now_on = bool(p.auto_sync)
        await s.commit()
    label = PANEL_LABELS.get(_bk(key), key)
    await cb.answer(
        (f"✅ از این به بعد ساب‌لینک کاربران {label} هر {AUTOSYNC_EVERY_HOURS} ساعت خودکار بروز می‌شود. "
         "لیست اینباندها فقط در حالت «همه» با پنل هماهنگ می‌شود؛ انتخاب دستی تو ثابت می‌ماند.")
        if now_on else f"⛔️ بروزرسانیِ خودکارِ {label} خاموش شد.",
        show_alert=True)
    # منوی همان پنل دوباره رندر شود تا لیبلِ دکمه عوض شود.
    from tenant.bots.vpn.vpn_admin import _vpn_panel_menu_content
    info, kb = await _vpn_panel_menu_content(row, key)
    try:
        await cb.message.edit_text(info, reply_markup=kb)
    except Exception:
        await cb.message.answer(info, reply_markup=kb)


# ───────────────────────── خودِ همگام‌سازی ─────────────────────────
async def sync_panel_once(panel_id: int) -> tuple[int, int]:
    """یک پنل را همگام می‌کند و (تعدادِ اکانتِ بروزشده، تعدادِ ناموفق)
    برمی‌گرداند. هیچ پیامی به کسی نمی‌دهد."""
    async with Session() as s:
        panel = await s.get(VpnPanel, panel_id)
        if not panel or not panel.enabled:
            return 0, 0
        bot_id, key = panel.bot_id, panel.panel_key
        # نمونهٔ جدا برای استفادهٔ بیرون از session
        p_snapshot = panel

    client = make_panel_client(p_snapshot)
    updated = failed = 0
    try:
        await client.login()

        # ── ۱) اینباندها/گروه‌های فعالِ پنل ──
        # === PATCH_PANEL_INBOUND_PICKER_V1 === (تیکت MidnightSoul)
        # قبلا این بخش همیشه inbound_ids را با «همهٔ اینباندهای فعال» بازنویسی می‌کرد و
        # انتخاب دستی مالک (مثلا فقط ترکیه) هر ۲ ساعت از بین می‌رفت. حالا فقط وقتی
        # لیست را بروز می‌کند که مالک «همه» را خواسته باشد؛ انتخاب دستی ثابت می‌ماند.
        try:
            # === PATCH_AUTOSYNC_EYLAN_V1 === در ایلان «اینباند» = پروتکل (openvpn، wireguard، …) و
            # انتخابِ پروتکل‌ها با خودِ ادمین است؛ همگام‌سازیِ خودکار نباید آن را با «همهٔ پروتکل‌ها»
            # بازنویسی کند (قبلاً int('openvpn') خطا می‌داد و هر ساعت لاگ می‌شد).
            inbounds = [] if _bk(key) == "eylan" else (await client.list_inbounds() or [])
            active_ids = PI.active_ids(_bk(key), inbounds)
            if active_ids:
                async with Session() as s:
                    p2 = await s.get(VpnPanel, panel_id)
                    if p2:
                        plan = PI.plan_inbound_sync(p2.inbound_mode, p2.inbound_ids, active_ids)
                        if plan.changed:
                            old_ids = p2.inbound_ids
                            p2.inbound_ids = plan.ids
                            p2.inbound_mode = plan.mode
                            await s.commit()
                            if old_ids != plan.ids:
                                logger.info("panel_autosync: اینباندهای پنل %s (bot %s) بروز شد: %s",
                                            key, bot_id, plan.ids)
                        if plan.missing:
                            logger.warning("panel_autosync: پنل %s (bot %s) — این انتخاب‌ها دیگر در پنل نیستند/غیرفعال‌اند: %s",
                                           key, bot_id, ",".join(plan.missing))
        except Exception:
            logger.exception("panel_autosync: خواندنِ اینباندهای پنل %s (bot %s) ناموفق", key, bot_id)

        # ── ۲) ساب‌لینکِ اکانت‌های فعالِ همین پنل ──
        async with Session() as s:
            accts = (await s.scalars(select(PanelAccount).where(
                PanelAccount.bot_id == bot_id,
                PanelAccount.panel_key == key,
                PanelAccount.status == "active"))).all()
            work = [(a.id, a.email, a.sub_id, a.sub_link) for a in accts]

        marzban_like = _bk(key) in _MARZBAN_FAMILY
        # === PATCH_AUTOSYNC_CIRCUIT_BREAKER_V1 ===
        # وقتی پنل خاموش/کند است (DNS خراب، تایم‌اوت)، قبلاً برایِ *تک‌تکِ* اکانت‌ها
        # ۸ ثانیه صبر و یک traceback کامل لاگ می‌شد (۱۵۰+ خطا در روز برایِ یک پنل).
        # بعد از ۳ شکستِ پشتِ‌هم، همین دور برایِ این پنل رها می‌شود.
        _consec_fail = 0
        for aid, email, sub_id, old_link in work:
            if _consec_fail >= 3:
                logger.warning("panel_autosync: پنل %s (bot %s) پشتِ‌هم ۳ بار جواب نداد؛ "
                               "بقیهٔ اکانت‌ها در این دور رد شدند", key, bot_id)
                break
            try:
                if marzban_like:
                    cur = await client.get_client(email)
                    new_link = (cur or {}).get("sub_link") or ""
                    new_sub_id = (cur or {}).get("subId") or sub_id
                else:
                    new_sub_id = sub_id
                    if not new_sub_id:
                        cur = await client.get_client(email)
                        new_sub_id = (cur or {}).get("subId")
                    new_link = client.sub_link(new_sub_id) if new_sub_id else ""
                _consec_fail = 0
                if not new_link:
                    failed += 1
                    continue
                if new_link != (old_link or "") or (new_sub_id and new_sub_id != sub_id):
                    async with Session() as s:
                        a2 = await s.get(PanelAccount, aid)
                        if a2:
                            a2.sub_link = new_link
                            if new_sub_id:
                                a2.sub_id = new_sub_id
                            await s.commit()
                    updated += 1
            except Exception as _e:
                failed += 1
                _consec_fail += 1
                logger.warning("panel_autosync: بروزرسانیِ ساب‌لینکِ %s ناموفق (%s: %s)",
                               email, type(_e).__name__, str(_e)[:160])
            await asyncio.sleep(_BETWEEN_ACCOUNTS_SLEEP)
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("panel_autosync: بستنِ کلاینتِ پنل ناموفق")

    async with Session() as s:
        p3 = await s.get(VpnPanel, panel_id)
        if p3:
            p3.auto_sync_at = datetime.utcnow()
            await s.commit()
    return updated, failed


_FAIL_COUNT: dict[int, int] = {}
_FAIL_UNTIL: dict[int, float] = {}


async def panel_autosync_once() -> None:
    """پنل‌هایی که نوبتشان رسیده را همگام می‌کند."""
    due_before = datetime.utcnow() - timedelta(hours=AUTOSYNC_EVERY_HOURS)
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(
            VpnPanel.auto_sync == True,  # noqa: E712
            VpnPanel.enabled == True))).all()  # noqa: E712
        due = [p.id for p in panels
               if p.auto_sync_at is None or p.auto_sync_at <= due_before]
    # === PATCH_AUTOSYNC_FAIL_BACKOFF_V1 ===
    # لاگ: پنل‌هایِ خراب (رمزِ اشتباه، دامنهٔ ناموجود) چون auto_sync_at فقط
    # در موفقیت به‌روز می‌شود، هر ۱۵ دقیقه با traceback کامل دوباره امتحان
    # می‌شدند (~۲۴۰ خطایِ تکراری برایِ هر پنل در ۳ روز). حالا بعد از هر شکست
    # فاصله دو برابر می‌شود (۱ ساعت تا حداکثر ۲۴ ساعت) و به‌جایِ traceback
    # یک warning کوتاه می‌آید. موفقیت، شمارنده را صفر می‌کند.
    import time as _t
    now_m = _t.monotonic()
    due = [pid for pid in due if _FAIL_UNTIL.get(pid, 0) <= now_m]
    if not due:
        return
    logger.info("panel_autosync: %d پنل برای همگام‌سازی در نوبت است", len(due))
    for pid in due:
        try:
            updated, failed = await sync_panel_once(pid)
            _FAIL_COUNT.pop(pid, None)
            _FAIL_UNTIL.pop(pid, None)
            logger.info("panel_autosync: پنل %s — %d بروز، %d ناموفق", pid, updated, failed)
        except Exception as e:
            n = _FAIL_COUNT.get(pid, 0) + 1
            _FAIL_COUNT[pid] = n
            wait = min(3600 * (2 ** (n - 1)), 86400)
            _FAIL_UNTIL[pid] = _t.monotonic() + wait
            # PATCH_LOG_THROTTLE_PERSIST_V1 — قبلاً «بارِ اول» با traceback ثبت می‌شد و چون شمارنده در
            # حافظه بود، هر ری‌استارت دوباره «بارِ اول» بود (۷۶ ERROR در یک روز برایِ ۳ پنلِ قطع).
            # قطعیِ پنل (PanelError) خطایِ کدِ ما نیست → یک WARNINGِ یک‌خطی، حداکثر هر ۶ ساعت.
            from log_throttle import should_log
            from tenant.shared.panel_clients.vpn_panel import PanelError as _PE
            if not should_log(f"pasync:{pid}", 6 * 3600):
                logger.debug("panel_autosync: پنل %s ناموفق (تکراری، ثبت نشد)", pid)
            elif isinstance(e, _PE):
                logger.warning("panel_autosync: همگام‌سازیِ پنل %s ناموفق: %s",
                               pid, (str(e) or type(e).__name__).splitlines()[0][:200])
            elif n == 1:
                logger.exception("panel_autosync: همگام‌سازیِ پنل %s ناموفق", pid)
            else:
                logger.warning("panel_autosync: پنل %s برایِ %dمین بار ناموفق (%s) — تلاشِ بعدی %d ساعت دیگر",
                               pid, n, str(e)[:120], wait // 3600)
        await asyncio.sleep(_BETWEEN_PANELS_SLEEP)


async def panel_autosync_loop(interval_sec: int = _LOOP_INTERVAL_SEC) -> None:
    await asyncio.sleep(90)
    while True:
        try:
            await panel_autosync_once()
        except Exception:
            logger.exception("panel_autosync_loop error")
        await asyncio.sleep(interval_sec)
