"""
top_customers.py — قابلیت «برترین مشتریان» در بخش «آمار و گزارشات» پنل مدیریت.

منطق:
  • مبنای رتبه‌بندی: مجموع تومان خریدهای موفق (Order.status == "paid") هر
    کاربر در ۳۰ روز اخیر (created_at >= now - 30d).
  • نمایش ۱۰ نفر برتر + رتبهٔ خود مالک اگر داخل ۱۰ نفر نباشد (در این نسخه
    مالک ربات معمولاً مشتری نیست، پس همیشه «خارج از رتبه‌بندی» نشان داده
    می‌شود مگر این‌که مالک هم به‌عنوان کاربر در ربات خودش خرید کرده باشد).
  • تخفیف ۵ نفر اول: درصد یا تومان ثابت به ازای هر گیگ/هر روز — این مقادیر
    در زمان خرید (vc:pay) روی قیمت لحظه‌ای پنل مربوطه اعمال می‌شود.
  • قسمت‌گذاری: برای هر panel_key که ادمین فعال کرده، می‌تواند یک عنوان
    نمایشی دلخواه + شکلک (اموجی) + تخفیف مخصوص آن پنل (مستقل از تخفیف عمومی
    ۵ نفر اول) تعریف کند. اگر برای یک پنل override تعریف نشده باشد، عنوان
    پیش‌فرض PANEL_LABELS و تخفیف عمومی استفاده می‌شود.

تمام تنظیمات روی جدول Setting (key/value) ذخیره می‌شوند — بدون نیاز به
migration یا ستون/جدول جدید:
  tc_enabled                 → "1"/"0"  فعال/غیرفعال کل قابلیت
  tc_discount_mode           → "percent" یا "flat"
  tc_discount_gb             → عدد (درصد یا تومان، بر اساس mode)
  tc_discount_day            → عدد (درصد یا تومان، بر اساس mode)
  tc_panel_{panel_key}_label → عنوان دلخواه قسمت (مثلاً "ویژه")
  tc_panel_{panel_key}_emoji → شکلک دلخواه قسمت (مثلاً "💎")
  tc_panel_{panel_key}_mode  → "percent"/"flat" تخفیف مخصوص این پنل
  tc_panel_{panel_key}_gb    → تخفیف هر گیگ مخصوص این پنل
  tc_panel_{panel_key}_day   → تخفیف هر روز مخصوص این پنل
  tc_rank_enabled            → "1"/"0"  فعال/غیرفعال حالت «تخفیف بر اساس رتبه»
  tc_rank_{n}_percent        → درصد تخفیف اختصاصی رتبهٔ n (n از ۱ تا DISCOUNT_N)

  توجه: وقتی tc_rank_enabled فعال باشد، تخفیف هر یک از ۵ نفر اول فقط بر
  اساس همان درصد رتبه‌اش محاسبه می‌شود (روی هر گیگ و هر روز، مستقل از
  قسمت‌گذاری پنل‌ها). با غیرفعال بودنش، منطق قبلی (تخفیف عمومی/قسمتی)
  همچنان برقرار است.

callbackها با پیشوند: tc:
  tc:menu                 — صفحهٔ اصلی برترین مشتریان (لیست + تخفیف‌های روز)
  tc:toggle                — فعال/غیرفعال کردن کل قابلیت
  tc:settings              — تنظیمات تخفیف عمومی ۵ نفر اول
  tc:mode:{percent|flat}   — تغییر نوع تخفیف عمومی
  tc:segments              — لیست پنل‌ها برای قسمت‌گذاری
  tc:seg:{panel_key}       — ویرایش قسمت یک پنل خاص
  tc:seg_mode:{key}:{m}    — تغییر نوع تخفیف یک قسمت
  tc:ranks                 — صفحهٔ تخفیف بر اساس رتبه (۱ تا ۵)
  tc:rank_toggle           — فعال/غیرفعال کردن حالت رتبه‌ای
  tc:rank:{n}              — تنظیم درصد تخفیف رتبهٔ n
"""
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select

from config import Session
# === PATCH_TOP_CUSTOMERS_REFERRAL_BASIS_V1 ===
from models import (
    Bot as BotRow, BotUser, Order, PartnerBot, PartnerCardOrder, ReferralJoin,
    Setting, WalletTxn,
)
from plans import has_feature, Feature

top_customers_router = Router()

TOP_N = 10
DISCOUNT_N = 5
WINDOW_DAYS = 30

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉", 4: "🎗", 5: "⭐"}


class TcFSM(StatesGroup):
    set_gb = State()
    set_day = State()
    seg_label = State()
    seg_emoji = State()
    seg_gb = State()
    seg_day = State()
    rank_percent = State()


# ─────────────────────── کمک‌توابع تنظیمات ───────────────────────

