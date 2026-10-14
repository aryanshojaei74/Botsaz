"""
ref_points.py — «امتیازگیری با رفرال» (PATCH_REFERRAL_POINTS_V1).

هر زیرمجموعه‌ای که کاربر بیاورد، به‌اندازهٔ «امتیازِ هر رفرال» (پیش‌فرض ۱)
امتیاز می‌گیرد و امتیازها در RefPointWallet جمع می‌شوند. دکمه‌هایی که در
tenant/referral_gate.py با ملاکِ شمارشِ «points» گیت شده‌اند، در هر استفاده
به‌اندازهٔ آستانه از همین کیف کم می‌کنند — یعنی با آستانهٔ ۳: ۳ امتیاز =
یک‌بار استفاده، ۶ امتیاز = دوبار، و امتیاز باقی‌مانده سرِ جایش می‌ماند.

جایزهٔ این دکمه‌ها (وقتی دکمه عملکردِ دیگری ندارد) از همین‌جا تحویل می‌شود و
نوعش را مالکِ ربات در «🏅 امتیازگیری با رفرال» انتخاب می‌کند:
  • stock → کانفیگِ آمادهٔ انبار (حجم/مدتِ مشخص)
  • auto  → ساختِ کانفیگِ خودکار روی پنلِ متصل
  • text  → متنِ ثابت

تنظیمات در جدولِ Setting همین ربات:
  refpoint_enabled (0/1) | refpoint_per_join | refpoint_reward_kind
  refpoint_reward_vol | refpoint_reward_days | refpoint_reward_text
"""
from __future__ import annotations
import contextvars
import json
from datetime import datetime as _dtm
from html import escape as _esc_html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from config import Session
from plans import has_feature, Feature  # PATCH_PLAN_FEATURE_GATING_V1
from models import BotUser, Config, RefPointWallet, RefPrizeClaim, Setting

from tenant.bots.vpn.vpn_user import fmt_vol as _fmt_vol  # PATCH_UNLIMITED_LABEL_V1

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

rpt_router = Router()

REWARD_LABELS = {
    "stock": "📦 کانفیگِ آمادهٔ انبار",
    "auto": "⚙️ کانفیگِ خودکار (از پنل)",
    "text": "📝 متنِ ثابت",
    # === PATCH_REFPOINT_PRODUCT_REWARD_V1 ===
    "product": "🛍 محصولِ فروشگاه",
    # === PATCH_REFPOINT_SMM_REWARD_V1 ===
    "smm": "📣 سرویسِ خدماتِ مجازی",
    # === PATCH_REFPOINT_PRIZES_V1 ===
    "gift": "🎁 گیفتِ تلگرامی (استارزی)",
    "stars": "⭐ استارز تلگرام",
}

# === PATCH_REFPOINT_PANEL_KIND_PICKER_V1 ===
CFG_KIND_LABELS = {
    "all": "🌐 همه",
    "sublink": "🔗 فقط لینکِ ساب (V2Ray)",
    "wireguard": "🟪 فقط وایرگارد",
    "openvpn": "🔐 فقط اوپن‌وی‌پی‌ان",
}


# ═══════════════════════════════════════════
#  تنظیمات
# ═══════════════════════════════════════════

# === PATCH_REFPOINT_PRIZES_V1 === وقتی یکی از جایزه‌هایِ «فهرست» تحویل می‌شود، تنظیماتِ همان جایزه
# (عکسِ لحظهٔ افزودن) به‌جایِ تنظیماتِ زندهٔ refpoint_reward_* خوانده می‌شود؛ پس همهٔ تابع‌هایِ تحویلِ قبلی
# (کانفیگ/محصول/SMM/…) بدونِ تغییر برایِ هر جایزهٔ فهرست هم کار می‌کنند.
_CFG_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("refpoint_cfg_override", default=None)


async def _get(bot_id: int, key: str, default: str = "") -> str:
    _ov = _CFG_OVERRIDE.get()
    if _ov is not None and key in _ov:
        return _ov[key] if _ov[key] is not None else default
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == key))
        return row.value if row and row.value is not None else default


async def _set(bot_id: int, key: str, value: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == key))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


async def points_enabled(bot_id: int) -> bool:
    return (await _get(bot_id, "refpoint_enabled", "0")) == "1"


async def per_join(bot_id: int) -> int:
    try:
        n = int(await _get(bot_id, "refpoint_per_join", "1"))
    except (TypeError, ValueError):
        n = 1
    return max(0, n)


# === PATCH_REFPOINT_CONVERT_BTN_V1 ===
async def convert_cost(bot_id: int) -> int:
    """هزینهٔ هر بارِ «تبدیل امتیاز» (۰ = دکمهٔ تبدیل خاموش)."""
    try:
        return max(0, int(await _get(bot_id, "refpoint_convert_cost", "0")))
    except (TypeError, ValueError):
        return 0


async def reward_summary(bot_id: int) -> str:
    """توضیحِ کوتاهِ جایزه برایِ نمایش به کاربر (مثلاً «۵ گیگ»)."""
    kind = await _get(bot_id, "refpoint_reward_kind", "stock")
    try:
        vol = int(await _get(bot_id, "refpoint_reward_vol", "0"))
        days = int(await _get(bot_id, "refpoint_reward_days", "0"))
    except (TypeError, ValueError):
        vol, days = 0, 0
    if kind in ("stock", "auto"):
        return f"{_fmt_vol(vol)}" + (f" / {days} روز" if days else "")
    if kind == "stars":
        return f"{await _get(bot_id, 'refpoint_reward_stars', '0')} استارز"
    return REWARD_LABELS.get(kind, "جایزه").split(" ", 1)[-1]


# ═══════════════════════════════════════════
#  کیفِ امتیاز
# ═══════════════════════════════════════════

async def get_points(bot_id: int, user_id: int) -> int:
    async with Session() as s:
        w = await s.scalar(select(RefPointWallet).where(
            RefPointWallet.bot_id == bot_id, RefPointWallet.user_id == user_id))
        return int(w.points or 0) if w else 0


async def award_points(bot_id: int, user_id: int, n: int | None = None) -> int:
    """افزودنِ امتیاز به کیفِ دعوت‌کننده؛ امتیازِ نهایی را برمی‌گرداند.

    اگر قابلیت خاموش باشد یا امتیازِ هر رفرال صفر باشد، هیچ کاری نمی‌کند.
    """
    if not await points_enabled(bot_id):
        return 0
    amount = await per_join(bot_id) if n is None else int(n)
    if amount <= 0:
        return 0
    async with Session() as s:
        w = await s.scalar(select(RefPointWallet).where(
            RefPointWallet.bot_id == bot_id,
            RefPointWallet.user_id == user_id).with_for_update())
        if not w:
            w = RefPointWallet(bot_id=bot_id, user_id=user_id,
                               points=0, total_earned=0)
            s.add(w)
            try:
                await s.flush()
            except IntegrityError:
                await s.rollback()
                return await award_points(bot_id, user_id, amount)
        w.points = int(w.points or 0) + amount
        w.total_earned = int(w.total_earned or 0) + amount
        await s.commit()
        return int(w.points)


# === PATCH_REFPOINT_MANUAL_ADD_V1 ===
async def admin_adjust_points(bot_id: int, user_id: int, delta: int) -> int:
    """افزودن/کسرِ دستیِ امتیاز توسطِ ادمین — برخلافِ award_points، به
    روشن‌بودنِ «امتیازگیری با رفرال» یا امتیازِ هر رفرال وابسته نیست، چون
    این یه اقدامِ صریحِ ادمینه، نه پاداشِ خودکارِ یه رفرال. اگه delta منفی
    باشه، هیچ‌وقت امتیاز رو منفی نمی‌کنه (حداکثر تا صفر کم می‌شه).
    امتیازِ نهایی رو برمی‌گردونه."""
    async with Session() as s:
        w = await s.scalar(select(RefPointWallet).where(
            RefPointWallet.bot_id == bot_id,
            RefPointWallet.user_id == user_id).with_for_update())
        if not w:
            w = RefPointWallet(bot_id=bot_id, user_id=user_id, points=0, total_earned=0)
            s.add(w)
            try:
                await s.flush()
            except IntegrityError:
                await s.rollback()
                return await admin_adjust_points(bot_id, user_id, delta)
        new_points = max(0, int(w.points or 0) + int(delta))
        w.points = new_points
        if delta > 0:
            w.total_earned = int(w.total_earned or 0) + int(delta)
        await s.commit()
        return int(w.points)


async def spend_points(bot_id: int, user_id: int, cost: int) -> bool:
    """کسرِ اتمیکِ امتیاز؛ False یعنی امتیاز کافی نبود (یا هم‌زمان مصرف شد)."""
    if cost <= 0:
        return True
    async with Session() as s:
        res = await s.execute(
            update(RefPointWallet)
            .where(RefPointWallet.bot_id == bot_id,
                   RefPointWallet.user_id == user_id,
                   RefPointWallet.points >= cost)
            .values(points=RefPointWallet.points - cost))
        await s.commit()
        return res.rowcount > 0


# ═══════════════════════════════════════════
#  تحویلِ جایزه
# ═══════════════════════════════════════════

