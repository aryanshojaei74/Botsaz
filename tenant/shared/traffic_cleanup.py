"""
traffic_cleanup.py — پایانِ چرخهٔ سرویس‌هایی که حجمشان تمام شده.

=== PATCH_TRAFFIC_OUT_CLEANUP_V1 ===
گزارشِ ادمین: «این پیام هر روز داره برای کاربر ارسال می‌شه، روزِ سومه».

چرا اتفاق می‌افتاد: در `vpn_expiry_loop` حذفِ خودکار فقط به `exp` (انقضایِ
زمانی) نگاه می‌کرد:

    if exp > 0 and ((now_ms > exp and warn_cnt >= warn_limit) or ...)

سرویسی که *حجمش* تمام شده ولی تاریخش هنوز نگذشته، هیچ‌وقت وارد این شرط
نمی‌شد. شمارندهٔ `expiry_warn_count` هم فقط برایِ اخطارِ «منقضی شده» بالا
می‌رفت، نه اخطارِ حجم. نتیجه: سرویس در برزخ می‌ماند و همان هشدار هر روز
تا ابد تکرار می‌شد.

چرخهٔ جدید، دقیقاً مثلِ `trial_cleanup.py`:
  ۱) حجم تمام می‌شود → یک اخطار می‌رود و `traffic_out_at` مهر می‌خورد.
  ۲) TRAFFIC_DELETE_HOURS ساعت بعد (پیش‌فرض ۵) → حساب از پنل و از
     «کانفیگ‌های من» حذف می‌شود و به کاربر اطلاع داده می‌شود.
  ۳) اگر کاربر در این فاصله ترافیک بخرد، `traffic_out_at` پاک می‌شود و
     چرخه لغو می‌گردد.

چرا حلقهٔ جدا و نه همان `vpn_expiry_loop`؟ آن حلقه هر ۱۲ ساعت اجرا
می‌شود، پس مهلتِ ۵ ساعته را تا ۱۲ ساعت عقب می‌انداخت. این حلقه هر ۱۰
دقیقه اجرا می‌شود و چون کارش صرفاً زمانی است، به پنل دست نمی‌زند مگر
برایِ خودِ حذف — یعنی بارِ اضافه‌ای روی پنل نمی‌گذارد.

مهلت برایِ هر ربات جدا قابلِ تنظیم است، با کلیدِ Setting:
    traffic_delete_hours   (پیش‌فرض "5")

=== PATCH_TRAFFIC_RENEW_GUARD_V1 === (تیکتِ تعزیان: «تمدید کردم چرا حذف شد»)
باگ: پاک‌شدنِ مهرِ `traffic_out_at` بعد از تمدید/خریدِ ترافیک فقط در `vpn_expiry_loop` انجام می‌شد که هر ۱۲ ساعت
اجرا می‌شود، ولی حذف را همین حلقه هر ۱۰ دقیقه و ۵ ساعت بعد از مهر انجام می‌دهد ⇒ کاربری که ظرفِ ۵ ساعت تمدید می‌کرد،
قبل از اینکه مهر پاک شود، سرویسِ تمدیدشده‌اش حذف می‌شد. دو لایهٔ دفاعی:
  ۱) هر مسیرِ تمدید/خریدِ ترافیک مهر را همان لحظه پاک می‌کند (clear_traffic_out).
  ۲) این حلقه قبل از حذف، وضعیتِ *زندهٔ* حساب را از پنل می‌خواند؛ اگر حجمِ باقی‌مانده دارد (یا نامحدود شده)، مهر
     پاک و حذف لغو می‌شود. اگر خواندنِ پنل ممکن نبود، حذف به دورِ بعد موکول می‌شود (هرگز کورکورانه حذف نمی‌کنیم).

این ماژول Router ندارد؛ فقط یک حلقهٔ پس‌زمینه است.
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

DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)
DEFAULT_DELETE_HOURS = 5.0


async def _setting_get(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == key))
    return r.value if (r and r.value is not None) else default


async def _delete_hours(bot_id: int, panel_key: str = "") -> float:
    """PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 — حالا per-panel است: اول کلیدِ
    مخصوصِ همین پنل (traffic_delete_hours_{panel_key}) را می‌خواند؛ اگر
    ثبت نشده بود، به کلیدِ عمومیِ قدیمی (traffic_delete_hours، بدونِ
    پسوند) برمی‌گردد؛ اگر آن هم نبود، پیش‌فرضِ کدی."""
    key = f"traffic_delete_hours_{panel_key}" if panel_key else "traffic_delete_hours"
    raw = await _setting_get(bot_id, key, "")
    if not raw and panel_key:
        raw = await _setting_get(bot_id, "traffic_delete_hours", "")
    try:
        v = float(raw) if raw else DEFAULT_DELETE_HOURS
    except (TypeError, ValueError):
        return DEFAULT_DELETE_HOURS
    # سقف و کف تا یک تنظیمِ اشتباه باعثِ حذفِ آنی یا هرگز-حذف-نشدن نشود
    return max(0.5, min(v, 720.0))


def _svc_info(title, code, vol_gb, days, email) -> str:
    lines = []
    if title:
        lines.append(f"💎 نام سرویس: <b>{title}</b>")
    if code:
        lines.append(f"🗂 کد سرویس: <code>{code}</code>")
    if email:
        lines.append(f"👤 نام کاربری پنل: <code>{email}</code>")
    if vol_gb:
        lines.append(f"📦 حجم خریداری‌شده: <b>{vol_gb} گیگ</b>")
    if days:
        lines.append(f"🗓 مدت سرویس: <b>{days} روز</b>")
    return "\n".join(lines)


def _make_client(pf: tuple):
    """pf = (panel_key, base_url, username, password, sub_base, inbound_ids, api_key)
    عیناً همان سازندهٔ trial_cleanup — تا هر دو چرخه از یک مسیر به پنل بروند."""
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
    key, base, user, pw, sub_base, inbound, api_key = pf
    return make_panel_client_from_fields(
        key, base, user, pw, sub_base=sub_base, inbound_ids_str=inbound, api_key=api_key)


async def _panels_of(bot_id: int) -> dict[str, tuple]:
    """{panel_key: pf} برایِ پنل‌هایِ فعالِ این ربات."""
    async with Session() as s:
        rows = (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))).all()  # noqa: E712
        return {r.panel_key: (r.panel_key, r.base_url, r.username, r.password,
                              r.sub_base, r.inbound_ids, r.api_key) for r in rows}


async def _delete_from_panel(pf: tuple | None, email: str) -> None:
    """حذفِ کلاینت از پنل. شکستش نباید جلویِ حذفِ ردیفِ دیتابیس را بگیرد —
    وگرنه یک پنلِ موقتاً در دسترس‌نبودن باعث می‌شود کاربر تا ابد پیام بگیرد."""
    if pf is None:
        logger.warning("traffic_cleanup: پنلی برایِ %r پیدا نشد؛ فقط ردیفِ "
                       "دیتابیس حذف می‌شود.", email)
        return
    client = _make_client(pf)
    try:
        await client.delete_account(email)
    except Exception:
        logger.exception("traffic_cleanup: حذفِ %r از پنلِ %s ناموفق بود.",
                         email, pf[0])
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("بستنِ کلاینتِ پنل ناموفق بود")


async def clear_traffic_out(account_id: int) -> None:
    """مهرِ «حجم تمام شد» را برمی‌دارد (بعد از تمدید/خریدِ ترافیک). هرگز استثنا پرتاب نمی‌کند."""
    try:
        async with Session() as s:
            a = await s.get(PanelAccount, account_id)
            if a and a.traffic_out_at is not None:
                a.traffic_out_at = None
                await s.commit()
    except Exception:
        logger.exception("traffic_cleanup: پاک‌کردنِ مهرِ حساب %s ناموفق بود", account_id)


async def _live_usage(pf: tuple | None) -> dict[str, tuple[int, int]] | None:
    """{email: (cap_bytes, used_bytes)} از رویِ پنل؛ None = خواندن ممکن نشد (حذف نباید انجام شود)؛
    {} = پنلی در دسترس نیست (حذف/غیرفعال) ⇒ حساب‌ها مثلِ قبل فقط از دیتابیس پاک می‌شوند.
    همان قالبِ خواندنِ `vpn_expiry_loop` (کلیدهایِ totalGB/total و up/down) تا برایِ همهٔ انواعِ پنل یکسان باشد."""
    if pf is None:
        return {}                         # پنل حذف/غیرفعال شده ⇒ مثلِ قبل فقط ردیفِ دیتابیس حذف می‌شود
    client = _make_client(pf)
    try:
        clients = await client.list_clients()
    except Exception as e:
        logger.warning("traffic_cleanup: خواندنِ وضعیتِ زندهٔ پنلِ %s ناموفق؛ حذف به دورِ بعد موکول شد: %s",
                       pf[0], (str(e) or type(e).__name__)[:200])
        return None
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("بستنِ کلاینتِ پنل ناموفق بود")
    out: dict[str, tuple[int, int]] = {}
    for c in clients or []:
        email = c.get("email")
        if not email:
            continue
        tr = c.get("traffic") if isinstance(c.get("traffic"), dict) else c
        try:
            up = int(tr.get("up", 0) or 0)
            down = int(tr.get("down", 0) or 0)
            cap = int((c.get("totalGB", tr.get("total", 0))) or 0)
        except (TypeError, ValueError):
            continue
        out[email] = (cap, up + down)
    return out


async def _process_bot(bot_id: int) -> None:
    now = _dt.datetime.utcnow()

    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
        if not brow or brow.status != "active" or not brow.token:
            return
        # === PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 ===
        # مهلت حالا per-panel است، پس دیگه نمی‌شه یه cutoffِ واحد تو خودِ
        # کوئری زد — همه‌ی حساب‌هایی که حجمشون تموم شده رو می‌خونیم و
        # برایِ هرکدوم جدا (بر اساسِ panel_key خودش) چک می‌کنیم که مهلتش
        # گذشته یا نه.
        # === PATCH_METERED_PLAN_V1 === سرویس‌هایِ «بستهٔ اعتباری» با تمام‌شدنِ موجودی حذف نمی‌شوند
        from models import MeteredAccount as _MA
        _metered = select(_MA.account_id).where(_MA.bot_id == bot_id, _MA.status.in_(("active", "no_credit")))
        pending = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id,
            PanelAccount.traffic_out_at.is_not(None),
            PanelAccount.id.notin_(_metered),
        ))).all()
        # مقادیر همین‌جا خوانده می‌شوند؛ بعد از خروج از بلاک منقضی می‌شوند
        pending_rows = [(a.id, a.email, a.user_telegram_id, a.plan_title, a.svc_code,
                         a.volume_gb, a.days, a.panel_key, a.traffic_out_at) for a in pending]
        token = brow.token

    if not pending_rows:
        return

    rows = []
    hours_by_panel: dict[str, float] = {}
    for aid, email, tg_id, title, code, vol_gb, days, panel_key, out_at in pending_rows:
        pk = panel_key or "sanai"
        if pk not in hours_by_panel:
            hours_by_panel[pk] = await _delete_hours(bot_id, pk)
        hours = hours_by_panel[pk]
        if out_at <= now - _dt.timedelta(hours=hours):
            rows.append((aid, email, tg_id, title, code, vol_gb, days, panel_key, hours))

    if not rows:
        return

    panels = await _panels_of(bot_id)

    # === PATCH_TRAFFIC_RENEW_GUARD_V1 === اعتبارسنجیِ زنده: فقط حساب‌هایی حذف می‌شوند که روی پنل هم هنوز
    # «حجمِ تمام‌شده» دارند. تمدید/افزایشِ حجم (از هر مسیری، حتی مستقیم روی پنل) حذف را لغو می‌کند.
    live_by_panel: dict[str, dict[str, tuple[int, int]] | None] = {}
    verified = []
    for row_t in rows:
        aid, email, tg_id, title, code, vol_gb, days, panel_key, hours = row_t
        pk = panel_key or "sanai"
        if pk not in live_by_panel:
            live_by_panel[pk] = await _live_usage(panels.get(pk))
        live = live_by_panel[pk]
        if live is None:
            continue                      # پنل خوانده نشد ⇒ امروز حذف نمی‌کنیم
        cur = live.get(email)
        if cur is not None:
            cap, used = cur
            if cap <= 0 or cap - used > 0:   # نامحدود شده یا حجمِ باقی‌مانده دارد ⇒ تمدید شده
                await clear_traffic_out(aid)
                logger.info("traffic_cleanup: حذفِ %r لغو شد؛ حجمِ زنده دارد (cap=%s used=%s)", email, cap, used)
                continue
        verified.append(row_t)
    rows = verified
    if not rows:
        return

    tbot = Bot(token=token, default=DEFAULTS)
    try:
        for aid, email, tg_id, title, code, vol_gb, days, panel_key, hours in rows:
            await _delete_from_panel(panels.get(panel_key or "sanai"), email)
            async with Session() as s:
                a2 = await s.get(PanelAccount, aid)
                if a2:
                    await s.delete(a2)
                    await s.commit()
            info = _svc_info(title, code, vol_gb, days, email)
            try:
                await tbot.send_message(tg_id, (
                    "🗑 <b>سرویس شما حذف شد</b>\n\n"
                    f"{info}\n\n"
                    "<blockquote>⛔️ حجمِ این سرویس تمام شده بود و در مهلتِ "
                    "تعیین‌شده ترافیکِ اضافه یا تمدیدی انجام نشد. سرویس اکنون "
                    "هم از پنل و هم از بخش «کانفیگ‌های من» حذف شده و قابلِ "
                    "بازیابی نیست.\n"
                    "هر زمان بخواهید می‌توانید سرویسِ جدیدی تهیه کنید.</blockquote>"))
            except TelegramForbiddenError:
                pass
            except Exception:
                logger.exception("traffic_cleanup: اطلاعِ حذف به کاربر نرسید")
            logger.info("traffic_cleanup: سرویسِ %r (bot_id=%s, panel=%s) به‌دلیلِ اتمامِ "
                        "حجم پس از %g ساعت حذف شد.", email, bot_id, panel_key, hours)
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.exception("بستنِ سشنِ ربات ناموفق بود")


async def traffic_cleanup_loop(interval: int = 600):
    """هر ۱۰ دقیقه: حذفِ سرویس‌هایی که حجمشان تمام شده و مهلتشان گذشته."""
    await asyncio.sleep(75)          # کمی بعد از trial_cleanup، تا هم‌زمان نشوند
    while True:
        try:
            async with Session() as s:
                bot_ids = list((await s.scalars(select(VpnPanel.bot_id).where(
                    VpnPanel.enabled == True).distinct())).all())  # noqa: E712
            for bot_id in bot_ids:
                try:
                    await _process_bot(bot_id)
                except Exception:
                    logger.exception("traffic_cleanup: خطا در ربات %s", bot_id)
        except Exception:
            logger.exception("traffic_cleanup_loop error")
        await asyncio.sleep(interval)