async def _tget(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return row.value if row else default


async def _tset(bot_id: int, key: str, value: str):
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


async def tc_is_enabled(bot_id: int) -> bool:
    return (await _tget(bot_id, "tc_enabled", "0")) == "1"


async def tc_general_discount(bot_id: int) -> tuple[str, int, int]:
    """برمی‌گرداند (mode, gb_discount, day_discount) تخفیف عمومی ۵ نفر اول."""
    mode = await _tget(bot_id, "tc_discount_mode", "flat")
    gb = int(await _tget(bot_id, "tc_discount_gb", "1000") or 0)
    day = int(await _tget(bot_id, "tc_discount_day", "500") or 0)
    return mode, gb, day


async def tc_segment(bot_id: int, panel_key: str) -> dict:
    """تنظیمات قسمت یک پنل خاص — اگر override نشده، مقدار خام برمی‌گردد."""
    label = await _tget(bot_id, f"tc_panel_{panel_key}_label", "")
    emoji = await _tget(bot_id, f"tc_panel_{panel_key}_emoji", "")
    mode = await _tget(bot_id, f"tc_panel_{panel_key}_mode", "")
    gb = await _tget(bot_id, f"tc_panel_{panel_key}_gb", "")
    day = await _tget(bot_id, f"tc_panel_{panel_key}_day", "")
    return {"label": label, "emoji": emoji, "mode": mode, "gb": gb, "day": day}


async def tc_segment_label(bot_id: int, panel_key: str, fallback: str) -> str:
    seg = await tc_segment(bot_id, panel_key)
    if seg["label"]:
        emoji = seg["emoji"] or ""
        return f"{emoji} {seg['label']}".strip()
    return fallback


async def tc_rank_enabled(bot_id: int) -> bool:
    """آیا حالت «تخفیف بر اساس رتبه» (درصد جدا برای هر یک از ۵ نفر اول) فعال است؟"""
    return (await _tget(bot_id, "tc_rank_enabled", "0")) == "1"


async def tc_rank_percent(bot_id: int, rank: int) -> int:
    """درصد تخفیف اختصاصی رتبهٔ rank (۱ تا ۵)."""
    val = int(await _tget(bot_id, f"tc_rank_{rank}_percent", "0") or 0)
    return max(0, min(100, val))

async def tc_rank_basis(bot_id: int) -> str:
    """مبنایِ رتبه‌بندیِ «نفرات برتر»: "toman" (مجموع خرید) یا "referral" (تعداد دعوت).
    پیش‌فرض همان رفتار قبلی یعنی "toman" است."""
    v = await _tget(bot_id, "tc_rank_basis", "toman")
    return v if v in ("toman", "referral") else "toman"


async def get_user_rank(bot_id: int, telegram_id: int) -> int | None:
    """رتبهٔ کاربر در ۵ نفر برتر ۳۰ روز اخیر (۱ تا DISCOUNT_N) یا None."""
    if not await tc_is_enabled(bot_id):
        return None
    buyers = await top_ranked(bot_id, DISCOUNT_N)
    for i, (u, _) in enumerate(buyers, start=1):
        if u.telegram_id == telegram_id:
            return i
    return None


async def _apply_discount(price: int, mode: str, amount: int) -> int:
    if amount <= 0:
        return price
    if mode == "percent":
        amount = max(0, min(100, amount))
        return max(0, price - round(price * amount / 100))
    return max(0, price - amount)


async def top_buyers(bot_id: int, limit: int = TOP_N) -> list[tuple[BotUser, float]]:
    """لیست (کاربر، مجموع خرید) در ۳۰ روز اخیر، مرتب نزولی.

    === PATCH_PARTNER_TOP_CUSTOMERS_V1 ===
    باگ: این تابع فقط جدولِ Order را می‌خواند — که مخصوصِ خریدِ کانفیگِ
    VPN است. رباتِ همکار وقتی رباتی می‌فروشد، هیچ ردیفِ Order ای نمی‌سازد
    (مستقیم یک ردیفِ Bot تازه می‌سازد)، پس خریدارهایِ رباتِ همکار همیشه
    غایب بودند و رتبه‌بندی برایِ این نوع ربات عملاً همیشه خالی می‌ماند.

    فقط برایِ ربات‌هایِ همکار (field == "partner")، مجموع را از دو منبعِ
    واقعیِ فروشِ رباتِ همکار هم جمع می‌زنیم — بدونِ اینکه به ربات‌هایِ
    دیگر (VPN/فروشگاه) دست بزنیم، چون آن‌ها همان WalletTxnِ خریدِ کانفیگ
    را هم دارند و جمع‌زدنش آنجا باعثِ دوبار‌شمردنِ همان مبلغ می‌شد:
      • WalletTxn (kind="purchase", amount<0) — خریدِ کیف‌پولی
      • PartnerCardOrder (status="approved") — خریدِ کارت‌به‌کارتِ تاییدشده
    """
    since = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
    totals: dict[int, float] = {}
    users: dict[int, BotUser] = {}

    async with Session() as s:
        rows = (await s.execute(
            select(BotUser, func.sum(Order.amount).label("total"))
            .join(Order, Order.user_id == BotUser.id)
            .where(BotUser.bot_id == bot_id, Order.status == "paid",
                   Order.created_at >= since)
            .group_by(BotUser.id)
        )).all()
        for u, total in rows:
            totals[u.id] = totals.get(u.id, 0) + float(total or 0)
            users[u.id] = u

        bot_row = await s.get(BotRow, bot_id)
        if bot_row and bot_row.field == "partner":
            wt_rows = (await s.execute(
                select(BotUser, func.sum(-WalletTxn.amount).label("total"))
                .join(WalletTxn, WalletTxn.user_id == BotUser.id)
                .where(BotUser.bot_id == bot_id, WalletTxn.kind == "purchase",
                       WalletTxn.amount < 0, WalletTxn.created_at >= since)
                .group_by(BotUser.id)
            )).all()
            for u, total in wt_rows:
                totals[u.id] = totals.get(u.id, 0) + float(total or 0)
                users[u.id] = u

            pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == bot_id))
            if pb:
                co_rows = (await s.execute(
                    select(PartnerCardOrder.buyer_telegram_id,
                          func.sum(PartnerCardOrder.price).label("total"))
                    .where(PartnerCardOrder.partner_bot_id == pb.id,
                          PartnerCardOrder.status == "approved",
                          PartnerCardOrder.decided_at >= since)
                    .group_by(PartnerCardOrder.buyer_telegram_id)
                )).all()
                if co_rows:
                    tg_ids = [r[0] for r in co_rows]
                    bu_rows = (await s.execute(
                        select(BotUser).where(
                            BotUser.bot_id == bot_id, BotUser.telegram_id.in_(tg_ids)))
                    ).scalars().all()
                    by_tg = {u.telegram_id: u for u in bu_rows}
                    for tg_id, total in co_rows:
                        u = by_tg.get(tg_id)
                        if not u:
                            continue
                        totals[u.id] = totals.get(u.id, 0) + float(total or 0)
                        users[u.id] = u

        if bot_row and bot_row.field == "virtual_service":
            await _add_virtual_service_totals(s, bot_id, since, totals, users)

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [(users[uid], total) for uid, total in ranked]


