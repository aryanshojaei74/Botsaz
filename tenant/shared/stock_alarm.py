"""
stock_alarm.py — آلارمِ تمام‌شدنِ «دستهٔ» انبارِ دستی + دکمهٔ «🩺 بررسی باگ و انبار».

=== PATCH_STOCK_ALARM_V1 ===
انبارِ دستی برخلافِ پنل‌ها کنترلِ خودکار ندارد؛ وقتی همهٔ کانفیگ‌هایِ یک دسته
(مثلاً «دسته ۱») فروخته یا به‌عنوانِ تست داده شود، مالک تا شکایتِ مشتری خبردار
نمی‌شد. این ماژول:
  • هر ۳ دقیقه موجودیِ هر دسته (Config.status == "in_stock") را می‌شمارد؛
  • وقتی دسته‌ای از «موجود» به «صفر» برود، یک‌بار به مالک پیام می‌دهد؛
  • وقتی دوباره شارژ شد، وضعیتش ریست می‌شود تا دفعهٔ بعدی دوباره خبر بدهد؛
  • اولین اجرا فقط وضعیت را ثبت می‌کند (بدونِ هشدارِ الکی برایِ دسته‌هایِ از قبل خالی).
حالتِ آلارم با Setting «stock_alarm_enabled» (پیش‌فرض روشن) قابلِ خاموش‌کردن است.

دکمهٔ «🩺 بررسی باگ و انبار» (callback = stk:menu): موجودیِ هر دسته + ناسازگاری‌هایِ
شناخته‌شده را نشان می‌دهد و فقط رفع‌هایِ بی‌خطر را (با تأییدِ مالک) انجام می‌دهد.
"""
from __future__ import annotations

import asyncio
import json

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, Config, MainButton, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

stock_router = Router()

STATE_KEY = "stock_alarm_state"
ENABLED_KEY = "stock_alarm_enabled"
NO_CAT = "(بدون دسته)"
DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)


# ─────────────────────────── Setting helpers (مستقل از کش) ───────────────────────────

