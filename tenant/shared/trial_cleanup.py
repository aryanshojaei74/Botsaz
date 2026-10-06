"""
trial_cleanup.py — بخشِ «پاک‌سازیِ کانفیگ‌هایِ تست رایگان».

=== PATCH_TRIAL_EXPIRE_CLEANUP_V1 ===
تا پیش از این، کانفیگِ «تست رایگان» عمداً از vpn_expiry_loop مستثنا بود
(همان `continue`) — یعنی بعد از تمام‌شدنِ تست، حسابِ کاربر برایِ همیشه روی
پنل می‌ماند و در «کانفیگ‌های من» هم دیده می‌شد. نتیجه: پنل پر از حساب‌هایِ
مردهٔ تست، و کاربری که یک کانفیگِ بی‌کار در لیستش دارد.

چرخهٔ این ماژول:
  ۱) تستِ کاربر تمام می‌شود  → یک اخطار می‌رود و trial_warned_at ثبت می‌شود.
  ۲) TRIAL_DELETE_HOURS ساعت بعد (پیش‌فرض ۵) → حساب از پنل حذف می‌شود،
     رکوردِ PanelAccount هم پاک می‌شود (پس از «کانفیگ‌های من» ناپدید می‌شود)
     و به کاربر اطلاع داده می‌شود که حذف شد.

مهلت برایِ هر ربات جدا قابلِ تنظیم است، با کلیدِ Setting:
    trial_delete_hours   (پیش‌فرض "5")

=== PATCH_TRIAL_UNUSED_REMINDER_V1 ===
یک چرخهٔ دومِ کاملاً مستقل، برایِ تستی که هنوز از نظرِ زمانی تمام نشده
ولی هیچ مصرفی هم نداشته (کاربر اصلاً وصل نشده):
  ۱) trial_unused_hours ساعت بعد از ساختِ تست، اگر هنوز صفر مصرف شده
     → یک اخطار می‌رود و trial_unused_warned_at ثبت می‌شود.
  ۲) trial_delete_hours ساعتِ دیگر (همان مهلتِ بالا) اگر هنوز صفر مصرف
     است → حذفِ کامل + اطلاع به کاربر.
trial_unused_hours پیش‌فرضش "0" است (خاموش) تا رفتارِ ربات‌هایِ قدیمی
عوض نشود؛ فقط با تنظیمِ صریحِ ادمین از تنظیمات تست رایگان فعال می‌شود.

این ماژول Router ندارد؛ فقط یک حلقهٔ پس‌زمینه است که worker_tenant.py
اسپانش می‌کند.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, PanelAccount, Setting, VpnPanel

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

TRIAL_TITLE = "تست رایگان"
# عمداً اینجا ساخته می‌شود و از main_tenant وارد نمی‌شود — ماژول‌های tenant
# نباید به سرویسِ پلتفرم/میزبان وابسته شوند.
DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)
DEFAULT_DELETE_HOURS = 5.0
# === PATCH_TRIAL_ORPHAN_ROW_CLEANUP_V1 ===
# ردیفی که روی پنل نیست فقط وقتی «یتیم» حساب می‌شود که دستِ‌کم این‌قدر از
# ساختش گذشته باشد — وگرنه ممکن است تستی باشد که همین الان ساخته شده و
# هنوز در پاسخِ کش‌شدهٔ پنل ظاهر نشده.
ORPHAN_MIN_AGE_HOURS = 24


async def _setting_get(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == key))
    return r.value if (r and r.value is not None) else default


async def _delete_hours(bot_id: int) -> float:
    try:
        v = float(await _setting_get(bot_id, "trial_delete_hours", "5"))
    except (TypeError, ValueError):
        return DEFAULT_DELETE_HOURS
    # بینِ ۱۰ دقیقه تا ۷ روز — جلویِ مقدارِ اشتباهِ دستی را می‌گیرد
    return min(max(v, 0.17), 168.0)


# === PATCH_TRIAL_UNUSED_REMINDER_V1 ===
async def _unused_hours(bot_id: int) -> float:
    """مدتِ انتظار برایِ «هنوز استفاده نکرده» قبل از اخطار — ۰ یعنی این
    قابلیت برایِ این ربات خاموش است (پیش‌فرض، تا برایِ ربات‌هایِ قدیمی
    رفتاری عوض نشود)."""
    try:
        v = float(await _setting_get(bot_id, "trial_unused_hours", "0"))
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    # بینِ ۱۵ دقیقه تا ۱۰ روز
    return min(max(v, 0.25), 240.0)


def _info_block(title: str | None, code: str | None, email: str) -> str:
    return (f"💎 نام سرویس: <b>{title or TRIAL_TITLE}</b>\n"
            f"🗂 کد سرویس: <code>{code or '—'}</code>\n"
            f"👤 نام کاربری پنل: <code>{email}</code>")


def _make_client(pf: tuple):
    """pf = (panel_key, base_url, username, password, sub_base, inbound_ids, api_key)"""
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
    key, base, user, pw, sub_base, inbound, api_key = pf
    return make_panel_client_from_fields(
        key, base, user, pw, sub_base=sub_base, inbound_ids_str=inbound, api_key=api_key)


async def _panel_client_map(pf: tuple, bot_id: int) -> dict[str, dict] | None:
    """{email: client_dict} از رویِ پنل. None یعنی پنل جواب نداد.

    قبلاً فقط expiryTime نگه می‌داشت (`_panel_expiry_map`)؛ حالا کلِ
    دیکشنریِ کلاینت را نگه می‌دارد تا هم انقضا و هم مصرف (برایِ اخطارِ
    «تستِ استفاده‌نشده») از همین یک فراخوانیِ list_clients استخراج شود،
    بدونِ تماسِ اضافه به پنل."""
    client = _make_client(pf)
    try:
        clients = await client.list_clients()
    except Exception:
        logger.warning("trial_cleanup: پنلِ %s (bot_id=%s) پاسخ نداد — این دور رد شد.",
                       pf[0], bot_id)
        return None
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (بستنِ کلاینتِ پنل)")
    out: dict[str, dict] = {}
    for c in clients:
        em = c.get("email")
        if em:
            out[em] = c
    return out


def _expiry_of(c: dict) -> int:
    return int(c.get("expiryTime", 0) or 0)


def _traffic_of(c: dict) -> tuple[int, int]:
    """(up, down) به‌طورِ یکسان از فرمت‌هایِ مختلفِ پنل — همان الگویِ
    vpn_first_connect_loop در main.py (بعضی پنل‌ها up/down را زیرِ
    «traffic» تو در تو می‌دهند، بعضی مستقیم رویِ خودِ آبجکت)."""
    tr = c.get("traffic") if isinstance(c.get("traffic"), dict) else c
    up = int(tr.get("up", 0) or 0)
    down = int(tr.get("down", 0) or 0)
    return up, down


async def _delete_from_panel(pf: tuple, email: str) -> None:
    client = _make_client(pf)
    try:
        await client.delete_account(email)
    except Exception:
        logger.exception("trial_cleanup: حذفِ %r از پنلِ %s ناموفق بود.",
                         email, pf[0])
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (بستنِ کلاینتِ پنل)")


async def _process_bot(bot_id: int) -> None:
    now = _dt.datetime.utcnow()
    now_ms = int(_dt.datetime.now().timestamp() * 1000)

    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
        if not brow or brow.status != "active" or not brow.token:
            return
        token = brow.token
        accts = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id,
            PanelAccount.plan_title == TRIAL_TITLE))).all()
        rows = [(a.id, a.email, a.user_telegram_id, a.plan_title, a.svc_code,
                 a.panel_key, a.expires_at, a.trial_warned_at, a.created_at,
                 a.trial_unused_warned_at)
                for a in accts]
    if not rows:
        return

    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))).all()
        # مقادیر همین‌جا (داخلِ بلاکِ سشن) بیرون کشیده می‌شوند؛ آبجکت‌ها
        # بعد از خروج از سشن منقضی می‌شوند.
        panel_fields = [(p.panel_key, p.base_url, p.username, p.password,
                         p.sub_base or "", p.inbound_ids, p.api_key) for p in panels]
    if not panel_fields:
        return
    by_key = {pf[0]: pf for pf in panel_fields}

    hours = await _delete_hours(bot_id)
    unused_hours = await _unused_hours(bot_id)
    exp_cache: dict[str, dict[str, dict] | None] = {}
    tbot = Bot(token=token, default=DEFAULTS)
    try:
        for (aid, email, tg_id, title, code, pkey, exp_at, warned_at, created_at,
             unused_warned_at) in rows:
            pf = by_key.get(pkey) or panel_fields[0]

            # ---- ۱) آیا تست تمام شده؟ منبعِ اصلی خودِ پنل، وگرنه expires_at
            if pf[0] not in exp_cache:
                exp_cache[pf[0]] = await _panel_client_map(pf, bot_id)
            emap = exp_cache[pf[0]]
            cdata = emap.get(email) if emap is not None else None
            exp_ms = _expiry_of(cdata) if cdata else 0
            if not exp_ms and exp_at:
                exp_ms = int(exp_at.timestamp() * 1000)
            if emap is not None and email not in emap and not exp_at:
                # === PATCH_TRIAL_ORPHAN_ROW_CLEANUP_V1 ===
                # رکوردِ یتیم: نه رویِ پنل هست و نه انقضایِ محلی دارد —
                # یعنی حسابش قبلاً (دستی یا با ریست‌شدنِ پنل) پاک شده و فقط
                # ردیفِ مرده‌اش در «کانفیگ‌های من» جا مانده. قبلاً فقط لاگ
                # می‌شد و هر ۱۰ دقیقه دوباره تکرار می‌شد. حالا خودِ ردیف
                # حذف می‌شود — بی‌صدا و بدونِ پیام به کاربر، چون سرویسی در
                # کار نیست که خبرِ حذفش معنا داشته باشد.
                # شرطِ سنی جلویِ حذفِ اشتباهِ تستِ تازه‌ساخته‌ای را می‌گیرد
                # که هنوز در پاسخِ پنل ظاهر نشده است.
                if created_at and (now - created_at).total_seconds() < ORPHAN_MIN_AGE_HOURS * 3600:
                    continue
                async with Session() as s:
                    a2 = await s.get(PanelAccount, aid)
                    if a2:
                        await s.delete(a2)
                        await s.commit()
                logger.info("trial_cleanup: ردیفِ یتیمِ %r (PanelAccount#%s، پنلِ %s) پاک شد.",
                            email, aid, pf[0])
                continue
            if exp_ms <= 0 or now_ms < exp_ms:
                # === PATCH_TRIAL_UNUSED_REMINDER_V1 ===
                # هنوز از نظرِ زمانی تمام نشده — اینجا فقط «عدمِ استفاده»
                # را بررسی می‌کنیم (مستقلِ کاملاً از چرخهٔ پایانِ تست بالا).
                if unused_hours > 0 and cdata is not None:
                    up, down = _traffic_of(cdata)
                    if (up + down) <= 0:
                        info_u = _info_block(title, code, email)
                        if unused_warned_at is None:
                            if created_at and (now - created_at).total_seconds() >= unused_hours * 3600:
                                _uh = int(hours) if float(hours).is_integer() else round(hours, 1)
                                msg_u = (
                                    "⏰ <b>هنوز از تستِ رایگانت استفاده نکردی</b>\n\n"
                                    f"{info_u}\n\n"
                                    f"<blockquote>⚠️ تا <b>{_uh} ساعت</b> دیگر فرصت داری "
                                    "وصل بشی و ازش استفاده کنی؛ اگه تا اون‌موقع هیچ "
                                    "مصرفی ثبت نشه، این کانفیگ به‌صورت خودکار حذف "
                                    "می‌شود.</blockquote>")
                                try:
                                    await tbot.send_message(tg_id, msg_u)
                                except TelegramForbiddenError:
                                    pass
                                except Exception:
                                    logger.exception(
                                        "خطای نادیده‌گرفته‌شده (ارسالِ اخطارِ تستِ استفاده‌نشده)")
                                async with Session() as s:
                                    a2 = await s.get(PanelAccount, aid)
                                    if a2:
                                        a2.trial_unused_warned_at = now
                                        await s.commit()
                        elif (now - unused_warned_at).total_seconds() >= hours * 3600:
                            await _delete_from_panel(pf, email)
                            async with Session() as s:
                                a2 = await s.get(PanelAccount, aid)
                                if a2:
                                    await s.delete(a2)
                                    await s.commit()
                            try:
                                await tbot.send_message(tg_id, (
                                    "🗑 <b>کانفیگ تست شما حذف شد</b>\n\n"
                                    f"{info_u}\n\n"
                                    "<blockquote>⛔️ تایمت برایِ استفاده از تستِ رایگان "
                                    "گذشت و چون هیچ مصرفی ثبت نشد، این کانفیگ به‌صورت "
                                    "خودکار حذف شد؛ دیگر قابلِ بازیابی نیست.\n"
                                    "هر زمان بخواهید می‌توانید یک سرویسِ اصلی "
                                    "تهیه کنید.</blockquote>"))
                            except TelegramForbiddenError:
                                pass
                            except Exception:
                                logger.exception(
                                    "خطای نادیده‌گرفته‌شده (اطلاعِ حذفِ تستِ استفاده‌نشده)")
                            logger.info(
                                "trial_cleanup: کانفیگِ تستِ استفاده‌نشدهٔ %r (bot_id=%s) حذف شد.",
                                email, bot_id)
                continue  # هنوز تمام نشده یا بی‌انقضاست

            info = _info_block(title, code, email)

            # ---- ۲) اخطارِ اول
            if warned_at is None:
                _h = int(hours) if float(hours).is_integer() else round(hours, 1)
                msg = (
                    "🔥 <b>پایان تست رایگان</b>\n"
                    "⏳ مدتِ کانفیگِ تستِ شما به پایان رسید.\n\n"
                    f"{info}\n\n"
                    f"<blockquote>⚠️ این کانفیگ تا <b>{_h} ساعت</b> دیگر "
                    "به‌صورت کامل از پنل و از بخش «کانفیگ‌های من» حذف می‌شود.\n"
                    "برای ادامهٔ اتصالِ بدون قطعی، همین حالا یکی از "
                    "سرویس‌های اصلی را تهیه کنید.</blockquote>")
                try:
                    await tbot.send_message(tg_id, msg)
                except TelegramForbiddenError:
                    pass
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (ارسالِ اخطارِ پایانِ تست)")
                async with Session() as s:
                    a2 = await s.get(PanelAccount, aid)
                    if a2:
                        a2.trial_warned_at = now
                        await s.commit()
                continue

            # ---- ۳) حذفِ کامل پس از مهلت
            if (now - warned_at).total_seconds() < hours * 3600:
                continue

            await _delete_from_panel(pf, email)
            async with Session() as s:
                a2 = await s.get(PanelAccount, aid)
                if a2:
                    await s.delete(a2)
                    await s.commit()
            try:
                await tbot.send_message(tg_id, (
                    "🗑 <b>کانفیگ تست شما حذف شد</b>\n\n"
                    f"{info}\n\n"
                    "<blockquote>⛔️ مهلتِ پس از پایانِ تست تمام شد و این کانفیگ "
                    "به‌صورت کامل از پنل و از بخش «کانفیگ‌های من» حذف شد؛ "
                    "دیگر قابلِ بازیابی نیست.\n"
                    "هر زمان بخواهید می‌توانید یک سرویسِ اصلی تهیه کنید.</blockquote>"))
            except TelegramForbiddenError:
                pass
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (اطلاعِ حذفِ کانفیگِ تست)")
            logger.info("trial_cleanup: کانفیگِ تستِ %r (bot_id=%s) حذف شد.", email, bot_id)
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (بستنِ سشنِ ربات)")


async def trial_cleanup_loop(interval: int = 600):
    """هر ۱۰ دقیقه: اخطارِ پایانِ تست + حذفِ کاملِ تست‌هایِ مهلت‌تمام‌شده."""
    await asyncio.sleep(55)
    while True:
        try:
            async with Session() as s:
                bot_ids = list((await s.scalars(select(VpnPanel.bot_id).where(
                    VpnPanel.enabled == True).distinct())).all())
            for bot_id in bot_ids:
                try:
                    await _process_bot(bot_id)
                except Exception:
                    logger.exception("trial_cleanup: خطا در ربات %s", bot_id)
        except Exception:
            logger.exception("trial_cleanup_loop error")
        await asyncio.sleep(interval)