async def _add_virtual_service_totals(s, bot_id: int, since, totals: dict, users: dict) -> None:
    """رباتِ «خدمات مجازی» Order نمی‌سازد؛ خریدهایش در SmmOrder / GiftOrder (استارز، گیفت، تون) / WalletTxn(شمارهٔ مجازی)
    است. قبلاً هیچ‌کدام در «نفراتِ برتر» حساب نمی‌شد (کارت‌به‌کارت و کیف‌پول هر دو). حالا هر روشِ پرداخت — چون
    هر دو همان ردیفِ سفارش را می‌سازند — یک بار شمرده می‌شود."""
    from models import GiftOrder, SmmOrder

    def _add(u, amount):
        if amount and amount > 0:
            totals[u.id] = totals.get(u.id, 0) + float(amount)
            users[u.id] = u

    smm = (await s.execute(
        select(BotUser, func.sum(SmmOrder.price_toman - func.coalesce(SmmOrder.refunded_toman, 0)))
        .join(SmmOrder, (SmmOrder.user_telegram_id == BotUser.telegram_id) & (SmmOrder.bot_id == BotUser.bot_id))
        .where(BotUser.bot_id == bot_id, SmmOrder.created_at >= since,
               SmmOrder.status.in_(("sent", "completed", "partial")))
        .group_by(BotUser.id))).all()
    for u, total in smm:
        _add(u, float(total or 0))

    gift = (await s.execute(
        select(BotUser, func.sum(GiftOrder.price_toman * func.coalesce(GiftOrder.quantity, 1)
                                 - func.coalesce(GiftOrder.discount_amount, 0)))
        .join(GiftOrder, GiftOrder.buyer_user_id == BotUser.id)
        .where(BotUser.bot_id == bot_id, GiftOrder.created_at >= since, GiftOrder.status == "sent")
        .group_by(BotUser.id))).all()
    for u, total in gift:
        _add(u, float(total or 0))

    vnum = (await s.execute(
        select(BotUser, func.sum(-WalletTxn.amount))
        .join(WalletTxn, WalletTxn.user_id == BotUser.id)
        .where(BotUser.bot_id == bot_id, WalletTxn.kind == "smm_order", WalletTxn.amount < 0,
               WalletTxn.created_at >= since)
        .group_by(BotUser.id))).all()
    for u, total in vnum:
        _add(u, float(total or 0))


async def top_referrers(bot_id: int, limit: int = TOP_N) -> list[tuple[BotUser, float]]:
    """لیست (کاربر، تعداد دعوتِ موفق) در ۳۰ روز اخیر، مرتب نزولی.
    مبنا: رکوردهایِ ReferralJoin (هر رکورد یعنی یک دعوت‌شدهٔ منحصربه‌فرد
    که با موفقیت به ربات پیوسته)."""
    since = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
    async with Session() as s:
        rows = (await s.execute(
            select(BotUser, func.count(ReferralJoin.id).label("cnt"))
            .join(ReferralJoin, ReferralJoin.inviter_user_id == BotUser.id)
            .where(BotUser.bot_id == bot_id, ReferralJoin.bot_id == bot_id,
                   ReferralJoin.created_at >= since)
            .group_by(BotUser.id)
            .order_by(func.count(ReferralJoin.id).desc())
            .limit(limit)
        )).all()
    return [(r[0], float(r[1] or 0)) for r in rows]


async def top_ranked(bot_id: int, limit: int = TOP_N) -> list[tuple[BotUser, float]]:
    """دیسپچرِ رتبه‌بندی طبق tc_rank_basis — همه‌جا به‌جایِ top_buyers از این
    استفاده می‌شود تا مبنایِ تومان/رفرال روی کل قابلیت یکسان اعمال شود."""
    basis = await tc_rank_basis(bot_id)
    if basis == "referral":
        return await top_referrers(bot_id, limit)
    return await top_buyers(bot_id, limit)


async def is_top_discount_user(bot_id: int, telegram_id: int) -> bool:
    """آیا این کاربر در ۵ نفر برتر ۳۰ روز اخیر است؟"""
    return await get_user_rank(bot_id, telegram_id) is not None


async def vc_price_with_discount(bot_id: int, telegram_id: int, panel_key: str,
                                  base_pg: int, base_pd: int) -> tuple[int, int, bool]:
    """قیمت مؤثر هر گیگ/روز برای یک کاربر روی یک پنل خاص — با اعمال
    تخفیف ۵ نفر برتر در صورت وجود.
    اگر حالت «تخفیف بر اساس رتبه» فعال باشد، فقط درصد اختصاصی همان رتبه
    اعمال می‌شود (مستقل از قسمت‌گذاری پنل‌ها). وگرنه طبق منطق قبلی، اول
    تخفیف مخصوص قسمت (اگر تعریف شده) بررسی می‌شود، وگرنه تخفیف عمومی.
    برمی‌گرداند (pg, pd, discounted: bool)."""
    rank = await get_user_rank(bot_id, telegram_id)
    if rank is None:
        return base_pg, base_pd, False

    if await tc_rank_enabled(bot_id):
        percent = await tc_rank_percent(bot_id, rank)
        if percent <= 0:
            return base_pg, base_pd, False
        pg = await _apply_discount(base_pg, "percent", percent)
        pd = await _apply_discount(base_pd, "percent", percent)
        return pg, pd, True

    seg = await tc_segment(bot_id, panel_key)
    if seg["mode"] and (seg["gb"] or seg["day"]):
        mode = seg["mode"]
        gb_amt = int(seg["gb"] or 0)
        day_amt = int(seg["day"] or 0)
    else:
        mode, gb_amt, day_amt = await tc_general_discount(bot_id)
    pg = await _apply_discount(base_pg, mode, gb_amt)
    pd = await _apply_discount(base_pd, mode, day_amt)
    return pg, pd, True