async def _sget(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return r.value if (r and r.value is not None) else default


async def _sset(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


# ─────────────────────────── شمارشِ موجودی ───────────────────────────

async def stock_by_category(bot_id: int) -> dict[str, tuple[int, int]]:
    """{دسته: (موجود, کلِ ثبت‌شده)} — دسته‌بندی‌نشده زیرِ NO_CAT می‌آید."""
    async with Session() as s:
        rows = (await s.execute(
            select(Config.category, Config.status, func.count()).where(
                Config.bot_id == bot_id).group_by(Config.category, Config.status))).all()
    out: dict[str, list[int]] = {}
    for cat, status, n in rows:
        k = cat or NO_CAT
        cur = out.setdefault(k, [0, 0])
        cur[1] += n
        if status == "in_stock":
            cur[0] += n
    return {k: (v[0], v[1]) for k, v in out.items()}


def diff_empty(prev: dict | None, now: dict[str, int]) -> tuple[list[str], list[str], dict]:
    """(تازه‌خالی‌شده‌ها، تازه‌شارژشده‌ها، stateِ جدید). prev=None یعنی اولین اجرا."""
    new_state = {k: int(v) for k, v in now.items()}
    if prev is None:
        return [], [], new_state
    emptied = [k for k, v in now.items() if v == 0 and int(prev.get(k, 0)) > 0]
    refilled = [k for k, v in now.items() if v > 0 and k in prev and int(prev[k]) == 0]
    return emptied, refilled, new_state


async def _load_state(bot_id: int) -> dict | None:
    raw = await _sget(bot_id, STATE_KEY, "")
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


async def check_bot(bot_id: int, send) -> list[str]:
    """یک دور بررسیِ این ربات. send(text) پیام را به مالک می‌فرستد. دسته‌هایِ
    تازه‌خالی را برمی‌گرداند."""
    if await _sget(bot_id, ENABLED_KEY, "1") == "0":
        return []
    counts = {k: v[0] for k, v in (await stock_by_category(bot_id)).items()}
    if not counts:
        return []
    prev = await _load_state(bot_id)
    emptied, _refilled, new_state = diff_empty(prev, counts)
    # state را قبل از ارسال ذخیره می‌کنیم تا خطایِ ارسال باعثِ هشدارِ تکراری نشود.
    await _sset(bot_id, STATE_KEY, json.dumps(new_state, ensure_ascii=False))
    for cat in emptied:
        label = "بدون دسته" if cat == NO_CAT else f"«{cat}»"
        try:
            await send(
                f"🚨 <b>موجودیِ دستهٔ {label} تمام شد!</b>\n\n"
                "همهٔ کانفیگ‌هایِ این دسته فروخته/تحویل شده‌اند. برای ادامهٔ فروش، "
                "کانفیگِ جدید به انبار اضافه کنید.\n"
                "ℹ️ خاموش‌کردنِ این هشدار: 🩺 بررسی باگ و انبار ← 🔔")
        except Exception:
            logger.exception("stock_alarm: ارسال به مالکِ ربات %s", bot_id)
    return emptied


async def stock_alarm_loop(interval: int = 180):
    await asyncio.sleep(75)
    while True:
        try:
            async with Session() as s:
                bot_ids = list((await s.scalars(select(Config.bot_id).distinct())).all())
            for bot_id in bot_ids:
                try:
                    async with Session() as s:
                        brow = await s.get(BotRow, bot_id)
                    if not brow or brow.status != "active" or not brow.token:
                        continue
                    tbot = Bot(token=brow.token, default=DEFAULTS)
                    try:
                        async def _send(text, _b=tbot, _o=brow.owner_id):
                            await _b.send_message(_o, text)
                        await check_bot(bot_id, _send)
                    finally:
                        await tbot.session.close()
                except Exception:
                    logger.exception("stock_alarm: خطا در ربات %s", bot_id)
        except Exception:
            logger.exception("stock_alarm_loop error")
        await asyncio.sleep(interval)


# ─────────────────────────── تشخیصِ مشکلات ───────────────────────────

async def diagnose(bot_id: int) -> dict:
    """مشکلاتِ شناخته‌شده را برمی‌گرداند. هر مورد: (متن، قابلِ‌رفعِ خودکار؟).
    فقط می‌خواند؛ رفع در apply_fixes است."""
    issues: list[tuple[str, bool]] = []
    cats = await stock_by_category(bot_id)
    async with Session() as s:
        n_owned = await s.scalar(select(func.count()).select_from(Config).where(
            Config.bot_id == bot_id, Config.status == "in_stock",
            Config.owner_user_id.is_not(None))) or 0
        n_empty = await s.scalar(select(func.count()).select_from(Config).where(
            Config.bot_id == bot_id, Config.status == "in_stock",
            func.length(func.trim(Config.payload)) == 0)) or 0
        dup = (await s.execute(
            select(Config.payload, func.count()).where(
                Config.bot_id == bot_id, Config.status == "in_stock",
                Config.kind == "sublink").group_by(Config.payload).having(func.count() > 1))).all()
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.action == "free_trial"))).all()
    if n_owned:
        issues.append((f"{n_owned} کانفیگِ «موجود» دارایِ صاحب است (ناسازگار؛ ممکن است به کسی تحویل داده شود که قبلاً داشته).", True))
    if n_empty:
        issues.append((f"{n_empty} کانفیگِ موجود، متنِ/لینکِ خالی دارد — به مشتری خالی تحویل می‌شود. (باید دستی حذف/اصلاح شود)", False))
    if dup:
        issues.append((f"{len(dup)} لینکِ تکراری در انبارِ موجود هست ({sum(n for _, n in dup)} ردیف) — ممکن است یک کانفیگ به دو نفر برسد. (دستی رسیدگی شود)", False))
    for mb in btns:
        try:
            cfg = json.loads(mb.config) if mb.config else {}
        except Exception:
            issues.append((f"تنظیماتِ دکمهٔ تستِ «{mb.label}» خراب است. (دوباره تنظیم کنید)", False))
            continue
        mode = cfg.get("trial_mode")
        if not mode:
            continue
        raw = cfg.get("trial_claim_limit", 1)
        try:
            int(str(raw).strip())
        except Exception:
            issues.append((f"«دفعاتِ مجاز» دکمهٔ «{mb.label}» عددِ معتبر نیست ({raw!r}) — به ۱ برمی‌گردد.", True))
        if mode in ("manual", "unlimited"):
            tc = cfg.get("trial_category")
            if tc and tc not in cats:
                issues.append((f"دکمهٔ تستِ «{mb.label}» به دستهٔ «{tc}» وصل است که اصلاً در انبار وجود ندارد.", False))
            elif tc and cats.get(tc, (0, 0))[0] == 0:
                issues.append((f"دستهٔ تستِ «{mb.label}» («{tc}») الان خالی است — کاربر «ناموجود» می‌بیند.", False))
            elif not tc and not any(v[0] for v in cats.values()):
                issues.append((f"انبار برایِ دکمهٔ تستِ «{mb.label}» خالی است.", False))
    return {"cats": cats, "issues": issues, "n_trial_btns": len(btns)}