async def _deliver_single(bot: Bot, bot_id: int, u: BotUser, msg: Message) -> bool:
    """یک جایزه را (طبقِ تنظیماتِ جاری/override) به کاربر می‌دهد. False یعنی تحویل نشد."""
    kind = await _get(bot_id, "refpoint_reward_kind", "stock")
    # === PATCH_REFPOINT_PRIZES_V1 ===
    if kind == "gift":
        return await _deliver_gift(bot, bot_id, u, msg)
    if kind == "stars":
        return await _deliver_stars(bot, bot_id, u, msg)
    if kind == "text":
        text = await _get(bot_id, "refpoint_reward_text", "")
        await msg.answer("🎁 جایزه‌ات:\n\n" + (text or "—"))
        return True
    try:
        vol = int(await _get(bot_id, "refpoint_reward_vol", "0"))
        days = int(await _get(bot_id, "refpoint_reward_days", "0"))
    except (TypeError, ValueError):
        vol, days = 0, 0

    if kind == "stock":
        # === PATCH_REFPOINT_PANEL_KIND_PICKER_V1 ===
        _cfgkind = await _get(bot_id, "refpoint_reward_cfgkind", "all")
        async with Session() as s:
            # === PATCH_CONFIG_STOCK_ROW_LOCK_V1 ===
            _cfg_q = select(Config).where(
                Config.bot_id == bot_id, Config.status == "in_stock",
                Config.volume_gb == vol, Config.days == days,
            )
            if _cfgkind != "all":
                _cfg_q = _cfg_q.where(Config.kind == _cfgkind)
            cfg = await s.scalar(_cfg_q.with_for_update().order_by(Config.id).limit(1))
            if not cfg:
                await msg.answer("⛔️ جایزه موقتاً ناموجود است؛ کمی بعد دوباره امتحان کن.")
                return False
            cfg.status = "sold"
            cfg.owner_user_id = u.id
            payload = cfg.payload
            await s.commit()
        await msg.answer(
            f"🎁 جایزه‌ات آماده شد!\n📦 {_fmt_vol(vol)} / 📅 {days} روز\n\n<code>{payload}</code>")
        return True

    # === PATCH_REFPOINT_PRODUCT_REWARD_V1 ===
    if kind == "product":
        return await _deliver_product(bot, bot_id, u, msg)

    # === PATCH_REFPOINT_SMM_REWARD_V1 ===
    if kind == "smm":
        return await _deliver_smm(bot, bot_id, u, msg)

    # kind == "auto" — ساختِ کانفیگ روی پنلِ متصل
    return await _deliver_auto(bot, bot_id, u, msg, vol, days)


# ═══════════════════════════════════════════
#  گیفت/استارز + فهرستِ جایزه‌ها (کاربر یکی را انتخاب می‌کند) — PATCH_REFPOINT_PRIZES_V1
# ═══════════════════════════════════════════
_POOL_KEY = "refpoint_prizes"          # JSON: [{"label": str, "cfg": {refpoint_reward_*: value}}]
_CFG_PREFIX = "refpoint_reward_"
MAX_PRIZES = 8


async def pool_get(bot_id: int) -> list[dict]:
    raw = await _get(bot_id, _POOL_KEY, "")
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("cfg"), dict)][:MAX_PRIZES]


async def _pool_set(bot_id: int, pool: list[dict]) -> None:
    await _set(bot_id, _POOL_KEY, json.dumps(pool, ensure_ascii=False))


async def snapshot_current_cfg(bot_id: int) -> dict:
    """همهٔ تنظیماتِ refpoint_reward_* فعلی (عکسِ لحظه‌ای)."""
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key.like(_CFG_PREFIX + "%")))).all()
    return {r.key: r.value for r in rows}


def entry_title(entry: dict) -> str:
    cfg = entry.get("cfg") or {}
    kind = cfg.get("refpoint_reward_kind", "stock")
    return (entry.get("label") or REWARD_LABELS.get(kind, kind))[:60]


async def _admin_targets(bot_id: int) -> list[int]:
    from models import AdminSupport, Bot as _B
    async with Session() as s:
        b = await s.get(_B, bot_id)
        adm = (await s.scalars(select(AdminSupport).where(AdminSupport.bot_id == bot_id))).all()
    out = [b.owner_id] if b else []
    return list({*out, *[a.telegram_id for a in adm]})