# ─────────────────────── نمایش صفحهٔ اصلی ───────────────────────

def _fmt_amount(n: float) -> str:
    return f"{int(n):,}"


async def _build_main_text(bot_id: int, viewer_telegram_id: int) -> str:
    enabled = await tc_is_enabled(bot_id)
    basis = await tc_rank_basis(bot_id)
    buyers = await top_ranked(bot_id, TOP_N)
    title = "برترین مشتریان" if basis == "toman" else "برترین دعوت‌کننده‌ها"
    unit_lbl = "" if basis == "toman" else " نفر دعوت"
    lines = [f"🏆 <b>{title} {WINDOW_DAYS} روز گذشته</b>\n"]
    if not buyers:
        lines.append(
            "هنوز خریدی در این بازه ثبت نشده." if basis == "toman"
            else "هنوز دعوتِ موفقی در این بازه ثبت نشده.")
    else:
        for i, (u, total) in enumerate(buyers, start=1):
            medal = _MEDALS.get(i, str(i) + "️⃣" if i <= 9 else str(i))
            name = u.full_name or u.username or f"#{u.telegram_id}"
            val = _fmt_amount(total) + unit_lbl if basis == "referral" else _fmt_amount(total)
            lines.append(f"{medal} {name} — {val}")
    # رتبهٔ بیننده
    rank = None
    viewer_total = 0.0
    for i, (u, total) in enumerate(buyers, start=1):
        if u.telegram_id == viewer_telegram_id:
            rank, viewer_total = i, total
            break
    lines.append("")
    viewer_lbl = "مجموع خرید" if basis == "toman" else "تعداد دعوت"
    if rank:
        lines.append(f"👤 رتبهٔ شما: {rank} — {viewer_lbl}: {_fmt_amount(viewer_total)}")
    else:
        lines.append(f"👤 رتبهٔ شما: خارج از رتبه‌بندی — {viewer_lbl}: 0")
    if not enabled:
        lines.append("\n⛔️ این قابلیت در حال حاضر برای کاربران غیرفعال است.")
        return "\n".join(lines)
    # === PATCH_PARTNER_TOP_CUSTOMERS_V1 ===
    # برایِ رباتِ همکار (و هر نوعِ غیرِ VPN)، بخشِ «قیمت‌هایِ پنل‌ها بعدِ
    # تخفیف» اصلاً معنی ندارد (نه پنلی هست، نه گیگ/روزی) — قبلاً همین‌جا
    # پیامِ گمراه‌کنندهٔ «هیچ پنلی فعال نشده» نشان داده می‌شد.
    async with Session() as s:
        bot_row = await s.get(BotRow, bot_id)
    if bot_row and bot_row.field != "vpn_sales":
        rank_mode = await tc_rank_enabled(bot_id)
        if rank_mode:
            lines.append(f"\n💸 تخفیف بر اساس رتبه (۱ تا {DISCOUNT_N}):")
            for n in range(1, DISCOUNT_N + 1):
                pct = await tc_rank_percent(bot_id, n)
                medal = _MEDALS.get(n, str(n))
                lines.append(f"{medal} رتبهٔ {n}: {pct}%")
        return "\n".join(lines)
    from tenant.bots.vpn.vpn_admin import get_all_panels, PANEL_LABELS
    panels = await get_all_panels(bot_id)
    rank_mode = await tc_rank_enabled(bot_id)
    if rank_mode:
        lines.append(f"\n💸 تخفیف بر اساس رتبه (۱ تا {DISCOUNT_N}):")
        for n in range(1, DISCOUNT_N + 1):
            pct = await tc_rank_percent(bot_id, n)
            medal = _MEDALS.get(n, str(n))
            lines.append(f"{medal} رتبهٔ {n}: {pct}%")
    else:
        mode, gb_amt, day_amt = await tc_general_discount(bot_id)
        lines.append(f"\n💸 تخفیف برای {DISCOUNT_N} نفر اول:")
        if not panels:
            lines.append("هیچ پنلی هنوز فعال نشده.")
        for p in panels:
            if not p.enabled:
                continue
            fallback = PANEL_LABELS.get(p.panel_key, p.panel_key)
            label = await tc_segment_label(bot_id, p.panel_key, fallback)
            seg = await tc_segment(bot_id, p.panel_key)
            if seg["mode"] and (seg["gb"] or seg["day"]):
                m, g, d = seg["mode"], int(seg["gb"] or 0), int(seg["day"] or 0)
            else:
                m, g, d = mode, gb_amt, day_amt
            u2 = "%" if m == "percent" else "تومان"
            lines.append(f"{label}: هر گیگ: {g:,}{u2}  |  هر روز: {d:,}{u2}")
    lines.append("\n💰 قیمت‌های شما (بعد از تخفیف):")
    from tenant.bots.vpn.vpn_user import _vc
    for p in panels:
        if not p.enabled:
            continue
        fallback = PANEL_LABELS.get(p.panel_key, p.panel_key)
        label = await tc_segment_label(bot_id, p.panel_key, fallback)
        base_pg = int(await _vc(bot_id, "vc_price_gb", p.panel_key))
        base_pd = int(await _vc(bot_id, "vc_price_day", p.panel_key))
        eff_pg, eff_pd, disc = await vc_price_with_discount(
            bot_id, viewer_telegram_id, p.panel_key, base_pg, base_pd)
        lines.append(f"{label}: هر گیگ: {eff_pg:,}  |  هر روز: {eff_pd:,}")
    lines.append("\nℹ️ توجه کنید تخفیف‌ها در آخر هر روز، ساعت 00:00 اعمال می‌شوند.")
    return "\n".join(lines)


