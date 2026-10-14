"""vpn_metered.py — «بستهٔ اعتباری»: پرداخت به‌ازایِ مصرف از کیف پول (PATCH_METERED_PLAN_V1).

ایده: ادمین قیمتِ هر گیگ (مثلاً ۲٬۰۰۰ تومان) و مدت (مثلاً ۳۰ روز) را تعیین می‌کند. کاربر با کیف پولِ شارژشده فعالش می‌کند؛
حجمِ اولیه = floor(موجودی ÷ قیمتِ گیگ) (مثلاً ۱۰٬۰۰۰ ت ⇒ ۵ گیگ). حلقهٔ پس‌زمینه هر دقیقه مصرفِ واقعی را از پنل می‌خواند و
هزینه‌اش را از کیف پول کم می‌کند. چون سقفِ حجمِ پنل همیشه فقط به‌اندازهٔ «پولی که کیف پول دارد» است، کاربر هرگز بیشتر از
موجودی مصرف نمی‌کند. وقتی موجودی تمام شد سرویس قطع (no_credit) می‌شود؛ با شارژِ کیف پول (هر ۱ گیگِ کامل) خودکار حجم اضافه
و سرویس وصل می‌شود. توقف/فعال‌سازی/حذف با دکمه‌هایِ همیشگیِ صفحهٔ سرویس (کاربر و ادمین) انجام می‌شود.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from logging_setup import get_logger as _get_logger
from models import Bot as BotRow, BotUser, MeteredAccount, PanelAccount, WalletTxn

logger = _get_logger(__name__)
mtr_router = Router(name="vpn_metered")

GIB = 1024 ** 3
LOW_HEADROOM_BYTES = 5 * 1024 * 1024        # زیرِ ۵ مگ حجمِ باقی‌مانده و پولی برایِ گیگِ بعد ⇒ قطع
LOOP_SECONDS = 60
MAX_GB = 500


class MtrFSM(StatesGroup):
    price = State()
    days = State()
    title = State()


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, cb, style=None):
    kw = {"text": text, "callback_data": cb}
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def _fa(t: str) -> str:
    return (t or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).replace(",", "").replace("٬", "").strip()


def fmt(n) -> str:
    return f"{int(n):,} تومان"


# ═══════════════════ تنظیمات ═══════════════════
async def get_cfg(bot_id: int) -> dict:
    from tenant import get_setting

    async def _i(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(float(await get_setting(bot_id, key, str(default)) or default))))
        except (TypeError, ValueError):
            return default
    return {
        "enabled": (await get_setting(bot_id, "mtr_enabled", "0")) == "1",
        "price": await _i("mtr_price_gb", 2000, 1, 100_000_000),
        "days": await _i("mtr_days", 30, 1, 3650),
        "title": ((await get_setting(bot_id, "mtr_title", "") or "").strip() or "بستهٔ ویژه")[:60],
        "panel_key": (await get_setting(bot_id, "mtr_panel_key", "") or "").strip(),   # "" = اولین پنلِ فعال
    }


async def feature_ok(row) -> bool:
    from plans import Feature, has_feature
    return bool(row) and (row.field or "") == "vpn_sales" and has_feature(row.plan, Feature.METERED_PLAN)


# ═══════════════════ منطقِ مالی (قابلِ تست) ═══════════════════
def affordable_gb(balance, price: int) -> int:
    """چند گیگِ کامل با این موجودی قابلِ خرید است."""
    if price <= 0:
        return 0
    return int(Decimal(str(balance or 0)) // Decimal(price))


def charge_for(delta_bytes: int, price: int) -> int:
    """هزینهٔ تومانیِ مصرف (گردِ به پایین؛ مصرفِ خیلی کم تا رسیدن به ۱ تومان جمع می‌شود)."""
    if delta_bytes <= 0 or price <= 0:
        return 0
    return int((delta_bytes * price) // GIB)


def bytes_for_charge(charge: int, price: int) -> int:
    """بایتی که این مبلغ پوشش می‌دهد (گردِ به بالا ⇒ هرگز بیشتر از حقِ ما مصرف حساب نمی‌شود)."""
    if charge <= 0 or price <= 0:
        return 0
    return -(-(charge * GIB) // price)


def plan_topup(balance, price: int, used: int, cap: int) -> int:
    """چند گیگِ کاملِ دیگر می‌شود به سقفِ پنل اضافه کرد بدونِ اینکه از پولِ کیف پول بیشتر شود."""
    if price <= 0 or cap <= 0:
        return 0
    headroom_gb = Decimal(max(cap - used, 0)) / Decimal(GIB)
    spare = Decimal(str(balance or 0)) / Decimal(price) - headroom_gb
    return max(0, int(spare // 1))


async def settle(m_id: int, used: int) -> tuple[int, int]:
    """کسرِ هزینهٔ مصرفِ جدید از کیف پول؛ (مبلغِ کسرشده، موجودیِ باقی‌مانده). اتمیک (قفلِ ردیفِ کاربر)."""
    async with Session() as s:
        m = await s.get(MeteredAccount, m_id, with_for_update=True)
        if not m:
            return 0, 0
        u = await s.get(BotUser, m.user_id, with_for_update=True)
        bal = int(Decimal(str(u.balance or 0))) if u else 0
        delta = used - int(m.last_used_bytes or 0)
        if delta < 0:                 # شمارندهٔ پنل ریست شده
            m.last_used_bytes = used
            await s.commit()
            return 0, bal
        due = charge_for(delta, m.price_per_gb)
        if due <= 0 or not u:
            return 0, bal
        pay = min(due, max(bal, 0))
        u.balance = float(Decimal(str(u.balance or 0)) - pay)
        m.billed_toman = int(m.billed_toman or 0) + pay
        m.last_used_bytes = min(used, int(m.last_used_bytes or 0) + bytes_for_charge(due, m.price_per_gb))
        m.updated_at = _dt.datetime.utcnow()
        if pay:
            s.add(WalletTxn(bot_id=m.bot_id, user_id=u.id, amount=-pay, kind="metered",
                            note=f"مصرفِ بستهٔ اعتباری ({delta / GIB:.2f} گیگ)"))
        await s.commit()
        return pay, bal - pay


async def process_one(m_id: int, client, notify) -> str:
    """یک دورِ حسابداری برایِ یک سرویس. client: PanelClient، notify(tg_id, text) coroutine. خروجی: وضعیتِ نتیجه (برایِ تست/لاگ)."""
    async with Session() as s:
        m = await s.get(MeteredAccount, m_id)
        a = await s.get(PanelAccount, m.account_id) if m else None
        if not m or m.status == "ended":
            return "gone"
        if not a:
            m.status = "ended"
            await s.commit()
            return "ended"
        if a.expires_at and a.expires_at < _dt.datetime.utcnow():
            m.status = "ended"
            await s.commit()
            return "expired"
        email, tg_id, price, status = a.email, a.user_telegram_id, int(m.price_per_gb), m.status
        user_disabled = (a.status == "disabled" and status != "no_credit")
    usage = await client.get_usage(email)
    if not usage:
        return "nousage"
    used = int(usage.get("up") or 0) + int(usage.get("down") or 0)
    cap = int(usage.get("total") or 0)
    await settle(m_id, used)
    async with Session() as s:
        m = await s.get(MeteredAccount, m_id)
        u = await s.get(BotUser, m.user_id)
        bal = int(Decimal(str(u.balance or 0))) if u else 0
    extra = plan_topup(bal, price, used, cap)
    headroom = max(cap - used, 0)
    if extra >= 1:
        await client.add_traffic(email, extra)
        revived = status == "no_credit"
        if revived:
            await client.set_enabled(email, True)
        async with Session() as s:
            m = await s.get(MeteredAccount, m_id, with_for_update=True)
            a = await s.get(PanelAccount, m.account_id)
            m.status = "active"
            m.updated_at = _dt.datetime.utcnow()
            if a:
                a.volume_gb = int(a.volume_gb or 0) + extra
                if revived:
                    a.status = "active"
                    a.depleted_at = None
            await s.commit()
        if revived:
            await notify(tg_id, f"✅ موجودی کیف پولت شارژ شد و سرویسِ اعتباری‌ات دوباره فعال شد "
                                f"(+{extra} گیگ، موجودی: {fmt(bal)}).")
        return "topup_revived" if revived else "topup"
    if headroom < LOW_HEADROOM_BYTES and status != "no_credit" and not user_disabled:
        await client.set_enabled(email, False)
        async with Session() as s:
            m = await s.get(MeteredAccount, m_id, with_for_update=True)
            a = await s.get(PanelAccount, m.account_id)
            m.status = "no_credit"
            m.updated_at = _dt.datetime.utcnow()
            if a:
                a.status = "disabled"
            await s.commit()
        need = max(price - bal, 0)
        await notify(tg_id, "⛔️ موجودیِ کیف پولت برایِ بستهٔ اعتباری تمام شد و سرویس موقتاً قطع شد.\n"
                            f"با شارژِ کیف پول (حداقل {fmt(need)} برایِ هر ۱ گیگ) خودکار دوباره وصل می‌شود.")
        return "no_credit"
    return "ok"


# ═══════════════════ حلقهٔ پس‌زمینه ═══════════════════
async def metered_loop(interval: int = LOOP_SECONDS):
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    while True:
        try:
            await asyncio.sleep(interval)
            await run_once(DefaultBotProperties(parse_mode=ParseMode.HTML))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("metered_loop: خطایِ دور")


async def run_once(defaults=None) -> int:
    from tenant.bots.vpn.vpn_user import _client_from_panel, _panel_for_account
    async with Session() as s:
        ids = (await s.execute(select(MeteredAccount.id, MeteredAccount.bot_id).where(
            MeteredAccount.status.in_(("active", "no_credit"))))).all()
    by_bot: dict[int, list[int]] = {}
    for mid, bid in ids:
        by_bot.setdefault(bid, []).append(mid)
    done = 0
    for bid, mids in by_bot.items():
        async with Session() as s:
            brow = await s.get(BotRow, bid)
        if not brow or brow.status != "active" or not brow.token:
            continue
        tbot = Bot(token=brow.token, default=defaults) if defaults is not None else Bot(token=brow.token)

        async def _notify(tg_id, text, _b=tbot):
            try:
                await _b.send_message(tg_id, text)
            except Exception:
                logger.info("metered: پیام به کاربر %s نرسید", tg_id)
        try:
            for mid in mids:
                try:
                    async with Session() as s:
                        m = await s.get(MeteredAccount, mid)
                        a = await s.get(PanelAccount, m.account_id) if m else None
                        panel = await _panel_for_account(s, bid, a) if a else None
                    if a is not None and not panel:
                        continue
                    client = _client_from_panel(panel) if panel else None
                    if client is None:       # سرویس از ربات حذف شده؛ process_one ended می‌کند
                        class _N:
                            async def get_usage(self, e): return None
                        client = _N()
                    await process_one(mid, client, _notify)
                    done += 1
                except Exception:
                    logger.exception("metered: پردازشِ سرویسِ %s ناموفق", mid)
        finally:
            try:
                await tbot.session.close()
            except Exception:
                logger.info("metered: بستنِ نشستِ ربات ممکن نشد")
    return done


# ═══════════════════ طرفِ کاربر ═══════════════════
async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _active_of(bot_id: int, user_id: int):
    async with Session() as s:
        return await s.scalar(select(MeteredAccount).where(
            MeteredAccount.bot_id == bot_id, MeteredAccount.user_id == user_id,
            MeteredAccount.status.in_(("active", "no_credit"))))


async def user_entry_row(bot_id: int, plan: str | None = None):
    """دکمهٔ «بستهٔ اعتباری» برایِ صفحهٔ پلن‌ها (یا None اگر خاموش/غیرمجاز)."""
    cfg = await get_cfg(bot_id)
    if not cfg["enabled"]:
        return None
    return [_btn(f"💳 {cfg['title']} — {cfg['price']:,} تومان/گیگ (پرداخت به‌ازایِ مصرف)", "mtr:open", ButtonStyle.PRIMARY)]


@mtr_router.callback_query(F.data == "mtr:open")
async def mtr_open(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row or not await feature_ok(row):
        await cb.answer("این قابلیت فعال نیست.", show_alert=True)
        return
    cfg = await get_cfg(row.id)
    if not cfg["enabled"]:
        await cb.answer("این بسته فعال نیست.", show_alert=True)
        return
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
    bal = int(Decimal(str(u.balance or 0))) if u else 0
    gb = min(affordable_gb(bal, cfg["price"]), MAX_GB)
    cur = await _active_of(row.id, u.id) if u else None
    txt = (f"💳 <b>{cfg['title']}</b>\n\n"
           f"💰 قیمت: <b>{cfg['price']:,} تومان</b> به‌ازایِ هر گیگ — فقط به‌اندازهٔ مصرفِ واقعی\n"
           f"⏳ مدت: <b>{cfg['days']} روز</b>\n"
           f"👛 موجودیِ کیف پولت: <b>{fmt(bal)}</b> (≈ {gb} گیگ)\n\n"
           "مصرفت کم‌کم از کیف پول کم می‌شود. وقتی موجودی تمام شود سرویس قطع می‌شود و با شارژِ کیف پول "
           "خودکار دوباره وصل می‌شود. هر وقت خواستی از «سرویس‌هایِ من» می‌توانی غیرفعال یا حذفش کنی.")
    rows = []
    if cur:
        txt += "\n\n✅ همین الان یک بستهٔ اعتباریِ فعال داری."
        rows.append([_btn("📂 بسته‌هایِ من", "mtr:mine")])
    elif gb >= 1:
        rows.append([_btn(f"✅ فعال‌سازیِ بسته ({gb} گیگ با موجودیِ فعلی)", "mtr:go", ButtonStyle.SUCCESS)])
    else:
        txt += f"\n\n⚠️ برایِ شروع حداقل <b>{fmt(cfg['price'])}</b> موجودی لازم است."
    rows.append([_btn("🔋 شارژ کیف پول", "w:back", ButtonStyle.SUCCESS)])
    rows.append([_btn("⬅️ بازگشت", "vsvc:home", ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text(txt, reply_markup=_kb(rows), parse_mode="HTML")
    except Exception:
        await bot.send_message(cb.from_user.id, txt, reply_markup=_kb(rows), parse_mode="HTML")
    await cb.answer()


@mtr_router.callback_query(F.data == "mtr:mine")
async def mtr_mine(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        ms = (await s.scalars(select(MeteredAccount).where(
            MeteredAccount.bot_id == row.id, MeteredAccount.user_id == (u.id if u else 0)
        ).order_by(MeteredAccount.id.desc()).limit(10))).all()
    rows, lines = [], ["📂 <b>بسته‌هایِ اعتباریِ من</b>\n"]
    lbl = {"active": "✅ فعال", "no_credit": "⛔️ قطع (موجودی تمام)", "ended": "⚪️ پایان‌یافته"}
    for m in ms:
        lines.append(f"• #{m.account_id} — {lbl.get(m.status, m.status)} — هزینهٔ تاکنون: {fmt(m.billed_toman)}")
        if m.status != "ended":
            rows.append([_btn(f"⚙️ مدیریتِ سرویسِ #{m.account_id}", f"vsvc:{m.account_id}")])
    if not ms:
        lines.append("هنوز بسته‌ای نداری.")
    rows.append([_btn("⬅️ بازگشت", "mtr:open", ButtonStyle.DANGER)])
    await cb.message.edit_text("\n".join(lines), reply_markup=_kb(rows), parse_mode="HTML")
    await cb.answer()


_creating: set[tuple[int, int]] = set()


@mtr_router.callback_query(F.data == "mtr:go")
async def mtr_go(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row or not await feature_ok(row):
        await cb.answer("این قابلیت فعال نیست.", show_alert=True)
        return
    cfg = await get_cfg(row.id)
    if not cfg["enabled"]:
        await cb.answer("این بسته فعال نیست.", show_alert=True)
        return
    key = (row.id, cb.from_user.id)
    if key in _creating:
        await cb.answer("در حال ساخت…", show_alert=True)
        return
    _creating.add(key)
    try:
        await _create(cb, bot, row, cfg)
    finally:
        _creating.discard(key)


async def _create(cb: CallbackQuery, bot: Bot, row, cfg: dict):
    from tenant.bots.vpn import vpn_user as VU
    from tenant.bots.vpn.vpn_admin import get_any_enabled_panel, get_panel
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u:
            await cb.answer("ابتدا ربات را /start کن.", show_alert=True)
            return
        uid, bal = u.id, int(Decimal(str(u.balance or 0)))
    if await _active_of(row.id, uid):
        await cb.answer("همین الان یک بستهٔ اعتباریِ فعال داری (از «بسته‌هایِ من» مدیریتش کن).", show_alert=True)
        return
    gb = min(affordable_gb(bal, cfg["price"]), MAX_GB)
    if gb < 1:
        await cb.answer(f"حداقل {fmt(cfg['price'])} موجودی لازم است؛ اول کیف پولت را شارژ کن.", show_alert=True)
        return
    panel = None
    if cfg.get("panel_key"):          # پنلِ انتخابیِ ادمین؛ اگر حذف/غیرفعال شده بود به اولین پنلِ فعال برمی‌گردیم
        panel = await get_panel(row.id, cfg["panel_key"])
        if panel is not None and not panel.enabled:
            panel = None
    if panel is None:
        panel = await get_any_enabled_panel(row.id)
    if not panel:
        await cb.answer("فعلاً سروری برایِ ساخت در دسترس نیست.", show_alert=True)
        return
    await cb.answer("در حال ساخت سرویس…")
    email = await VU._make_config_email(row.id, panel.panel_key, row.username, cb.from_user.id, cb.from_user.username)
    client = VU._client_from_panel(panel)
    try:
        acc = await client.create_account(email, gb=gb, days=cfg["days"], limit_ip=0,
                                          uuid_str=VU.gen_uuid(), sub_id=VU.gen_sub_id(), on_hold=False)
    except Exception as e:
        logger.warning("mtr: ساختِ سرویس ناموفق (bot=%s): %s", row.id, e)
        from tenant import report_user_error, USER_FAIL_TEXT
        await report_user_error(bot, row.id, "ساختِ بستهٔ اعتباری", e, cb.from_user.id)
        await bot.send_message(cb.from_user.id, "❌ ساختِ سرویس انجام نشد (پولی کم نشد).\n" + USER_FAIL_TEXT)
        return
    async with Session() as s:
        pa = PanelAccount(
            bot_id=row.id, user_telegram_id=cb.from_user.id, email=email, panel_key=panel.panel_key,
            uuid=acc.get("uuid"), sub_id=acc.get("sub_id"), sub_link=acc.get("sub_link") or "",
            volume_gb=gb, days=cfg["days"], plan_title=cfg["title"], status="active",
            svc_code=await VU.new_svc_code(s, row.id),
            expires_at=VU._expires_at_from_ms(acc.get("expiry_ms")), first_conn_notified=False)
        s.add(pa)
        await s.flush()
        s.add(MeteredAccount(bot_id=row.id, account_id=pa.id, user_id=uid, price_per_gb=cfg["price"],
                             last_used_bytes=0, billed_toman=0, status="active"))
        await s.commit()
        aid = pa.id
    try:
        await VU._send_config_delivery(bot, cb.from_user.id, aid)
    except Exception:
        logger.exception("mtr: تحویلِ کانفیگ ناموفق (aid=%s)", aid)
        await bot.send_message(cb.from_user.id, "✅ سرویست ساخته شد؛ از «سرویس‌هایِ من» در دسترس است.")
    await bot.send_message(cb.from_user.id,
                           f"💳 <b>بستهٔ اعتباری فعال شد</b>\n\n📦 حجمِ اولیه: <b>{gb} گیگ</b> (با موجودیِ فعلی)\n"
                           f"💰 هر گیگ {cfg['price']:,} تومان — فقط مصرفِ واقعی کم می‌شود.\n"
                           "با شارژِ کیف پول، حجم خودکار اضافه می‌شود.", parse_mode="HTML")


# ═══════════════════ پنلِ ادمین ═══════════════════
async def _admin_row(cb, bot):
    from tenant import is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط مالک/ادمین", show_alert=True)
        return None
    if not await feature_ok(row):
        await cb.answer("🔒 این قابلیت فقط در پلنِ حرفه‌ایِ رباتِ فروشِ VPN فعال است.", show_alert=True)
        return None
    return row


async def _panel_name(bot_id: int, key: str) -> str:
    if not key:
        return "خودکار (اولین پنلِ فعال)"
    from tenant.bots.vpn.vpn_admin import panel_label
    return await panel_label(bot_id, key)


async def _admin_view(bot_id: int):
    cfg = await get_cfg(bot_id)
    async with Session() as s:
        act = await s.scalar(select(func.count()).select_from(MeteredAccount).where(
            MeteredAccount.bot_id == bot_id, MeteredAccount.status == "active")) or 0
        nocr = await s.scalar(select(func.count()).select_from(MeteredAccount).where(
            MeteredAccount.bot_id == bot_id, MeteredAccount.status == "no_credit")) or 0
        billed = await s.scalar(select(func.coalesce(func.sum(MeteredAccount.billed_toman), 0)).where(
            MeteredAccount.bot_id == bot_id)) or 0
    txt = ("💳 <b>بستهٔ اعتباری (پرداخت به‌ازایِ مصرف)</b>\n\n"
           "کاربر با کیف پولِ شارژشده یک سرویس می‌گیرد؛ مصرفِ واقعی کم‌کم از کیف پول کم می‌شود. موجودی که تمام شد سرویس قطع "
           "و با شارژِ کیف پول خودکار وصل می‌شود. توقف/حذف هم برایِ کاربر هم برایِ شما از صفحهٔ سرویس ممکن است.\n\n"
           f"وضعیت: <b>{'✅ فعال' if cfg['enabled'] else '❌ خاموش'}</b>\n"
           f"🏷 عنوان: <b>{cfg['title']}</b>\n"
           f"💰 قیمتِ هر گیگ: <b>{cfg['price']:,} تومان</b>\n"
           f"⏳ مدت: <b>{cfg['days']} روز</b>\n"
           f"🔌 پنلِ ساختِ سرویس: <b>{await _panel_name(bot_id, cfg['panel_key'])}</b>\n\n"
           f"📊 فعال: <b>{act}</b> | ⛔️ قطعِ موجودی: <b>{nocr}</b> | 💵 مجموعِ کسرشده: <b>{fmt(billed)}</b>\n\n"
           "ℹ️ حجمِ هر سرویس به‌صورتِ «گیگِ کامل» اضافه می‌شود (مثلاً موجودی ۱۰٬۰۰۰ با قیمتِ ۲٬۰۰۰ ⇒ ۵ گیگ).")
    rows = [[_btn("⛔️ خاموش‌کردن" if cfg["enabled"] else "✅ روشن‌کردن", "mtr:adm:toggle",
                  ButtonStyle.DANGER if cfg["enabled"] else ButtonStyle.SUCCESS)],
            [_btn("💰 قیمتِ هر گیگ", "mtr:adm:price"), _btn("⏳ مدت (روز)", "mtr:adm:days")],
            [_btn("🏷 عنوان", "mtr:adm:title"), _btn("🔌 پنلِ ساخت", "mtr:adm:panel")],
            [_btn("📋 فهرستِ سرویس‌ها", "mtr:adm:list", ButtonStyle.PRIMARY)],
            [_btn("⬅️ بازگشت", "tadm:sec:settings", ButtonStyle.DANGER)]]
    return txt, _kb(rows)


async def admin_menu(cb, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    txt, kb = await _admin_view(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(cb.from_user.id, txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@mtr_router.callback_query(F.data == "mtr:adm")
async def _adm_entry(cb: CallbackQuery, bot: Bot):
    await admin_menu(cb, bot)


@mtr_router.callback_query(F.data == "mtr:adm:toggle")
async def adm_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _admin_row(cb, bot)
    if not row:
        return
    cfg = await get_cfg(row.id)
    await set_setting(row.id, "mtr_enabled", "0" if cfg["enabled"] else "1")
    await admin_menu(cb, bot)


@mtr_router.callback_query(F.data == "mtr:adm:panel")
async def adm_panel_pick(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    from models import VpnPanel
    from tenant.bots.vpn.vpn_admin import panel_label
    cfg = await get_cfg(row.id)
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == row.id, VpnPanel.enabled == True)  # noqa: E712
                                  .order_by(VpnPanel.id))).all()
    rows = [[_btn(("✅ " if not cfg["panel_key"] else "") + "🤖 خودکار (اولین پنلِ فعال)", "mtr:adm:setpanel:auto")]]
    for p in panels:
        rows.append([_btn(("✅ " if cfg["panel_key"] == p.panel_key else "") + await panel_label(row.id, p.panel_key),
                          f"mtr:adm:setpanel:{p.id}")])
    rows.append([_btn("⬅️ بازگشت", "mtr:adm", ButtonStyle.DANGER)])
    txt = ("🔌 <b>سرویسِ بستهٔ اعتباری روی کدام پنل ساخته شود؟</b>\n\n"
           "پنلی را انتخاب کن که کاربرانِ این بسته رویش ساخته می‌شوند. اگر «خودکار» باشد، اولین پنلِ فعال استفاده می‌شود."
           + ("\n\n⚠️ هنوز پنلِ فعالی در ربات ثبت نشده است." if not panels else ""))
    try:
        await cb.message.edit_text(txt, reply_markup=_kb(rows), parse_mode="HTML")
    except Exception:
        await bot.send_message(cb.from_user.id, txt, reply_markup=_kb(rows), parse_mode="HTML")
    await cb.answer()


@mtr_router.callback_query(F.data.regexp(r"^mtr:adm:setpanel:(auto|\d+)$"))
async def adm_panel_set(cb: CallbackQuery, bot: Bot):
    from models import VpnPanel
    from tenant import set_setting
    row = await _admin_row(cb, bot)
    if not row:
        return
    arg = cb.data.split(":")[3]
    key = ""
    if arg != "auto":
        async with Session() as s:
            p = await s.get(VpnPanel, int(arg))
        if not p or p.bot_id != row.id or not p.enabled:
            await cb.answer("این پنل پیدا نشد یا فعال نیست.", show_alert=True)
            return
        key = p.panel_key
    await set_setting(row.id, "mtr_panel_key", key)
    await admin_menu(cb, bot)


_PROMPTS = {"price": ("💰 قیمتِ هر گیگ را به تومان بفرست (مثلاً 2000):", MtrFSM.price),
            "days": ("⏳ مدتِ بسته را به روز بفرست (مثلاً 30):", MtrFSM.days),
            "title": ("🏷 عنوانِ بسته را بفرست (مثلاً «بستهٔ ویژه»):", MtrFSM.title)}


@mtr_router.callback_query(F.data.regexp(r"^mtr:adm:(price|days|title)$"))
async def adm_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_row(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    prompt, st = _PROMPTS[key]
    await state.set_state(st)
    await bot.send_message(cb.from_user.id, prompt, reply_markup=_kb([[_btn("❌ لغو", "mtr:adm", ButtonStyle.DANGER)]]))
    await cb.answer()


async def _save_input(msg: Message, bot: Bot, state: FSMContext, key: str, lo: int, hi: int):
    from tenant import set_setting, is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id) or not await feature_ok(row):
        await state.clear()
        return
    t = _fa(msg.text or "")
    if not t.isdigit() or not (lo <= int(t) <= hi):
        await msg.answer(f"❌ عددِ معتبر بین {lo:,} تا {hi:,} بفرست.")
        return
    await set_setting(row.id, key, str(int(t)))
    await state.clear()
    txt, kb = await _admin_view(row.id)
    await msg.answer("✅ ذخیره شد.\n\n" + txt, reply_markup=kb, parse_mode="HTML")


@mtr_router.message(MtrFSM.price, F.text)
async def adm_price(msg: Message, bot: Bot, state: FSMContext):
    await _save_input(msg, bot, state, "mtr_price_gb", 1, 100_000_000)


@mtr_router.message(MtrFSM.days, F.text)
async def adm_days(msg: Message, bot: Bot, state: FSMContext):
    await _save_input(msg, bot, state, "mtr_days", 1, 3650)


@mtr_router.message(MtrFSM.title, F.text)
async def adm_title(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting, is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id) or not await feature_ok(row):
        await state.clear()
        return
    t = (msg.text or "").strip()
    if not t or len(t) > 60:
        await msg.answer("❌ عنوان باید ۱ تا ۶۰ کاراکتر باشد.")
        return
    await set_setting(row.id, "mtr_title", t)
    await state.clear()
    txt, kb = await _admin_view(row.id)
    await msg.answer("✅ ذخیره شد.\n\n" + txt, reply_markup=kb, parse_mode="HTML")


@mtr_router.callback_query(F.data == "mtr:adm:list")
async def adm_list(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    async with Session() as s:
        ms = (await s.execute(select(MeteredAccount, BotUser).join(BotUser, BotUser.id == MeteredAccount.user_id).where(
            MeteredAccount.bot_id == row.id, MeteredAccount.status.in_(("active", "no_credit"))
        ).order_by(MeteredAccount.id.desc()).limit(20))).all()
    lbl = {"active": "✅", "no_credit": "⛔️"}
    lines = ["📋 <b>بسته‌هایِ اعتباریِ جاری</b>\n"]
    rows = []
    for m, u in ms:
        lines.append(f"{lbl.get(m.status, '')} #{m.account_id} — {u.full_name or u.telegram_id} (<code>{u.telegram_id}</code>) — "
                     f"کسرشده {fmt(m.billed_toman)} — موجودی {fmt(Decimal(str(u.balance or 0)))}")
        rows.append([_btn(f"⚙️ #{m.account_id} — مدیریت (توقف/حذف)", f"usrsvcacc:{m.account_id}:{u.telegram_id}")])
    if not ms:
        lines.append("موردی نیست.")
    rows.append([_btn("⬅️ بازگشت", "mtr:adm", ButtonStyle.DANGER)])
    await cb.message.edit_text("\n".join(lines), reply_markup=_kb(rows), parse_mode="HTML")
    await cb.answer()