async def _notify_admins_prize(bot: Bot, bot_id: int, claim_id: int, u: BotUser, label: str, note: str = "") -> None:
    who = f"@{u.username}" if u.username else (u.full_name or str(u.telegram_id))
    text = (f"🏅 <b>جایزهٔ امتیازِ رفرال — ارسالِ دستی</b>\n\n👤 {_esc_html(str(who))} (<code>{u.telegram_id}</code>)\n"
            f"🎁 {_esc_html(label)}\n"
            + (f"ℹ️ {_esc_html(note)}\n" if note else "")
            + "\nبعد از ارسال «✅ ارسال شد» را بزن تا به کاربر خبر برسد.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="✅ ارسال شد", callback_data=f"rpg:done:{claim_id}", style=ButtonStyle.SUCCESS)]])
    for tid in await _admin_targets(bot_id):
        try:
            await bot.send_message(tid, text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            logger.info("refpoint prize: اطلاع به ادمین %s نرسید", tid)


async def _manual_claim(bot: Bot, bot_id: int, u: BotUser, msg: Message, kind: str, label: str,
                        note: str = "") -> bool:
    """جایزه به‌صورتِ دستی: ثبت + اطلاع به ادمین‌ها؛ به کاربر گفته می‌شود به‌زودی ارسال می‌شود."""
    async with Session() as s:
        c = RefPrizeClaim(bot_id=bot_id, user_id=u.id, kind=kind, label=label[:160], status="pending",
                          note=(note or None))
        s.add(c)
        await s.commit()
        cid = c.id
    await msg.answer(f"🎁 جایزه‌ات ثبت شد: <b>{_esc_html(label)}</b>\n"
                     "ادمین به‌زودی برایت ارسال می‌کند و همین‌جا خبر می‌دهیم ✅", parse_mode="HTML")
    await _notify_admins_prize(bot, bot_id, cid, u, label, note)
    return True


async def _deliver_gift(bot: Bot, bot_id: int, u: BotUser, msg: Message) -> bool:
    gid = (await _get(bot_id, "refpoint_reward_gift_id", "")).strip()
    stars = await _get(bot_id, "refpoint_reward_gift_stars", "")
    emoji = await _get(bot_id, "refpoint_reward_gift_emoji", "🎁")
    if not gid:
        await msg.answer("⛔️ هنوز گیفتِ جایزه انتخاب نشده؛ به مالکِ ربات اطلاع بده.")
        return False
    label = f"{emoji} گیفتِ تلگرامی" + (f" ({stars}⭐)" if stars else "")
    if (await _get(bot_id, "refpoint_reward_delivery", "manual")) == "auto":
        try:
            await bot.send_gift(user_id=u.telegram_id, gift_id=gid)
        except Exception as e:                               # noqa: BLE001
            logger.warning("refpoint gift: ارسالِ خودکار نشد (%s): %s", type(e).__name__, e)
            return await _manual_claim(bot, bot_id, u, msg, "gift", label,
                                       note=f"ارسالِ خودکار نشد ({type(e).__name__}) — دستی بفرست")
        async with Session() as s:
            s.add(RefPrizeClaim(bot_id=bot_id, user_id=u.id, kind="gift", label=label[:160], status="done",
                                decided_at=_dtm.utcnow(), note="ارسالِ خودکار"))
            await s.commit()
        await msg.answer(f"🎁 گیفتت ارسال شد! {emoji}")
        return True
    return await _manual_claim(bot, bot_id, u, msg, "gift", label)


async def _deliver_stars(bot: Bot, bot_id: int, u: BotUser, msg: Message) -> bool:
    n = (await _get(bot_id, "refpoint_reward_stars", "0")).strip()
    if not n.isdigit() or int(n) <= 0:
        await msg.answer("⛔️ تعدادِ استارزِ جایزه هنوز تنظیم نشده؛ به مالکِ ربات اطلاع بده.")
        return False
    return await _manual_claim(bot, bot_id, u, msg, "stars", f"⭐ {int(n):,} استارز")


async def deliver_reward(bot: Bot, bot_id: int, u: BotUser, msg: Message) -> bool:
    """جایزه را تحویل می‌دهد. اگر «فهرستِ جایزه‌ها» تنظیم شده باشد: با یک جایزه مستقیم، با چند جایزه
    کاربر یکی را انتخاب می‌کند؛ وگرنه جایزهٔ تکیِ قبلی. False یعنی تحویل نشد."""
    pool = await pool_get(bot_id)
    if not pool:
        return await _deliver_single(bot, bot_id, u, msg)
    if len(pool) == 1:
        tok = _CFG_OVERRIDE.set(pool[0]["cfg"])
        try:
            return await _deliver_single(bot, bot_id, u, msg)
        finally:
            _CFG_OVERRIDE.reset(tok)
    async with Session() as s:
        c = RefPrizeClaim(bot_id=bot_id, user_id=u.id, kind="choice", label="انتخابِ جایزه", status="choose")
        s.add(c)
        await s.commit()
        cid = c.id
    rows = [[InlineKeyboardButton(text=f"🎁 {entry_title(e)}", callback_data=f"rpz:pick:{cid}:{i}",
                                  style=ButtonStyle.SUCCESS)] for i, e in enumerate(pool)]
    await msg.answer("🎉 <b>قفلِ جایزه باز شد!</b>\n\nیکی از جایزه‌هایِ زیر را انتخاب کن "
                     "(فقط یکی قابلِ دریافت است):",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    return True


@rpt_router.callback_query(F.data.startswith("rpz:pick:"))
async def rpz_pick(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    try:
        _, _, cid_s, idx_s = cb.data.split(":")
        cid, idx = int(cid_s), int(idx_s)
    except ValueError:
        return await cb.answer()
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        c = await s.get(RefPrizeClaim, cid)
    if not u or not c or c.bot_id != row.id or c.user_id != u.id:
        return await cb.answer("این جایزه مالِ تو نیست.", show_alert=True)
    pool = await pool_get(row.id)
    if not (0 <= idx < len(pool)):
        return await cb.answer("این گزینه دیگر موجود نیست.", show_alert=True)
    # قفلِ اتمیک: فقط یک انتخاب
    async with Session() as s:
        res = await s.execute(update(RefPrizeClaim).where(
            RefPrizeClaim.id == cid, RefPrizeClaim.status == "choose").values(status="pending",
                                                                              label=entry_title(pool[idx])[:160]))
        await s.commit()
    if not res.rowcount:
        return await cb.answer("جایزه‌ات را قبلاً انتخاب کرده‌ای ✅", show_alert=True)
    await cb.answer("⏳ در حالِ آماده‌سازی…")
    tok = _CFG_OVERRIDE.set(pool[idx]["cfg"])
    try:
        ok = await _deliver_single(bot, row.id, u, cb.message)
    except Exception:
        logger.exception("refpoint prize: تحویلِ جایزهٔ انتخابی ناموفق بود")
        ok = False
    finally:
        _CFG_OVERRIDE.reset(tok)
    async with Session() as s:
        await s.execute(update(RefPrizeClaim).where(RefPrizeClaim.id == cid).values(
            status=("done" if ok else "choose"), decided_at=(_dtm.utcnow() if ok else None)))
        await s.commit()
    if ok:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.info("refpoint prize: پاک‌کردنِ دکمه‌ها ممکن نشد")
    else:
        await cb.message.answer("⚠️ این جایزه الان تحویل نشد؛ می‌توانی دوباره امتحان کنی یا جایزهٔ دیگری بزنی.")


@rpt_router.callback_query(F.data.startswith("rpg:done:"))
async def rpg_done(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        res = await s.execute(update(RefPrizeClaim).where(
            RefPrizeClaim.id == cid, RefPrizeClaim.bot_id == row.id, RefPrizeClaim.status == "pending"
        ).values(status="done", decided_at=_dtm.utcnow()))
        await s.commit()
        c = await s.get(RefPrizeClaim, cid)
        u = await s.get(BotUser, c.user_id) if c else None
    if not res.rowcount:
        return await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(f"✅ ثبت شد — کاربر مطلع شد. (جایزه: {_esc_html(c.label or '')})")
    except Exception:
        logger.info("refpoint prize: ویرایشِ پیامِ ادمین ممکن نشد")
    if u:
        try:
            await bot.send_message(u.telegram_id, f"🎁 جایزه‌ات ارسال شد ✅\n{_esc_html(c.label or '')}", parse_mode="HTML")
        except Exception:
            logger.info("refpoint prize: اطلاع به کاربر نرسید")
    await cb.answer("ثبت شد")


async def _deliver_product(bot: Bot, bot_id: int, u: BotUser, msg: Message) -> bool:
    """جایزه = یکی از محصولاتِ فروشگاهِ دیجیتالِ همین ربات (tenant/shop.py)
    — رایگان، بدونِ کم‌شدن از کیف‌پول. اگر فایل دارد فایل، وگرنه توضیحات
    فرستاده می‌شود؛ اگر موجودیِ محدود دارد یکی کم می‌شود."""
    from models import Product
    pid_s = await _get(bot_id, "refpoint_reward_product_id", "")
    if not pid_s.isdigit():
        await msg.answer("⛔️ هنوز محصولی برایِ جایزه انتخاب نشده؛ به مالکِ ربات اطلاع بده.")
        return False
    async with Session() as s:
        p = await s.scalar(select(Product).where(
            Product.id == int(pid_s)).with_for_update())
        if not p or p.bot_id != bot_id or not p.is_active:
            await msg.answer("⛔️ محصولِ جایزه دیگر در دسترس نیست؛ به مالکِ ربات اطلاع بده.")
            return False
        if p.stock == 0:
            await msg.answer("⛔️ جایزه موقتاً ناموجود است (موجودیِ محصول تمام شده)؛ کمی بعد دوباره امتحان کن.")
            return False
        if p.stock > 0:
            p.stock -= 1
        title, desc, file_id, photo_id = p.title, p.description, p.file_id, p.photo_id
        await s.commit()

    caption = f"🎁 جایزه‌ات: <b>{title}</b>"
    if desc:
        caption += f"\n\n{desc}"
    try:
        if file_id:
            await bot.send_document(msg.chat.id, file_id, caption=caption, parse_mode="HTML")
        elif photo_id:
            await bot.send_photo(msg.chat.id, photo_id, caption=caption, parse_mode="HTML")
        else:
            await msg.answer(caption, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (ارسالِ فایلِ جایزهٔ محصول)")
        await msg.answer(caption, parse_mode="HTML")
    return True


async def _reward_account(bot_id: int, user_id: int):
    """اکانتِ جایزهٔ قبلیِ این کاربر (اگر هنوز معتبر است) — برای شارژ. === PATCH_REFPOINT_REWARD_TOPUP_V1 ==="""
    from models import PanelAccount
    async with Session() as s:
        w = await s.scalar(select(RefPointWallet).where(
            RefPointWallet.bot_id == bot_id, RefPointWallet.user_id == user_id))
        aid = getattr(w, "reward_account_id", None) if w else None
        if not aid:
            return None
        acc = await s.get(PanelAccount, aid)
        if not acc or acc.bot_id != bot_id or acc.status != "active":
            return None
        return acc


async def _set_reward_account(bot_id: int, user_id: int, aid: int | None) -> None:
    async with Session() as s:
        w = await s.scalar(select(RefPointWallet).where(
            RefPointWallet.bot_id == bot_id, RefPointWallet.user_id == user_id))
        if w:
            w.reward_account_id = aid
            await s.commit()


async def _deliver_auto(bot: Bot, bot_id: int, u: BotUser, msg: Message,
                        vol: int, days: int) -> bool:
    from models import Bot as BotRow, PanelAccount, VpnPanel
    from tenant.shared.panel_clients.vpn_panel import PanelError, gen_sub_id, gen_uuid
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
    from tenant.bots.vpn.vpn_user import (
        _make_config_email, _send_config_delivery, new_svc_code as _new_svc_code,
    )

    # === PATCH_REFPOINT_REWARD_TOPUP_V1 ===
    # بارِ دوم به بعد: همان کانفیگِ قبلیِ کاربر شارژ می‌شود (نه کانفیگِ تازه)،
    # تا کاربر با هر دورِ امتیاز، حجمِ همان سرویس را بالا ببرد.
    prev = await _reward_account(bot_id, u.id)
    if prev is not None:
        return await _topup_reward(bot, bot_id, u, msg, prev, vol, days)

    # === PATCH_REFPOINT_PANEL_KIND_PICKER_V1 ===
    _sel_panel_id_s = await _get(bot_id, "refpoint_reward_panel_id", "")
    async with Session() as s:
        panel = None
        if _sel_panel_id_s.isdigit():
            _cand = await s.get(VpnPanel, int(_sel_panel_id_s))
            if _cand and _cand.bot_id == bot_id and _cand.enabled:
                panel = _cand
        if panel is None:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))  # noqa: E712
        bot_row = await s.get(BotRow, bot_id)
        panel_key = panel.panel_key if panel else None
    if not panel:
        await msg.answer("⛔️ پنلِ خودکار تنظیم نشده؛ با پشتیبانی تماس بگیر.")
        return False
    client = make_panel_client(panel)
    try:
        email = await _make_config_email(
            bot_id, panel_key, bot_row.username if bot_row else None,
            u.telegram_id, msg.from_user.username if msg.from_user else None)
        acc = await client.create_account(
            email, gb=vol, days=days, limit_ip=0,
            uuid_str=gen_uuid(), sub_id=gen_sub_id())
        async with Session() as s:
            new_acc = PanelAccount(
                bot_id=bot_id, user_telegram_id=u.telegram_id,
                email=email, panel_key=panel_key,
                uuid=acc.get("uuid"), sub_id=acc.get("sub_id"),
                sub_link=acc.get("sub_link") or "", volume_gb=vol, days=days,
                plan_title="جایزهٔ امتیازِ رفرال", status="active",
                svc_code=await _new_svc_code(s, bot_id), first_conn_notified=False)
            s.add(new_acc)
            await s.commit()
            aid = new_acc.id
        await _set_reward_account(bot_id, u.id, aid)  # PATCH_REFPOINT_REWARD_TOPUP_V1
        await _send_config_delivery(bot, u.telegram_id, aid)
        return True
    except PanelError as e:
        await msg.answer(f"❌ ساختِ کانفیگِ جایزه ناموفق بود.\n<code>{_esc_html(str(e))}</code>")
        return False
    except Exception as e:
        logger.exception("خطا در ساختِ کانفیگِ جایزهٔ امتیازِ رفرال")
        await msg.answer(f"❌ خطای غیرمنتظره در ساختِ جایزه.\n<code>{type(e).__name__}: {_esc_html(str(e))}</code>")
        return False
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده هنگام بستنِ کلاینتِ پنل")


# ═══════════════════════════════════════════
#  پنلِ مالکِ ربات
# ═══════════════════════════════════════════

class RptFSM(StatesGroup):
    convert_cost = State()   # === PATCH_REFPOINT_CONVERT_BTN_V1 ===
    smm_qty = State()   # === PATCH_REFPOINT_SMM_REWARD_V1 ===
    per_join = State()
    vol = State()
    days = State()
    text = State()
    # === PATCH_REFPOINT_MANUAL_ADD_V1 ===
    manual_user = State()
    manual_amount = State()
    # === PATCH_REFPOINT_PRIZES_V1 ===
    stars_amt = State()
    pool_label = State()


def _fa_to_int(text: str) -> int | None:
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    digits = (text or "").translate(trans).replace(",", "").replace("،", "").strip()
    return int(digits) if digits.isdigit() else None


async def _owner_ok(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return None
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return None
    return row


async def _panel_text(bot_id: int) -> str:
    on = await points_enabled(bot_id)
    n = await per_join(bot_id)
    kind = await _get(bot_id, "refpoint_reward_kind", "stock")
    vol = await _get(bot_id, "refpoint_reward_vol", "0")
    days = await _get(bot_id, "refpoint_reward_days", "0")
    lines = [
        "🏅 <b>امتیازگیری با رفرال</b>\n",
        f"وضعیت: {'🟢 فعال' if on else '🔴 غیرفعال'}",
        f"امتیازِ هر رفرال: <b>{n}</b>",
        (f"🔁 تبدیلِ امتیاز: هر <b>{await convert_cost(bot_id)}</b> امتیاز = یک جایزه"
         if await convert_cost(bot_id) > 0 else "🔁 تبدیلِ امتیاز: <i>خاموش (با «🔁 هزینهٔ تبدیل» روشنش کن)</i>"),
        f"نوعِ جایزه: {REWARD_LABELS.get(kind, kind)}",
    ]
    if kind in ("stock", "auto"):
        lines.append(f"بستهٔ جایزه: <b>{vol}</b> گیگ / <b>{days}</b> روز")
        if (vol, days) == ("0", "0"):
            lines.append("⚠️ حجم/مدتِ جایزه هنوز تنظیم نشده؛ از «📐 حجم و مدتِ جایزه» مشخصش کن.")
    if kind == "auto":
        lines.append("🔁 بارِ اول کانفیگ ساخته می‌شود؛ دفعاتِ بعد همان کانفیگ شارژ می‌شود.")
        # === PATCH_REFPOINT_PANEL_KIND_PICKER_V1 ===
        _panel_id_s = await _get(bot_id, "refpoint_reward_panel_id", "")
        if _panel_id_s.isdigit():
            from models import VpnPanel as _VP
            from tenant.bots.vpn.vpn_admin import panel_label as _plabel
            async with Session() as _s_p:
                _p = await _s_p.get(_VP, int(_panel_id_s))
            if _p and _p.bot_id == bot_id and _p.enabled:
                lines.append(f"🔌 پنلِ انتخاب‌شده: <b>{await _plabel(bot_id, _p.panel_key)}</b>")
            else:
                lines.append("🔌 پنل: پیش‌فرض (اولین پنلِ فعال)")
        else:
            lines.append("🔌 پنل: پیش‌فرض (اولین پنلِ فعال) — برایِ تعیینِ دستی از «🔌 انتخابِ پنل» استفاده کن.")
    elif kind == "stock":
        lines.append("🔁 هر بار یک کانفیگِ جداگانه از انبار تحویل می‌شود.")
        # === PATCH_REFPOINT_PANEL_KIND_PICKER_V1 ===
        _cfgkind = await _get(bot_id, "refpoint_reward_cfgkind", "all")
        lines.append(f"📶 نوعِ کانفیگ: <b>{CFG_KIND_LABELS.get(_cfgkind, _cfgkind)}</b>")
    # === PATCH_REFPOINT_SMM_REWARD_V1 ===
    elif kind == "smm":
        from models import SmmService as _Svc
        sid = await _get(bot_id, "refpoint_reward_smm_id", "")
        qty = await _get(bot_id, "refpoint_reward_smm_qty", "0")
        name = "—"
        if sid.isdigit():
            async with Session() as s:
                v = await s.get(_Svc, int(sid))
            name = v.name if v else "—"
        lines.append(f"📣 سرویسِ جایزه: <b>{name}</b> — تعداد: <b>{qty}</b>")
        if name == "—" or qty in ("", "0"):
            lines.append("⚠️ سرویس/تعدادِ جایزه هنوز تنظیم نشده؛ از «📣 انتخابِ سرویس و تعداد» مشخصش کن.")
        else:
            lines.append("🔗 موقعِ دریافتِ جایزه، از کاربر لینکِ هدف پرسیده می‌شود و سفارش رایگان ثبت می‌شود.")
    # === PATCH_REFPOINT_PRODUCT_REWARD_V1 ===
    elif kind == "product":
        from models import Product
        pid = await _get(bot_id, "refpoint_reward_product_id", "")
        async with Session() as s:
            p = await s.get(Product, int(pid)) if pid.isdigit() else None
        if p and p.bot_id == bot_id:
            stock_txt = "نامحدود" if p.stock < 0 else f"{p.stock:,} عدد"
            lines.append(f"محصولِ انتخاب‌شده: <b>{p.title}</b> (موجودی: {stock_txt})")
        else:
            lines.append("⚠️ هنوز محصولی برایِ جایزه انتخاب نشده؛ از «🛍 انتخابِ محصول» مشخصش کن.")
    # === PATCH_REFPOINT_PRIZES_V1 ===
    elif kind == "gift":
        _gid = await _get(bot_id, "refpoint_reward_gift_id", "")
        _gst = await _get(bot_id, "refpoint_reward_gift_stars", "")
        _gem = await _get(bot_id, "refpoint_reward_gift_emoji", "🎁")
        _dl = await _get(bot_id, "refpoint_reward_delivery", "manual")
        lines.append(f"گیفتِ انتخاب‌شده: <b>{_gem} {_gst + '⭐' if _gst else ('—' if not _gid else _gid)}</b>")
        lines.append("🚚 ارسال: <b>" + ("خودکار (خودِ ربات از استارزِ خودش می‌فرستد؛ اگر نشد برایِ تو پیام می‌آید)"
                                       if _dl == "auto" else "دستی (برایِ تو پیام می‌آید و خودت می‌فرستی)") + "</b>")
        if not _gid:
            lines.append("⚠️ هنوز گیفتی انتخاب نشده؛ از «🎁 انتخابِ گیفت» مشخصش کن.")
    elif kind == "stars":
        _sn = await _get(bot_id, "refpoint_reward_stars", "0")
        lines.append(f"تعدادِ استارز: <b>{_sn}</b> — ارسال: دستی (برایِ تو پیام می‌آید و خودت می‌فرستی)")
        if _sn in ("", "0"):
            lines.append("⚠️ تعدادِ استارز هنوز تنظیم نشده؛ از «⭐ تعدادِ استارز» مشخصش کن.")
    _pool = await pool_get(bot_id)
    if _pool:
        lines.append(f"\n🧺 <b>فهرستِ جایزه‌ها ({len(_pool)}):</b> " + "، ".join(entry_title(e) for e in _pool)
                     + "\n(وقتی قفل باز شود، کاربر یکی از اینها را انتخاب می‌کند؛ تنظیماتِ بالا برایِ ساختنِ جایزهٔ تازه است.)")
    lines.append(
        "\n💡 هر زیرمجموعه‌ای که کاربر بیاورد امتیاز می‌گیرد و امتیازها جمع می‌شوند. "
        "بعد در «🏠 مدیریت دکمه‌های صفحه اصلی → قوانین و دسترسی و تستِ این دکمه → "
        "🔒 گیتِ دسترسی با زیرمجموعه» ملاکِ شمارش را روی «🏅 امتیازِ رفرال» بگذار؛ "
        "هر استفاده به‌اندازهٔ آستانه از امتیازِ کاربر کم می‌شود "
        "(آستانهٔ ۳ ← ۳ امتیاز یک‌بار، ۶ امتیاز دوبار، …).")
    return "\n".join(lines)


async def _panel_kb(bot_id: int, on: bool, kind: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=("🔴 غیرفعال کن" if on else "🟢 فعال کن"),
            callback_data="rpt:toggle",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔢 امتیازِ هر رفرال",
                              callback_data="rpt:perjoin", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔁 هزینهٔ تبدیلِ امتیاز (دکمهٔ «تبدیل امتیاز»)",
                              callback_data="rpt:cost", style=ButtonStyle.PRIMARY)],
        # === PATCH_REF_BANNER_V1 ===
        [InlineKeyboardButton(text="✏️ متنِ صفحهٔ زیرمجموعه‌گیری",
                              callback_data="refrw:pagetext", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🖼 بنرِ دعوت (عکس + متن)",
                              callback_data="refrw:banner", style=ButtonStyle.PRIMARY)],
        # === PATCH_REFPOINT_MANUAL_ADD_V1 ===
        [InlineKeyboardButton(text="➕ افزودنِ دستیِ امتیاز به یه کاربر",
                              callback_data="rpt:manual", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📝 جایزه: متنِ ثابت",
                              callback_data="rpt:kind:text", style=ButtonStyle.PRIMARY)],
        # === PATCH_REFPOINT_PRIZES_V1 ===
        [InlineKeyboardButton(text="🎁 جایزه: گیفتِ تلگرامی", callback_data="rpt:kind:gift", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="⭐ جایزه: استارز", callback_data="rpt:kind:stars", style=ButtonStyle.PRIMARY)],
    ]
    # === PATCH_REFPOINT_SMM_REWARD_V1 ===
    from models import Bot as _BotRow
    async with Session() as s0:
        _brow = await s0.get(_BotRow, bot_id)
    _is_virtual = bool(_brow and _brow.field == "virtual_service")
    if not _is_virtual:
        rows[2:2] = [
            [InlineKeyboardButton(text="📦 جایزه: کانفیگِ آمادهٔ انبار",
                                  callback_data="rpt:kind:stock", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="⚙️ جایزه: کانفیگِ خودکار",
                                  callback_data="rpt:kind:auto", style=ButtonStyle.PRIMARY)],
        ]
    else:
        from models import SmmService as _Svc
        async with Session() as s0:
            _has_svc = bool(await s0.scalar(select(_Svc.id).where(
                _Svc.bot_id == bot_id, _Svc.is_active == True).limit(1)))  # noqa: E712
        if _has_svc:
            rows.insert(2, [InlineKeyboardButton(text="📣 جایزه: سرویسِ خدماتِ مجازی",
                                                 callback_data="rpt:kind:smm", style=ButtonStyle.PRIMARY)])
    # === PATCH_REFPOINT_PRODUCT_REWARD_V1 ===
    # درخواستِ ادمین: برایِ ربات‌هایی که چیزی تویِ فروشگاهِ دیجیتالشون
    # (tenant/shop.py) برایِ فروش گذاشته‌اند، جایزه می‌تواند یکی از همون
    # محصولات باشد — نه فقط کانفیگِ وی‌پی‌ان یا یک متنِ ثابت. این گزینه
    # فقط وقتی نشان داده می‌شود که واقعاً حداقل یک محصولِ فعال داشته
    # باشند، وگرنه یک دکمهٔ بی‌فایده و گیج‌کننده می‌شد.
    from models import Product
    async with Session() as s:
        has_products = bool(await s.scalar(select(Product.id).where(
            Product.bot_id == bot_id, Product.is_active == True).limit(1)))  # noqa: E712
    if has_products:
        rows.append([InlineKeyboardButton(text="🛍 جایزه: محصولِ فروشگاه",
                                          callback_data="rpt:kind:product", style=ButtonStyle.PRIMARY)])
    if kind in ("stock", "auto"):
        rows.append([InlineKeyboardButton(text="📐 حجم و مدتِ جایزه",
                                          callback_data="rpt:pack", style=ButtonStyle.PRIMARY)])
        # === PATCH_REFPOINT_PANEL_KIND_PICKER_V1 ===
        if kind == "auto":
            rows.append([InlineKeyboardButton(text="🔌 انتخابِ پنل",
                                              callback_data="rpt:pickpanel", style=ButtonStyle.PRIMARY)])
        elif kind == "stock":
            rows.append([InlineKeyboardButton(text="📶 نوعِ کانفیگ",
                                              callback_data="rpt:pickcfgkind", style=ButtonStyle.PRIMARY)])
    elif kind == "product":
        rows.append([InlineKeyboardButton(text="🛍 انتخابِ محصول",
                                          callback_data="rpt:pickproduct:0", style=ButtonStyle.PRIMARY)])
    # === PATCH_REFPOINT_PRIZES_V1 ===
    elif kind == "gift":
        _dl = await _get(bot_id, "refpoint_reward_delivery", "manual")
        rows.append([InlineKeyboardButton(text="🎁 انتخابِ گیفت", callback_data="rpt:pickgift:0",
                                          style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text=("🚚 ارسال: ⚡️ خودکار (بزن تا دستی شود)" if _dl == "auto"
                                                else "🚚 ارسال: ✋ دستی (بزن تا خودکار شود)"),
                                          callback_data="rpt:delivery", style=ButtonStyle.PRIMARY)])
    elif kind == "stars":
        rows.append([InlineKeyboardButton(text="⭐ تعدادِ استارز", callback_data="rpt:starsamt",
                                          style=ButtonStyle.PRIMARY)])
    # === PATCH_REFPOINT_SMM_REWARD_V1 ===
    elif kind == "smm":
        rows.append([InlineKeyboardButton(text="📣 انتخابِ سرویس و تعداد",
                                          callback_data="rpt:picksmm:0", style=ButtonStyle.PRIMARY)])
    else:
        rows.append([InlineKeyboardButton(text="✏️ متنِ جایزه",
                                          callback_data="rpt:rtext", style=ButtonStyle.PRIMARY)])
    # === PATCH_REFPOINT_PRIZES_V1 ===
    _np = len(await pool_get(bot_id))
    rows.append([InlineKeyboardButton(text=f"🧺 فهرستِ جایزه‌ها ({_np}) — کاربر یکی را انتخاب می‌کند",
                                      callback_data="rpt:pool", style=ButtonStyle.SUCCESS)])
    # باگ: «tadm:settings» همیشه منویِ شیشه‌ای می‌ساخت، پس در حالتِ
    # کیبوردی بازگشت، منو را اینلاین می‌کرد. «tadm:sec:settings» حالتِ
    # فعلیِ منو را رعایت می‌کند.
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت",
                                      callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_panel(msg: Message, bot_id: int) -> None:
    kind = await _get(bot_id, "refpoint_reward_kind", "stock")
    await msg.answer(await _panel_text(bot_id),
                     reply_markup=await _panel_kb(bot_id, await points_enabled(bot_id), kind),
                     parse_mode="HTML")


@rpt_router.callback_query(F.data == "rpt:menu")
async def rpt_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    if not has_feature(row.plan, Feature.REFERRAL_POINTS):
        await cb.answer("🔒 این قابلیت در پلن فعلی این ربات روشن نیست.", show_alert=True)
        return
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام پاک‌کردنِ کیبورد")
    await _show_panel(cb.message, row.id)
    await cb.answer()


@rpt_router.callback_query(F.data == "rpt:toggle")
async def rpt_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    new = "0" if await points_enabled(row.id) else "1"
    await _set(row.id, "refpoint_enabled", new)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام پاک‌کردنِ کیبورد")
    await _show_panel(cb.message, row.id)
    await cb.answer("ذخیره شد")


@rpt_router.callback_query(F.data.startswith("rpt:kind:"))
async def rpt_kind(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    kind = cb.data.split(":")[2]
    if kind not in REWARD_LABELS:
        await cb.answer("نامعتبر", show_alert=True)
        return
    await _set(row.id, "refpoint_reward_kind", kind)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام پاک‌کردنِ کیبورد")
    await _show_panel(cb.message, row.id)
    await cb.answer("نوعِ جایزه ذخیره شد")


# === PATCH_REFPOINT_PRODUCT_REWARD_V1 ===
@rpt_router.callback_query(F.data.startswith("rpt:pickproduct:"))
async def rpt_pick_product(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    from models import Product
    per_page = 10
    async with Session() as s:
        products = (await s.scalars(select(Product).where(
            Product.bot_id == row.id, Product.is_active == True  # noqa: E712
        ).order_by(Product.sort_order, Product.id))).all()
    if not products:
        await cb.answer("هنوز محصولِ فعالی در فروشگاه نداری.", show_alert=True)
        return
    start, end = page * per_page, page * per_page + per_page
    rows = []
    for p in products[start:end]:
        stock_txt = "∞" if p.stock < 0 else str(p.stock)
        rows.append([InlineKeyboardButton(
            text=f"{p.title[:40]} (موجودی: {stock_txt})",
            callback_data=f"rpt:setproduct:{p.id}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"rpt:pickproduct:{page-1}"))
    if end < len(products):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"rpt:pickproduct:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rpt:menu", style=ButtonStyle.DANGER)])
    text = "🛍 کدوم محصول جایزه باشه؟"
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@rpt_router.callback_query(F.data.startswith("rpt:setproduct:"))
async def rpt_set_product(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    from models import Product
    async with Session() as s:
        p = await s.get(Product, pid)
    if not p or p.bot_id != row.id:
        await cb.answer("این محصول دیگر موجود نیست.", show_alert=True)
        return
    await _set(row.id, "refpoint_reward_kind", "product")
    await _set(row.id, "refpoint_reward_product_id", str(pid))
    await cb.answer(f"✅ «{p.title}» به‌عنوانِ جایزه ثبت شد")
    await _show_panel(cb.message, row.id)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data="rpt:menu",
                             style=ButtonStyle.DANGER)]])


@rpt_router.callback_query(F.data == "rpt:perjoin")
async def rpt_perjoin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(RptFSM.per_join)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام پاک‌کردنِ کیبورد")
    await cb.message.answer(
        "🔢 هر رفرال چند امتیاز بدهد؟ یک عدد بفرست (مثلاً <code>1</code>):",
        reply_markup=_cancel_kb())
    await cb.answer()


@rpt_router.message(RptFSM.per_join, F.text)
async def rpt_set_perjoin(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    n = _fa_to_int(msg.text)
    if n is None or n <= 0 or n > 10000:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 1).")
        return
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    await _set(row.id, "refpoint_per_join", str(n))
    await state.clear()
    await msg.answer(f"✅ هر رفرال از این پس <b>{n}</b> امتیاز می‌دهد.")
    await _show_panel(msg, row.id)


@rpt_router.callback_query(F.data == "rpt:cost")
async def rpt_cost(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(RptFSM.convert_cost)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام پاک‌کردنِ کیبورد")
    await cb.message.answer(
        "🔁 هر بارِ «تبدیل امتیاز» چند امتیاز کم کند و جایزه بدهد؟\n"
        "مثلاً <code>5</code> → کاربری که ۵ امتیاز دارد با یک کلیک جایزه می‌گیرد "
        "(جایزه همان است که در همین بخش تنظیم کرده‌ای).\n"
        "برایِ خاموش‌کردنِ دکمه <code>0</code> بفرست:",
        reply_markup=_cancel_kb())
    await cb.answer()


@rpt_router.message(RptFSM.convert_cost, F.text)
async def rpt_set_cost(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    raw = (msg.text or "").strip()
    n = 0 if raw == "0" else _fa_to_int(raw)
    if n is None or n < 0 or n > 100000:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 5، یا 0 برایِ خاموش).")
        return
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    await _set(row.id, "refpoint_convert_cost", str(n))
    await state.clear()
    await msg.answer(f"✅ دکمهٔ «تبدیل امتیاز» روشن شد: هر <b>{n}</b> امتیاز = یک جایزه."
                     if n else "⛔️ دکمهٔ «تبدیل امتیاز» خاموش شد.")
    await _show_panel(msg, row.id)


@rpt_router.callback_query(F.data == "rpt:convert")
async def rpt_convert(cb: CallbackQuery, bot: Bot):
    """=== PATCH_REFPOINT_CONVERT_BTN_V1 === کاربر امتیازش را به جایزه تبدیل می‌کند."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await points_enabled(row.id):
        return await cb.answer("⛔️ امتیازگیری در این ربات فعال نیست.", show_alert=True)
    cost = await convert_cost(row.id)
    if cost <= 0:
        return await cb.answer("⛔️ تبدیلِ امتیاز در این ربات فعال نیست.", show_alert=True)
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
    if not u:
        return await cb.answer()
    have = await get_points(row.id, u.id)
    if have < cost:
        return await cb.answer(
            f"⚠️ برایِ تبدیل {cost} امتیاز لازم است.\n🏅 امتیازِ فعلیِ تو: {have}\n"
            "با دعوتِ دوستانت امتیاز بگیر.", show_alert=True)
    if not await spend_points(row.id, u.id, cost):
        return await cb.answer("⏳ لحظه‌ای بعد دوباره امتحان کن.", show_alert=True)
    await cb.answer("⏳ در حالِ آماده‌سازیِ جایزه…")
    try:
        ok = await deliver_reward(bot, row.id, u, cb.message)
    except Exception:
        logger.exception("تحویلِ جایزهٔ تبدیلِ امتیاز ناموفق بود")
        ok = False
    if not ok:
        await award_points(row.id, u.id, cost)   # امتیازِ کسرشده برمی‌گردد
        await cb.message.answer("⚠️ جایزه الان تحویل نشد؛ امتیازت برگشت. کمی بعد دوباره امتحان کن "
                                "یا به پشتیبانی بگو.")
        return
    left = await get_points(row.id, u.id)
    await cb.message.answer(f"🏅 {cost} امتیاز کسر شد. امتیازِ باقی‌مانده‌ات: <b>{left}</b>")


# === PATCH_REFPOINT_MANUAL_ADD_V1 ===
@rpt_router.callback_query(F.data == "rpt:manual")
async def rpt_manual_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(RptFSM.manual_user)
    await cb.answer()
    await cb.message.answer(
        "👤 آیدیِ عددیِ تلگرامِ کاربری که می‌خوای بهش امتیاز اضافه/کم کنی رو بفرست:",
        reply_markup=_cancel_kb())


@rpt_router.message(RptFSM.manual_user, F.text)
async def rpt_manual_user(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    raw = (msg.text or "").strip()
    tg_id = _fa_to_int(raw)
    if tg_id is None or tg_id <= 0:
        await msg.answer("❌ یه آیدیِ عددیِ معتبر بفرست:", reply_markup=_cancel_kb())
        return
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == tg_id))
    if not user:
        await msg.answer(
            "⚠️ کاربری با این آیدی تویِ این ربات پیدا نشد (باید حداقل یه‌بار /start زده باشه). "
            "دوباره امتحان کن یا لغو کن:", reply_markup=_cancel_kb())
        return
    current = await get_points(row.id, user.id)
    await state.update_data(manual_bot_user_id=user.id, manual_tg_id=tg_id)
    await state.set_state(RptFSM.manual_amount)
    uname = f"@{user.username}" if getattr(user, "username", None) else f"<code>{tg_id}</code>"
    await msg.answer(
        f"👤 کاربر: {uname}\n💰 امتیازِ فعلی: <b>{current}</b>\n\n"
        "چند امتیاز اضافه بشه؟ برایِ کم‌کردن، عدد رو با منفی بفرست (مثلاً <code>-5</code>):",
        reply_markup=_cancel_kb())


@rpt_router.message(RptFSM.manual_amount, F.text)
async def rpt_manual_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    raw = (msg.text or "").strip()
    neg = raw.startswith("-")
    if neg:
        raw = raw[1:]
    n = _fa_to_int(raw)
    if n is None or n <= 0 or n > 1_000_000:
        await msg.answer("❌ یه عددِ معتبر بفرست (مثلاً 5 یا -5):", reply_markup=_cancel_kb())
        return
    delta = -n if neg else n
    data = await state.get_data()
    bot_user_id = data.get("manual_bot_user_id")
    tg_id = data.get("manual_tg_id")
    row = await get_bot_row(bot)
    await state.clear()
    if not row or not bot_user_id:
        await msg.answer("⚠️ نشست منقضی شد؛ از اول شروع کن.")
        return
    new_total = await admin_adjust_points(row.id, bot_user_id, delta)
    sign = "➕" if delta > 0 else "➖"
    await msg.answer(
        f"✅ {sign} {abs(delta)} امتیاز برایِ <code>{tg_id}</code> ثبت شد.\n"
        f"💰 امتیازِ جدید: <b>{new_total}</b>")
    await _show_panel(msg, row.id)


@rpt_router.callback_query(F.data == "rpt:pack")
async def rpt_pack(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(RptFSM.vol)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام پاک‌کردنِ کیبورد")
    await cb.message.answer(
        "📦 حجمِ کانفیگِ جایزه چند گیگ باشد؟ (<code>0</code> = نامحدود):",
        reply_markup=_cancel_kb())
    await cb.answer()


@rpt_router.message(RptFSM.vol, F.text)
async def rpt_set_vol(msg: Message, state: FSMContext):
    n = _fa_to_int(msg.text)
    if n is None or n < 0:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 2).")
        return
    await state.update_data(rpt_vol=n)
    await state.set_state(RptFSM.days)
    await msg.answer("📅 مدتِ کانفیگِ جایزه چند روز باشد؟ (مثلاً <code>30</code>):",
                     reply_markup=_cancel_kb())


@rpt_router.message(RptFSM.days, F.text)
async def rpt_set_days(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    n = _fa_to_int(msg.text)
    if n is None or n < 0:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 30).")
        return
    data = await state.get_data()
    vol = int(data.get("rpt_vol", 0))
    row = await get_bot_row(bot)
    await state.clear()
    if not row:
        return
    await _set(row.id, "refpoint_reward_vol", str(vol))
    await _set(row.id, "refpoint_reward_days", str(n))
    await msg.answer(f"✅ بستهٔ جایزه: <b>{vol} گیگ / {n} روز</b> ذخیره شد.")
    await _show_panel(msg, row.id)


@rpt_router.callback_query(F.data == "rpt:rtext")
async def rpt_rtext(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(RptFSM.text)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام پاک‌کردنِ کیبورد")
    await cb.message.answer("✏️ متنِ جایزه را بفرست:", reply_markup=_cancel_kb())
    await cb.answer()


@rpt_router.message(RptFSM.text, F.text)
async def rpt_set_text(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    await state.clear()
    if not row:
        return
    await _set(row.id, "refpoint_reward_text", msg.text)
    await msg.answer("✅ متنِ جایزه ذخیره شد.")
    await _show_panel(msg, row.id)


# === PATCH_REFPOINT_REWARD_TOPUP_V1 ===

async def _topup_reward(bot: Bot, bot_id: int, u: BotUser, msg: Message,
                        acc, vol: int, days: int) -> bool:
    """شارژِ همان کانفیگِ جایزهٔ قبلی به‌جایِ ساختِ کانفیگِ تازه."""
    from models import PanelAccount, VpnPanel
    from tenant.shared.panel_clients.vpn_panel import PanelError
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client

    aid, email, panel_key = acc.id, acc.email, acc.panel_key
    async with Session() as s:
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == panel_key))
        if not panel:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))  # noqa: E712
    if not panel:
        await msg.answer("⛔️ پنلِ این سرویس دیگر متصل نیست؛ با پشتیبانی تماس بگیر.")
        return False

    client = make_panel_client(panel)
    try:
        if vol > 0:
            await client.add_traffic(email, vol)
        if days > 0:
            await client.renew(email, days)
    except PanelError as e:
        await msg.answer(f"❌ شارژِ سرویس ناموفق بود.\n<code>{_esc_html(str(e))}</code>")
        return False
    except Exception as e:
        logger.exception("خطا در شارژِ کانفیگِ جایزهٔ امتیازِ رفرال")
        await msg.answer(f"❌ خطای غیرمنتظره در شارژِ سرویس.\n<code>{type(e).__name__}: {_esc_html(str(e))}</code>")
        return False
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده هنگام بستنِ کلاینتِ پنل")

    async with Session() as s:
        a2 = await s.get(PanelAccount, aid)
        if a2:
            if vol > 0:
                a2.volume_gb = (a2.volume_gb or 0) + vol
            if days > 0:
                a2.days = (a2.days or 0) + days
            await s.commit()
    parts = []
    if vol > 0:
        parts.append(_fmt_vol(vol))
    if days > 0:
        parts.append(f"{days} روز")
    await msg.answer(
        "🎁 جایزه‌ات روی همان سرویسِ قبلی اعمال شد!\n"
        + (f"➕ {' و '.join(parts)} اضافه شد.\n" if parts else "")
        + "برایِ دیدنِ سرویس، از «سرویس‌های من» استفاده کن.")
    return True


# ═══════════════════════════════════════════
# === PATCH_REFPOINT_SMM_REWARD_V1 ===
# جایزه = یکی از سرویس‌هایِ خدماتِ مجازیِ خودِ ربات (SmmService)، رایگان.
#
# چرا با «انتظارِ لینک» و نه تحویلِ مستقیم: سفارشِ SMM بدونِ لینکِ هدف
# (پیجِ اینستاگرام، پستِ تلگرام و...) بی‌معناست، پس بعدِ کسرِ امتیاز از
# کاربر لینک خواسته می‌شود و سفارش با قیمتِ صفر ثبت می‌شود.
#
# وضعیتِ «منتظرِ لینک» عمداً در Setting ذخیره می‌شود نه FSM: مسیرِ تحویل
# (referral_gate) به این تابع state نمی‌دهد، و این‌طوری با ری‌استارتِ
# سرویس هم جایزهٔ کاربر نمی‌سوزد.
# ═══════════════════════════════════════════

_PENDING_KEY = "refpoint_smm_pending_%s"


async def _deliver_smm(bot: Bot, bot_id: int, u: BotUser, msg: Message) -> bool:
    from models import SmmService
    svc_s = await _get(bot_id, "refpoint_reward_smm_id", "")
    qty_s = await _get(bot_id, "refpoint_reward_smm_qty", "0")
    if not svc_s.isdigit() or not qty_s.isdigit() or int(qty_s) <= 0:
        await msg.answer("⛔️ هنوز سرویسِ جایزه تنظیم نشده؛ به مالکِ ربات اطلاع بده.")
        return False
    async with Session() as s:
        v = await s.get(SmmService, int(svc_s))
    if not v or v.bot_id != bot_id or not v.is_active:
        await msg.answer("⛔️ سرویسِ جایزه دیگر در دسترس نیست؛ به مالکِ ربات اطلاع بده.")
        return False
    qty = int(qty_s)
    if qty < (v.min_qty or 1) or (v.max_qty and qty > v.max_qty):
        await msg.answer("⛔️ تعدادِ تنظیم‌شدهٔ جایزه با محدودیتِ سرویس نمی‌خواند؛ به مالکِ ربات اطلاع بده.")
        return False
    await _set(bot_id, _PENDING_KEY % u.telegram_id, f"{v.id}:{qty}")
    await msg.answer(
        f"🎁 جایزه‌ات: <b>{v.name}</b> — {qty:,} عدد\n\n"
        "حالا <b>لینکِ هدف</b> رو بفرست (مثلاً آدرسِ پیج یا پست) تا سفارش ثبت بشه.",
        parse_mode="HTML")
    return True


async def _pending_smm_match(msg: Message, bot: Bot) -> bool | dict:
    """فیلترِ async — اگر این کاربر جایزهٔ SMM گرفته و منتظرِ لینک است.
    برگرداندنِ False یعنی این هندلر اصلاً صدا زده نمی‌شود و پیام دستِ
    بقیهٔ روترها می‌ماند (همان الگویِ smm_ai)."""
    if not msg.text or msg.chat.type != "private":
        return False
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or row.field != "virtual_service":
        return False
    pend = (await _get(row.id, _PENDING_KEY % msg.from_user.id, "")).strip()
    if not pend or ":" not in pend:
        return False
    return {"rp_pending": pend, "rp_row": row}


@rpt_router.message(_pending_smm_match)
async def rpt_smm_link_in(msg: Message, bot: Bot, rp_pending: str, rp_row) -> None:
    from models import SmmService, SmmOrder
    link = (msg.text or "").strip()
    if link.startswith("/") or len(link) > 500:
        await msg.answer("❌ لینکِ معتبر بفرست (یا از منو ادامه بده).")
        return
    svc_pk, qty = rp_pending.split(":", 1)
    async with Session() as s:
        v = await s.get(SmmService, int(svc_pk))
    if not v or v.bot_id != rp_row.id:
        await _set(rp_row.id, _PENDING_KEY % msg.from_user.id, "")
        await msg.answer("⛔️ سرویسِ جایزه دیگر در دسترس نیست؛ به مالکِ ربات اطلاع بده.")
        return
    from tenant import _gen_tracking
    from datetime import datetime as _dt
    from models import next_tracking as _next_tracking  # PATCH_SEQ_TRACKING_V1
    trk = await _next_tracking(rp_row.id)
    async with Session() as s:
        o = SmmOrder(
            bot_id=rp_row.id, user_telegram_id=msg.from_user.id, smm_service_pk=v.id,
            service_id=v.service_id, service_name=v.name, quantity=int(qty), link=link,
            price_toman=0, status="awaiting_admin", tracking_code=trk,
            panel_pk=v.panel_pk, updated_at=_dt.utcnow(),
            note="جایزهٔ امتیازِ رفرال",
        )
        s.add(o)
        await s.commit()
        order_pk = o.id
    await _set(rp_row.id, _PENDING_KEY % msg.from_user.id, "")
    try:
        from tenant.bots.virtual_service.smm import _panel_order_mode, _place_on_panel
        if (await _panel_order_mode(rp_row.id, v.panel_pk)) == "auto" and not v.is_manual:
            await _place_on_panel(rp_row.id, order_pk)
        else:
            # === PATCH_MANUAL_ORDER_CHANNEL_V1 === سفارشِ دستیِ جایزه هم به کانال/ادمین اعلام شود
            from tenant.bots.virtual_service.smm import announce_manual_smm_order
            await announce_manual_smm_order(bot, rp_row, order_pk)
    except Exception:
        logger.exception("refpoint smm reward: ثبت رویِ پنل ناموفق (سفارش ثبت شد و دستِ ادمین می‌ماند)")
    await msg.answer(
        f"✅ سفارشِ جایزه ثبت شد.\n📣 {v.name} — {int(qty):,} عدد\n"
        f"🔖 کدِ پیگیری: <code>{trk}</code>", parse_mode="HTML")


@rpt_router.callback_query(F.data.startswith("rpt:picksmm:"))
async def rpt_pick_smm(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    from models import SmmService
    page = int(cb.data.split(":")[2] or 0)
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id,
            SmmService.is_active == True).order_by(SmmService.id))).all()  # noqa: E712
    per = 8
    chunk = svcs[page * per:(page + 1) * per]
    rows = [[InlineKeyboardButton(text=f"{v.name[:45]}", callback_data=f"rpt:setsmm:{v.id}")]
            for v in chunk]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"rpt:picksmm:{page-1}",
                                        style=ButtonStyle.PRIMARY))
    if (page + 1) * per < len(svcs):
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"rpt:picksmm:{page+1}",
                                        style=ButtonStyle.PRIMARY))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rpt:menu",
                                      style=ButtonStyle.DANGER)])
    await cb.message.answer(
        "📣 سرویسی که می‌خوای به‌عنوانِ جایزه داده بشه رو انتخاب کن:" if chunk
        else "هیچ سرویسِ فعالی نداری. اول از بخشِ خدماتِ مجازی سرویس اضافه کن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@rpt_router.callback_query(F.data.startswith("rpt:setsmm:"))
async def rpt_set_smm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    from models import SmmService
    sid = int(cb.data.split(":")[2])
    async with Session() as s:
        v = await s.get(SmmService, sid)
    if not v or v.bot_id != row.id:
        await cb.answer("این سرویس دیگر موجود نیست.", show_alert=True)
        return
    await _set(row.id, "refpoint_reward_kind", "smm")
    await _set(row.id, "refpoint_reward_smm_id", str(sid))
    await state.set_state(RptFSM.smm_qty)
    await state.update_data(rp_smm_id=sid)
    await cb.message.answer(
        f"📣 سرویس: <b>{v.name}</b>\n"
        f"محدودهٔ مجاز: {v.min_qty or 1:,} تا {v.max_qty or 0:,}\n\n"
        "حالا <b>تعدادِ</b> جایزه رو بفرست (فقط عدد):",
        parse_mode="HTML", reply_markup=_cancel_kb())
    await cb.answer()


@rpt_router.message(RptFSM.smm_qty, F.text)
async def rpt_set_smm_qty(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, fa_to_int
    from models import SmmService
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    qty = fa_to_int((msg.text or "").strip())
    data = await state.get_data()
    sid = int(data.get("rp_smm_id") or 0)
    async with Session() as s:
        v = await s.get(SmmService, sid)
        if v is not None and await _is_foreign(v, bot):   # PATCH_TENANT_SCOPE_V1
            v = None
    if not v:
        await state.clear()
        await msg.answer("سرویس پیدا نشد.")
        return
    if not qty or qty < (v.min_qty or 1) or (v.max_qty and qty > v.max_qty):
        await msg.answer(f"❌ عدد باید بینِ {v.min_qty or 1:,} و {v.max_qty or 0:,} باشد. دوباره بفرست:")
        return
    await state.clear()
    await _set(row.id, "refpoint_reward_smm_qty", str(qty))
    await msg.answer(f"✅ جایزه ثبت شد: {v.name} — {qty:,} عدد")
    await _show_panel(msg, row.id)


# === PATCH_REFPOINT_PANEL_KIND_PICKER_V1 ===
@rpt_router.callback_query(F.data.startswith("rpt:pickpanel"))
async def rpt_pick_panel(cb: CallbackQuery, bot: Bot):
    """لیستِ پنل‌هایِ فعالِ همین ربات؛ مالک یکی را برایِ جایزهٔ کانفیگِ
    خودکار انتخاب می‌کند."""
    row = await _owner_ok(cb, bot)
    if not row:
        return
    from models import VpnPanel
    from tenant.bots.vpn.vpn_admin import panel_label
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == row.id, VpnPanel.enabled == True  # noqa: E712
        ).order_by(VpnPanel.id))).all()
    if not panels:
        await cb.answer("هنوز پنلی وصل نکرده‌ای؛ از «🤖 پنل VPN خودکار (X-UI)» یکی وصل کن.",
                        show_alert=True)
        return
    cur = await _get(row.id, "refpoint_reward_panel_id", "")
    rows = []
    for p in panels:
        label = await panel_label(row.id, p.panel_key)
        mark = "✅ " if cur.isdigit() and int(cur) == p.id else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}",
                                          callback_data=f"rpt:setpanel:{p.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rpt:menu",
                                      style=ButtonStyle.DANGER)])
    text = "🔌 کدوم پنل برایِ ساختِ این کانفیگِ خودکار استفاده بشه؟"
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@rpt_router.callback_query(F.data.startswith("rpt:setpanel:"))
async def rpt_set_panel(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    from models import VpnPanel
    async with Session() as s:
        p = await s.get(VpnPanel, pid)
    if not p or p.bot_id != row.id or not p.enabled:
        await cb.answer("این پنل دیگر در دسترس نیست.", show_alert=True)
        return
    from tenant.bots.vpn.vpn_admin import panel_label
    await _set(row.id, "refpoint_reward_panel_id", str(pid))
    await cb.answer(f"✅ «{await panel_label(row.id, p.panel_key)}» به‌عنوانِ پنلِ جایزه ثبت شد")
    await _show_panel(cb.message, row.id)


@rpt_router.callback_query(F.data == "rpt:pickcfgkind")
async def rpt_pick_cfgkind(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await _get(row.id, "refpoint_reward_cfgkind", "all")
    rows = []
    for key, label in CFG_KIND_LABELS.items():
        mark = "✅ " if cur == key else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}",
                                          callback_data=f"rpt:setcfgkind:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rpt:menu",
                                      style=ButtonStyle.DANGER)])
    text = "📶 جایزه فقط از کدوم نوعِ کانفیگِ انبار داده بشه؟"
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@rpt_router.callback_query(F.data.startswith("rpt:setcfgkind:"))
async def rpt_set_cfgkind(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    if key not in CFG_KIND_LABELS:
        await cb.answer("نامعتبر", show_alert=True)
        return
    await _set(row.id, "refpoint_reward_cfgkind", key)
    await cb.answer(f"✅ «{CFG_KIND_LABELS[key]}» ذخیره شد")
    await _show_panel(cb.message, row.id)


# ═══════════════════════════════════════════
#  === PATCH_REFPOINT_PRIZES_V1 === ادمین: گیفت، استارز و فهرستِ جایزه‌ها
# ═══════════════════════════════════════════
@rpt_router.callback_query(F.data.startswith("rpt:pickgift:"))
async def rpt_pick_gift(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    try:
        gifts = sorted((await bot.get_available_gifts()).gifts, key=lambda g: g.star_count)
    except Exception as e:                                   # noqa: BLE001
        await cb.answer()
        return await cb.message.answer(f"❌ دریافتِ فهرستِ گیفت‌ها از تلگرام ناموفق بود:\n{_esc_html(str(e)[:200])}")
    per = 8
    chunk = gifts[page * per:(page + 1) * per]
    rows = [[InlineKeyboardButton(
        text=f"{getattr(getattr(g, 'sticker', None), 'emoji', None) or '🎁'} {g.star_count}⭐",
        callback_data=f"rpt:setgift:{g.id}")] for g in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"rpt:pickgift:{page-1}"))
    if (page + 1) * per < len(gifts):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"rpt:pickgift:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rpt:menu", style=ButtonStyle.DANGER)])
    text = "🎁 کدام گیفت جایزه باشد؟ (ارسالِ خودکار از استارزِ خودِ ربات کم می‌شود)"
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@rpt_router.callback_query(F.data.startswith("rpt:setgift:"))
async def rpt_set_gift(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = cb.data.split(":", 2)[2]
    stars, emoji = "", "🎁"
    try:
        for g in (await bot.get_available_gifts()).gifts:
            if g.id == gid:
                stars, emoji = str(g.star_count), (getattr(getattr(g, "sticker", None), "emoji", None) or "🎁")
    except Exception:
        logger.info("refpoint gift: خواندنِ مشخصاتِ گیفت ممکن نشد")
    await _set(row.id, "refpoint_reward_kind", "gift")
    await _set(row.id, "refpoint_reward_gift_id", gid)
    await _set(row.id, "refpoint_reward_gift_stars", stars)
    await _set(row.id, "refpoint_reward_gift_emoji", emoji)
    await cb.answer("✅ گیفت ثبت شد")
    await _show_panel(cb.message, row.id)


@rpt_router.callback_query(F.data == "rpt:delivery")
async def rpt_delivery(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await _get(row.id, "refpoint_reward_delivery", "manual")
    await _set(row.id, "refpoint_reward_delivery", "manual" if cur == "auto" else "auto")
    await cb.answer("ذخیره شد")
    await _show_panel(cb.message, row.id)


@rpt_router.callback_query(F.data == "rpt:starsamt")
async def rpt_stars_amt(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(RptFSM.stars_amt)
    await cb.message.answer("⭐ تعدادِ استارزِ جایزه را بفرست (عدد):", reply_markup=_cancel_kb())
    await cb.answer()


@rpt_router.message(RptFSM.stars_amt, F.text)
async def rpt_set_stars_amt(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    n = _fa_to_int(msg.text)
    if not row or not n or n <= 0 or n > 1_000_000:
        return await msg.answer("❌ عددِ معتبر بفرست.", reply_markup=_cancel_kb())
    await state.clear()
    await _set(row.id, "refpoint_reward_kind", "stars")
    await _set(row.id, "refpoint_reward_stars", str(n))
    await msg.answer(f"✅ جایزه: {n:,} استارز")
    await _show_panel(msg, row.id)


async def _pool_view(bot_id: int):
    pool = await pool_get(bot_id)
    lines = ["🧺 <b>فهرستِ جایزه‌ها</b>\n",
             "هر جایزه‌ای که الان در پنلِ بالا تنظیم کرده‌ای را می‌توانی با «➕ افزودنِ جایزهٔ فعلی» به این فهرست اضافه کنی؛ "
             "بعد برایِ جایزهٔ بعدی تنظیماتِ پنل را عوض کن و دوباره اضافه کن.\n"
             "• خالی = فقط جایزهٔ تکیِ پنل (مثلِ قبل)\n• یک جایزه = مستقیم تحویل\n"
             "• چند جایزه = کاربر بعد از بازشدنِ قفل <b>یکی</b> را انتخاب می‌کند\n"]
    rows = []
    for i, e in enumerate(pool):
        lines.append(f"{i + 1}. {_esc_html(entry_title(e))}")
        rows.append([InlineKeyboardButton(text=f"🗑 حذفِ «{entry_title(e)[:30]}»", callback_data=f"rpt:pooldel:{i}",
                                          style=ButtonStyle.DANGER)])
    if not pool:
        lines.append("— خالی —")
    if len(pool) < MAX_PRIZES:
        rows.insert(0, [InlineKeyboardButton(text="➕ افزودنِ جایزهٔ فعلی (تنظیماتِ پنل)", callback_data="rpt:pooladd",
                                             style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rpt:menu", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@rpt_router.callback_query(F.data == "rpt:pool")
async def rpt_pool(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    text, kb = await _pool_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@rpt_router.callback_query(F.data == "rpt:pooladd")
async def rpt_pool_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if len(await pool_get(row.id)) >= MAX_PRIZES:
        return await cb.answer(f"حداکثر {MAX_PRIZES} جایزه.", show_alert=True)
    await state.set_state(RptFSM.pool_label)
    kind = await _get(row.id, "refpoint_reward_kind", "stock")
    await cb.message.answer(
        f"🏷 یک نامِ کوتاه برایِ این جایزه بفرست (کاربر همین را می‌بیند).\nجایزهٔ فعلی: {REWARD_LABELS.get(kind, kind)}",
        reply_markup=_cancel_kb())
    await cb.answer()


@rpt_router.message(RptFSM.pool_label, F.text)
async def rpt_pool_label(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    label = (msg.text or "").strip()[:60]
    if not label:
        return await msg.answer("❌ نامِ معتبر بفرست.", reply_markup=_cancel_kb())
    await state.clear()
    pool = await pool_get(row.id)
    if len(pool) >= MAX_PRIZES:
        return await msg.answer(f"حداکثر {MAX_PRIZES} جایزه.")
    pool.append({"label": label, "cfg": await snapshot_current_cfg(row.id)})
    await _pool_set(row.id, pool)
    await msg.answer(f"✅ «{_esc_html(label)}» به فهرست اضافه شد.", parse_mode="HTML")
    text, kb = await _pool_view(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@rpt_router.callback_query(F.data.startswith("rpt:pooldel:"))
async def rpt_pool_del(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    i = int(cb.data.split(":")[2])
    pool = await pool_get(row.id)
    if 0 <= i < len(pool):
        pool.pop(i)
        await _pool_set(row.id, pool)
    text, kb = await _pool_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer("حذف شد")


async def _is_foreign(obj, bot) -> bool:
    """=== PATCH_TENANT_SCOPE_V1 === True اگر رکوردی که با idِ داخلِ
    callback_data/state خوانده شده مالِ رباتِ جاریِ این آپدیت نباشد."""
    if bot is None or getattr(obj, "bot_id", None) is None:
        return False
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    return bool(_r) and obj.bot_id != _r.id