def _main_kb(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="tc:menu", style=ButtonStyle.SUCCESS)]]
    if is_owner:
        rows.append([InlineKeyboardButton(text="⚙️ تنظیمات (فقط مالک)", callback_data="tc:settings", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت به پنل مدیریت", callback_data="tadm:sec:stats",
                                          style=ButtonStyle.DANGER)])
    else:
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="mbtn:home", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@top_customers_router.callback_query(F.data == "tc:menu")
async def tc_menu(cb: CallbackQuery, bot: Bot):
    """صفحهٔ اصلی «نفرات برتر» — برای همهٔ کاربران ربات (نه فقط مالک).
    دسترسی فقط بر اساس قابلیت پلن ربات کنترل می‌شود، نه مالکیت."""
    from tenant import get_bot_row, edit_or_replace
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if not has_feature(row.plan, Feature.ADVANCED_STATS):
        await cb.answer("🔒 این قابلیت در پلن فعلی این ربات روشن نیست.", show_alert=True)
        return
    text = await _build_main_text(row.id, cb.from_user.id)
    await edit_or_replace(cb.message, text, reply_markup=_main_kb(row.owner_id == cb.from_user.id))
    await cb.answer()


# ─────────────────────── تنظیمات (ادمین) ───────────────────────

def _settings_kb(enabled: bool, mode: str, basis: str = "toman",
                 field: str = "vpn_sales") -> InlineKeyboardMarkup:
    # === PATCH_PARTNER_TOP_CUSTOMERS_V1 ===
    # باگ: این کیبورد همیشه «نوعِ تخفیف»، «تخفیف هر گیگ/روز» و «قسمت‌گذاری
    # پنل‌ها» را نشان می‌داد، حتی برایِ رباتِ همکار که اصلاً پنلِ VPN یا
    # مفهومِ گیگ/روز ندارد — همان چیزی که ادمینِ همکار را گیج کرده بود.
    # این سه فقط برایِ ربات‌هایِ VPN معنی دارند.
    tog = "🔴 غیرفعال کردن قابلیت" if enabled else "🟢 فعال کردن قابلیت"
    toman_mark = "✅ " if basis == "toman" else ""
    ref_mark = "✅ " if basis == "referral" else ""
    rows = [
        [InlineKeyboardButton(text=tog, callback_data="tc:toggle", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"{toman_mark}💰 بر اساس تومان", callback_data="tc:basis:toman", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"{ref_mark}👥 بر اساس رفرال", callback_data="tc:basis:referral", style=ButtonStyle.PRIMARY)],
    ]
    if field == "vpn_sales":
        pct_mark = "✅ " if mode == "percent" else ""
        flat_mark = "✅ " if mode == "flat" else ""
        rows.append(
            [InlineKeyboardButton(text=f"{pct_mark}درصدی (%)", callback_data="tc:mode:percent", style=ButtonStyle.PRIMARY),
             InlineKeyboardButton(text=f"{flat_mark}تومان ثابت", callback_data="tc:mode:flat", style=ButtonStyle.PRIMARY)])
        rows.append(
            [InlineKeyboardButton(text="💎 تخفیف هر گیگ", callback_data="tc:set:gb", style=ButtonStyle.PRIMARY),
             InlineKeyboardButton(text="📅 تخفیف هر روز", callback_data="tc:set:day", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="🧩 قسمت‌گذاری پنل‌ها", callback_data="tc:segments", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="🏅 تخفیف بر اساس رتبه (۱ تا ۵)", callback_data="tc:ranks", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:stats", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _settings_text(bot_id: int, field: str = "vpn_sales") -> str:
    enabled = await tc_is_enabled(bot_id)
    mode, gb, day = await tc_general_discount(bot_id)
    basis = await tc_rank_basis(bot_id)
    basis_lbl = "💰 تومان (مجموع خرید)" if basis == "toman" else "👥 رفرال (تعداد دعوت)"
    st = "🟢 فعال" if enabled else "🔴 غیرفعال"
    if field != "vpn_sales":
        # === PATCH_PARTNER_TOP_CUSTOMERS_V1 ===
        # برایِ رباتِ همکار (و هر نوعِ غیرِ VPN) اصلاً «هر گیگ/هر روز» معنی
        # ندارد؛ اینجا فقط رتبه‌بندی و درِ ورودیِ تخفیفِ رتبه‌ای را می‌گوییم.
        return (
            f"⚙️ <b>تنظیمات برترین مشتریان</b>\n\n"
            f"وضعیت: {st}\n"
            f"مبنایِ رتبه‌بندی: {basis_lbl}\n\n"
            f"برایِ این نوع ربات، تخفیف بر اساسِ «هر گیگ/هر روز» معنی ندارد. "
            f"از «🏅 تخفیف بر اساس رتبه» یک درصدِ تخفیف برایِ هرکدام از "
            f"{DISCOUNT_N} نفرِ برترِ ماهِ اخیر تعریف کن — همان درصد رویِ "
            f"قیمتِ خریدِ بعدیِ همان کاربر اعمال می‌شود."
        )
    unit = "%" if mode == "percent" else "تومان"
    return (
        f"⚙️ <b>تنظیمات برترین مشتریان</b>\n\n"
        f"وضعیت: {st}\n"
        f"مبنایِ رتبه‌بندی: {basis_lbl}\n"
        f"نوع تخفیف: {'درصدی' if mode == 'percent' else 'تومان ثابت'}\n"
        f"تخفیف هر گیگ: {gb:,}{unit}\n"
        f"تخفیف هر روز: {day:,}{unit}\n\n"
        f"این تخفیف عمومی برای {DISCOUNT_N} نفر اول روی پنل‌هایی که قسمت "
        f"اختصاصی برایشان تعریف نشده اعمال می‌شود."
    )


@top_customers_router.callback_query(F.data == "tc:settings")
async def tc_settings(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    enabled = await tc_is_enabled(row.id)
    mode, _, _ = await tc_general_discount(row.id)
    basis = await tc_rank_basis(row.id)
    await edit_or_replace(cb.message, await _settings_text(row.id, row.field),
                          reply_markup=_settings_kb(enabled, mode, basis, row.field))
    await cb.answer()


@top_customers_router.callback_query(F.data.startswith("tc:basis:"))
async def tc_basis(cb: CallbackQuery, bot: Bot):
    """جابه‌جاییِ مبنایِ رتبه‌بندیِ نفراتِ برتر بینِ تومان و رفرال."""
    from tenant import _owner_ok, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    new_basis = cb.data.split(":")[2]
    if new_basis not in ("toman", "referral"):
        await cb.answer()
        return
    await _tset(row.id, "tc_rank_basis", new_basis)
    enabled = await tc_is_enabled(row.id)
    mode, _, _ = await tc_general_discount(row.id)
    await edit_or_replace(cb.message, await _settings_text(row.id, row.field),
                          reply_markup=_settings_kb(enabled, mode, new_basis, row.field))
    await cb.answer("✅ مبنا تغییر کرد")


@top_customers_router.callback_query(F.data == "tc:toggle")
async def tc_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await tc_is_enabled(row.id)
    await _tset(row.id, "tc_enabled", "0" if cur else "1")
    enabled = not cur
    mode, _, _ = await tc_general_discount(row.id)
    basis = await tc_rank_basis(row.id)
    await edit_or_replace(cb.message, await _settings_text(row.id, row.field),
                          reply_markup=_settings_kb(enabled, mode, basis, row.field))
    await cb.answer("✅ فعال شد" if enabled else "غیرفعال شد")


@top_customers_router.callback_query(F.data.startswith("tc:mode:"))
async def tc_mode(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    new_mode = cb.data.split(":")[2]
    await _tset(row.id, "tc_discount_mode", new_mode)
    enabled = await tc_is_enabled(row.id)
    basis = await tc_rank_basis(row.id)
    await edit_or_replace(cb.message, await _settings_text(row.id, row.field),
                          reply_markup=_settings_kb(enabled, new_mode, basis, row.field))
    await cb.answer()


@top_customers_router.callback_query(F.data == "tc:set:gb")
async def tc_set_gb_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mode, _, _ = await tc_general_discount(row.id)
    unit = "درصد (0 تا 100)" if mode == "percent" else "تومان"
    await state.set_state(TcFSM.set_gb)
    await cb.message.answer(f"💎 مقدار تخفیف هر گیگ را به {unit} بفرست:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@top_customers_router.message(TcFSM.set_gb, F.text)
async def tc_set_gb_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg, fa_to_int
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر. یک عدد بفرست:")
        return
    await _tset(row.id, "tc_discount_gb", str(val))
    await state.clear()
    enabled = await tc_is_enabled(row.id)
    mode, _, _ = await tc_general_discount(row.id)
    await msg.answer(await _settings_text(row.id), reply_markup=_settings_kb(enabled, mode))


@top_customers_router.callback_query(F.data == "tc:set:day")
async def tc_set_day_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mode, _, _ = await tc_general_discount(row.id)
    unit = "درصد (0 تا 100)" if mode == "percent" else "تومان"
    await state.set_state(TcFSM.set_day)
    await cb.message.answer(f"📅 مقدار تخفیف هر روز را به {unit} بفرست:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@top_customers_router.message(TcFSM.set_day, F.text)
async def tc_set_day_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg, fa_to_int
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر. یک عدد بفرست:")
        return
    await _tset(row.id, "tc_discount_day", str(val))
    await state.clear()
    enabled = await tc_is_enabled(row.id)
    mode, _, _ = await tc_general_discount(row.id)
    await msg.answer(await _settings_text(row.id), reply_markup=_settings_kb(enabled, mode))


# ─────────────────────── قسمت‌گذاری پنل‌ها ───────────────────────

@top_customers_router.callback_query(F.data == "tc:segments")
async def tc_segments(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    from tenant.bots.vpn.vpn_admin import get_all_panels, PANEL_LABELS
    row = await _owner_ok(cb, bot)
    if not row:
        return
    panels = await get_all_panels(row.id)
    rows_kb = []
    if not panels:
        text = "🧩 <b>قسمت‌گذاری پنل‌ها</b>\n\nهنوز هیچ پنلی متصل/فعال نکرده‌اید."
    else:
        text = "🧩 <b>قسمت‌گذاری پنل‌ها</b>\n\nروی هر پنل بزن تا اسم، شکلک و تخفیف اختصاصی‌اش را تنظیم کنی:"
        for p in panels:
            fallback = PANEL_LABELS.get(p.panel_key, p.panel_key)
            label = await tc_segment_label(row.id, p.panel_key, fallback)
            st = "🟢" if p.enabled else "🔴"
            rows_kb.append([InlineKeyboardButton(
                text=f"{st} {label}", callback_data=f"tc:seg:{p.panel_key}", style=ButtonStyle.PRIMARY)])
    rows_kb.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tc:settings",
                                         style=ButtonStyle.DANGER)])
    await edit_or_replace(cb.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb))
    await cb.answer()


async def _seg_text(bot_id: int, panel_key: str, fallback: str) -> str:
    seg = await tc_segment(bot_id, panel_key)
    label = await tc_segment_label(bot_id, panel_key, fallback)
    if seg["mode"]:
        unit = "%" if seg["mode"] == "percent" else "تومان"
        disc_line = (f"تخفیف اختصاصی: هر گیگ {int(seg['gb'] or 0):,}{unit}  |  "
                     f"هر روز {int(seg['day'] or 0):,}{unit}")
    else:
        disc_line = "تخفیف اختصاصی تعریف نشده — تخفیف عمومی اعمال می‌شود."
    return (
        f"🧩 <b>قسمت: {label}</b>\n\n"
        f"کلید پنل: <code>{panel_key}</code>\n"
        f"عنوان فعلی: {seg['label'] or '(پیش‌فرض)'}\n"
        f"شکلک فعلی: {seg['emoji'] or '(پیش‌فرض)'}\n"
        f"{disc_line}"
    )


def _seg_kb(panel_key: str, has_override: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ تغییر عنوان", callback_data=f"tc:seg_label:{panel_key}", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="🙂 تغییر شکلک", callback_data=f"tc:seg_emoji:{panel_key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="درصدی (%)", callback_data=f"tc:seg_mode:{panel_key}:percent", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="تومان ثابت", callback_data=f"tc:seg_mode:{panel_key}:flat", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💎 تخفیف هر گیگ", callback_data=f"tc:seg_gb:{panel_key}", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="📅 تخفیف هر روز", callback_data=f"tc:seg_day:{panel_key}", style=ButtonStyle.PRIMARY)],
    ]
    if has_override:
        rows.append([InlineKeyboardButton(text="🗑 حذف تنظیمات اختصاصی این قسمت",
                                          callback_data=f"tc:seg_reset:{panel_key}",
                                          style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tc:segments",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@top_customers_router.callback_query(F.data.startswith("tc:seg:"))
async def tc_seg_open(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    row = await _owner_ok(cb, bot)
    if not row:
        return
    panel_key = cb.data.split(":", 2)[2]
    fallback = PANEL_LABELS.get(panel_key, panel_key)
    seg = await tc_segment(row.id, panel_key)
    has_override = bool(seg["label"] or seg["emoji"] or seg["mode"])
    await edit_or_replace(cb.message, await _seg_text(row.id, panel_key, fallback),
                          reply_markup=_seg_kb(panel_key, has_override))
    await cb.answer()


@top_customers_router.callback_query(F.data.startswith("tc:seg_mode:"))
async def tc_seg_mode(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, panel_key, m = cb.data.split(":")
    await _tset(row.id, f"tc_panel_{panel_key}_mode", m)
    fallback = PANEL_LABELS.get(panel_key, panel_key)
    await edit_or_replace(cb.message, await _seg_text(row.id, panel_key, fallback),
                          reply_markup=_seg_kb(panel_key, True))
    await cb.answer()


@top_customers_router.callback_query(F.data.startswith("tc:seg_reset:"))
async def tc_seg_reset(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    row = await _owner_ok(cb, bot)
    if not row:
        return
    panel_key = cb.data.split(":", 2)[2]
    for suffix in ("label", "emoji", "mode", "gb", "day"):
        await _tset(row.id, f"tc_panel_{panel_key}_{suffix}", "")
    fallback = PANEL_LABELS.get(panel_key, panel_key)
    await edit_or_replace(cb.message, await _seg_text(row.id, panel_key, fallback),
                          reply_markup=_seg_kb(panel_key, False))
    await cb.answer("✅ به پیش‌فرض برگشت")


@top_customers_router.callback_query(F.data.startswith("tc:seg_label:"))
async def tc_seg_label_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    panel_key = cb.data.split(":", 2)[2]
    await state.set_state(TcFSM.seg_label)
    await state.update_data(tc_panel_key=panel_key)
    await cb.message.answer("✏️ عنوان دلخواه این قسمت را بفرست (مثلاً «ویژه»):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@top_customers_router.message(TcFSM.seg_label, F.text)
async def tc_seg_label_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    panel_key = data.get("tc_panel_key", "")
    await _tset(row.id, f"tc_panel_{panel_key}_label", msg.text.strip()[:40])
    await state.clear()
    fallback = PANEL_LABELS.get(panel_key, panel_key)
    seg = await tc_segment(row.id, panel_key)
    has_override = bool(seg["label"] or seg["emoji"] or seg["mode"])
    await msg.answer(await _seg_text(row.id, panel_key, fallback),
                     reply_markup=_seg_kb(panel_key, has_override))


@top_customers_router.callback_query(F.data.startswith("tc:seg_emoji:"))
async def tc_seg_emoji_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    panel_key = cb.data.split(":", 2)[2]
    await state.set_state(TcFSM.seg_emoji)
    await state.update_data(tc_panel_key=panel_key)
    await cb.message.answer("🙂 یک شکلک (اموجی) برای این قسمت بفرست (مثلاً 💎):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@top_customers_router.message(TcFSM.seg_emoji, F.text)
async def tc_seg_emoji_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    panel_key = data.get("tc_panel_key", "")
    await _tset(row.id, f"tc_panel_{panel_key}_emoji", msg.text.strip()[:8])
    await state.clear()
    fallback = PANEL_LABELS.get(panel_key, panel_key)
    seg = await tc_segment(row.id, panel_key)
    has_override = bool(seg["label"] or seg["emoji"] or seg["mode"])
    await msg.answer(await _seg_text(row.id, panel_key, fallback),
                     reply_markup=_seg_kb(panel_key, has_override))


@top_customers_router.callback_query(F.data.startswith("tc:seg_gb:"))
async def tc_seg_gb_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    panel_key = cb.data.split(":", 2)[2]
    seg = await tc_segment(row.id, panel_key)
    mode = seg["mode"] or "flat"
    unit = "درصد (0 تا 100)" if mode == "percent" else "تومان"
    await state.set_state(TcFSM.seg_gb)
    await state.update_data(tc_panel_key=panel_key)
    await cb.message.answer(f"💎 تخفیف هر گیگ این قسمت را به {unit} بفرست:",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@top_customers_router.message(TcFSM.seg_gb, F.text)
async def tc_seg_gb_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg, fa_to_int
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر. یک عدد بفرست:")
        return
    data = await state.get_data()
    panel_key = data.get("tc_panel_key", "")
    seg = await tc_segment(row.id, panel_key)
    if not seg["mode"]:
        await _tset(row.id, f"tc_panel_{panel_key}_mode", "flat")
    await _tset(row.id, f"tc_panel_{panel_key}_gb", str(val))
    await state.clear()
    fallback = PANEL_LABELS.get(panel_key, panel_key)
    await msg.answer(await _seg_text(row.id, panel_key, fallback),
                     reply_markup=_seg_kb(panel_key, True))


@top_customers_router.callback_query(F.data.startswith("tc:seg_day:"))
async def tc_seg_day_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    panel_key = cb.data.split(":", 2)[2]
    seg = await tc_segment(row.id, panel_key)
    mode = seg["mode"] or "flat"
    unit = "درصد (0 تا 100)" if mode == "percent" else "تومان"
    await state.set_state(TcFSM.seg_day)
    await state.update_data(tc_panel_key=panel_key)
    await cb.message.answer(f"📅 تخفیف هر روز این قسمت را به {unit} بفرست:",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@top_customers_router.message(TcFSM.seg_day, F.text)
async def tc_seg_day_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg, fa_to_int
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر. یک عدد بفرست:")
        return
    data = await state.get_data()
    panel_key = data.get("tc_panel_key", "")
    seg = await tc_segment(row.id, panel_key)
    if not seg["mode"]:
        await _tset(row.id, f"tc_panel_{panel_key}_mode", "flat")
    await _tset(row.id, f"tc_panel_{panel_key}_day", str(val))
    await state.clear()
    fallback = PANEL_LABELS.get(panel_key, panel_key)
    await msg.answer(await _seg_text(row.id, panel_key, fallback),
                     reply_markup=_seg_kb(panel_key, True))


# ─────────────────────── تخفیف بر اساس رتبه (۱ تا ۵) ───────────────────────

def _ranks_kb(enabled: bool, percents: list[int]) -> InlineKeyboardMarkup:
    tog = "🔴 غیرفعال کردن حالت رتبه‌ای" if enabled else "🟢 فعال کردن حالت رتبه‌ای"
    rows = [[InlineKeyboardButton(text=tog, callback_data="tc:rank_toggle", style=ButtonStyle.PRIMARY)]]
    for n in range(1, DISCOUNT_N + 1):
        medal = _MEDALS.get(n, str(n))
        rows.append([InlineKeyboardButton(
            text=f"{medal} رتبهٔ {n}: {percents[n - 1]}%", callback_data=f"tc:rank:{n}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tc:settings",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ranks_text(bot_id: int, field: str = "vpn_sales") -> str:
    enabled = await tc_rank_enabled(bot_id)
    st = "🟢 فعال" if enabled else "🔴 غیرفعال"
    lines = ["🏅 <b>تخفیف بر اساس رتبه</b>\n", f"وضعیت: {st}\n"]
    if enabled:
        if field == "vpn_sales":
            lines.append(
                "در این حالت، به‌جای تخفیف عمومی یا اختصاصی قسمت‌ها، فقط همین درصدها "
                "بر اساس رتبهٔ کاربر در بین ۵ نفر برتر روی هر گیگ/روز اعمال می‌شود.\n"
                "روی هر رتبه بزن تا درصدش را تغییر بدهی."
            )
        else:
            # === PATCH_PARTNER_TOP_CUSTOMERS_V1 ===
            lines.append(
                "این درصدها بر اساسِ رتبهٔ کاربر در بینِ ۵ نفرِ برترِ ماهِ اخیر، رویِ "
                "قیمتِ خریدِ بعدیِ همان کاربر اعمال می‌شود.\n"
                "روی هر رتبه بزن تا درصدش را تغییر بدهی."
            )
    else:
        if field == "vpn_sales":
            lines.append(
                "در حالت غیرفعال، تخفیف عمومی یا اختصاصی هر قسمت (بخش‌های قبلی همین "
                "تنظیمات) اعمال می‌شود."
            )
        else:
            lines.append("در حالتِ غیرفعال، هیچ تخفیفی اعمال نمی‌شود.")
    return "\n".join(lines)


@top_customers_router.callback_query(F.data == "tc:ranks")
async def tc_ranks(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    enabled = await tc_rank_enabled(row.id)
    percents = [await tc_rank_percent(row.id, n) for n in range(1, DISCOUNT_N + 1)]
    await edit_or_replace(cb.message, await _ranks_text(row.id, row.field),
                          reply_markup=_ranks_kb(enabled, percents))
    await cb.answer()


@top_customers_router.callback_query(F.data == "tc:rank_toggle")
async def tc_rank_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await tc_rank_enabled(row.id)
    await _tset(row.id, "tc_rank_enabled", "0" if cur else "1")
    enabled = not cur
    percents = [await tc_rank_percent(row.id, n) for n in range(1, DISCOUNT_N + 1)]
    await edit_or_replace(cb.message, await _ranks_text(row.id, row.field),
                          reply_markup=_ranks_kb(enabled, percents))
    await cb.answer("✅ فعال شد" if enabled else "غیرفعال شد")


@top_customers_router.callback_query(F.data.startswith("tc:rank:"))
async def tc_rank_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    n = int(cb.data.split(":")[2])
    await state.set_state(TcFSM.rank_percent)
    await state.update_data(tc_rank_n=n)
    await cb.message.answer(f"🏅 درصد تخفیف رتبهٔ {n} را بفرست (عددی بین 0 تا 100):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@top_customers_router.message(TcFSM.rank_percent, F.text)
async def tc_rank_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg, fa_to_int
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0 or val > 100:
        await msg.answer("عدد نامعتبر. یک عدد بین 0 تا 100 بفرست:")
        return
    data = await state.get_data()
    n = data.get("tc_rank_n", 1)
    await _tset(row.id, f"tc_rank_{n}_percent", str(val))
    await state.clear()
    enabled = await tc_rank_enabled(row.id)
    percents = [await tc_rank_percent(row.id, k) for k in range(1, DISCOUNT_N + 1)]
    await msg.answer(await _ranks_text(row.id), reply_markup=_ranks_kb(enabled, percents))