async def apply_fixes(bot_id: int) -> list[str]:
    """فقط رفع‌هایِ بی‌خطر. لیستِ کارهایِ انجام‌شده را برمی‌گرداند."""
    done: list[str] = []
    async with Session() as s:
        res = await s.execute(
            Config.__table__.update().where(
                Config.bot_id == bot_id, Config.status == "in_stock",
                Config.owner_user_id.is_not(None)).values(owner_user_id=None))
        if res.rowcount:
            done.append(f"{res.rowcount} کانفیگِ موجود از صاحبِ اشتباه آزاد شد")
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.action == "free_trial"))).all()
        for mb in btns:
            try:
                cfg = json.loads(mb.config) if mb.config else {}
            except Exception:
                continue
            if not cfg.get("trial_mode"):
                continue
            try:
                int(str(cfg.get("trial_claim_limit", 1)).strip())
            except Exception:
                cfg["trial_claim_limit"] = "1"
                mb.config = json.dumps(cfg, ensure_ascii=False)
                done.append(f"«دفعاتِ مجاز» دکمهٔ «{mb.label}» روی ۱ تنظیم شد")
        await s.commit()
    if done:
        try:
            from tenant import _BTN_CFG_CACHE
            _BTN_CFG_CACHE.clear()
        except Exception:
            pass
    return done


# ─────────────────────────── رابطِ کاربری ───────────────────────────

def _render(info: dict, enabled: bool) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🩺 <b>بررسی باگ و انبارِ دستی</b>\n"]
    cats = info["cats"]
    if cats:
        lines.append("📦 <b>موجودی هر دسته</b>")
        for k in sorted(cats):
            have, total = cats[k]
            mark = "🔴" if have == 0 else ("🟡" if have <= 2 else "🟢")
            lines.append(f"{mark} {k}: <b>{have}</b> موجود از {total}")
    else:
        lines.append("📦 انبارِ دستی خالی است.")
    lines.append("")
    issues = info["issues"]
    if issues:
        lines.append(f"⚠️ <b>{len(issues)} مورد پیدا شد</b>")
        for t, fix in issues:
            lines.append(("🛠 " if fix else "• ") + t)
        lines.append("\n🛠 = با دکمهٔ رفع قابل‌اصلاحِ خودکار")
    else:
        lines.append("✅ مشکلِ شناخته‌شده‌ای پیدا نشد.")
    lines.append("\nℹ️ مالک/ادمینِ ربات از سقفِ «دفعاتِ مجازِ تست» معاف‌اند؛ برای آزمایشِ سقف با یک "
                 "حسابِ عادی تست بگیرید.")
    rows = []
    if any(f for _, f in issues):
        rows.append([InlineKeyboardButton(text="🛠 رفعِ خودکارِ موارد بی‌خطر",
                                          callback_data="stk:fix", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🔄 بررسیِ دوباره", callback_data="stk:menu",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text="🔔 هشدارِ تمام‌شدنِ دسته: " + ("روشن ✅" if enabled else "خاموش ⛔️"),
        callback_data="stk:tog", style=ButtonStyle.PRIMARY)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb: CallbackQuery, bot_id: int) -> None:
    info = await diagnose(bot_id)
    enabled = (await _sget(bot_id, ENABLED_KEY, "1")) != "0"
    text, kb = _render(info, enabled)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


async def stk_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await cb.answer()
    await _show(cb, row.id)


@stock_router.callback_query(F.data == "stk:menu")
async def _h_menu(cb: CallbackQuery, bot: Bot):
    await stk_menu(cb, bot)


@stock_router.callback_query(F.data == "stk:fix")
async def _h_fix(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    done = await apply_fixes(row.id)
    await cb.answer("✅ " + ("؛ ".join(done) if done else "موردی برای رفع نبود"), show_alert=True)
    await _show(cb, row.id)


@stock_router.callback_query(F.data == "stk:tog")
async def _h_tog(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = (await _sget(row.id, ENABLED_KEY, "1")) != "0"
    await _sset(row.id, ENABLED_KEY, "0" if cur else "1")
    await cb.answer("هشدار " + ("خاموش شد" if cur else "روشن شد"))
    await _show(cb, row.id)
