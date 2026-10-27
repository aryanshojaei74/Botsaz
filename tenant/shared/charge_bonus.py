"""
charge_bonus.py — «🎁 بونوسِ شارژِ کیف پول» (PATCH_CHARGE_BONUS_V1).

درخواستِ مشتری (Weirdnet): «با ۳۰۰ هزار تومان شارژ کیف پول، ۲۰ درصد بیشتر بگیرند».

مالکِ ربات چند «پله» تعریف می‌کند (مثلاً ۳۰۰٬۰۰۰ ⇒ ۲۰٪ و ۱٬۰۰۰٬۰۰۰ ⇒ ۲۵٪). هر شارژِ موفقِ کیف پول که مبلغش
به یک پله برسد، همان لحظه و در همان تراکنشِ دیتابیس درصدِ بونوسِ بالاترین پلهٔ رسیده‌شده هم به موجودی اضافه
و در تاریخچه با نوعِ «charge_bonus» ثبت می‌شود (در آمارِ درآمد/شارژ شمرده نمی‌شود چون پولِ واقعی نیست).

چرا هوکِ SQLAlchemy؟ کیف پول رویِ ~۲۰ مسیرِ مستقل شارژ می‌شود (کارت‌به‌کارت، زرین‌پال، زیبال، آپی،
تون‌پیز، ارز دیجیتال، استارز، سواپ، حساب‌پی، آبان‌گیت‌وی، یونیک‌پی…) و هیچ نقطهٔ مشترکی نبود. هوکِ
`after_flush` فقط تراکنش‌هایِ «شارژِ درگاه» را می‌شناسد (فهرستِ TOPUP_KINDS)، پس بازگشتِ وجه، جایزهٔ بازی،
پاداشِ دعوت و تنظیمِ دستیِ ادمین هرگز بونوس نمی‌گیرند. پیامِ «بونوس اضافه شد» بعد از commit به کاربر می‌رود.
"""
from __future__ import annotations

import asyncio
import re

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import event, insert, select, update
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from models import Bot as BotRow, BotUser, Setting, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

charge_bonus_router = Router(name="charge_bonus")

KEY_ON = "chg_bonus_on"          # "1" = روشن
KEY_TIERS = "chg_bonus_tiers"    # «300000=20;1000000=25»
KEY_CAP = "chg_bonus_cap"        # سقفِ بونوسِ هر شارژ (تومان)، ۰/خالی = بدونِ سقف
KEY_METHODS = "chg_bonus_methods"   # PATCH_CHARGE_BONUS_METHODS_V1: روش‌های بونوس‌دار («card,zarinpal»)؛ خالی = همهٔ روش‌ها
BONUS_KIND = "charge_bonus"      # ≤ ۲۰ کاراکتر (ستونِ WalletTxn.kind)

# شارژهایِ واقعیِ درگاه/دستی (تراکنش‌هایِ «charge» عمومی فقط با این یادداشت‌ها: بقیه بازگشتِ وجه/جایزه‌اند)
TOPUP_KINDS = frozenset({
    "crypto_charge", "nowpay_charge", "uniquepay_charge", "tonpays_charge", "swap_charge",
    "abangateway_charge", "hesabpay_charge", "zibal_charge", "zarinpal_charge", "apay_charge", "hooshpay_charge", "blupal_charge",
})
TOPUP_NOTE_PREFIXES = ("AUTO:شارژ خودکار", "شارژ با استارز", "شارژ سفارش #")

# === PATCH_CHARGE_BONUS_METHODS_V1 === گزارش فدایی: «بتونیم تنظیم کنیم با چه روش‌هایی شارژ کنند بونوس بگیرند».
# کلید روش ⇒ (برچسب، kindهای WalletTxn، پیشوند یادداشت شارژهای عمومی «charge»)
METHODS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "card":        ("💳 کارت‌به‌کارت", (), ("AUTO:شارژ خودکار", "شارژ سفارش #")),
    "stars":       ("⭐️ استارز تلگرام", (), ("شارژ با استارز",)),
    "zarinpal":    ("🔐 زرین‌پال", ("zarinpal_charge",), ()),
    "zibal":       ("🟦 زیبال", ("zibal_charge",), ()),
    "apay":        ("🅰️ آپی", ("apay_charge",), ()),
    "crypto":      ("🪙 ارز دیجیتال/تون", ("crypto_charge", "nowpay_charge", "tonpays_charge"), ()),
    "swap":        ("🔄 سواپ‌ولت", ("swap_charge",), ()),
    "uniquepay":   ("🟣 یونیک‌پی", ("uniquepay_charge",), ()),
    "abangateway": ("🏦 آبان‌گیت‌وی", ("abangateway_charge",), ()),
    "hooshpay":    ("💳 هوش‌پی", ("hooshpay_charge",), ()),          # PATCH_HOOSHPAY_GATEWAY_V1
    "blupal":      ("💳 بلوپال", ("blupal_charge",), ()),            # PATCH_BLUPAL_TENANT_V1
    "hesabpay":    ("🇦🇫 حساب‌پی", ("hesabpay_charge",), ()),
}


def method_of(t) -> str | None:
    """روش پرداخت یک تراکنش شارژ (کلید METHODS) یا None اگر ناشناخته است."""
    k = getattr(t, "kind", "") or ""
    note = getattr(t, "note", "") or ""
    for key, (_lbl, kinds, prefixes) in METHODS.items():
        if k in kinds or (k == "charge" and prefixes and note.startswith(prefixes)):
            return key
    return None


def parse_methods(raw: str | None) -> set[str]:
    """مجموعهٔ روش‌های فعال؛ خالی/نامعتبر ⇒ مجموعهٔ تهی (یعنی «همهٔ روش‌ها»، رفتار قبلی)."""
    return {m.strip() for m in (raw or "").split(",") if m.strip() in METHODS}


def method_allowed(t, enabled: set[str]) -> bool:
    """بدون محدودیت (enabled تهی) یا روش این شارژ در فهرست ⇒ مجاز. روش ناشناخته فقط وقتی محدودیتی نیست مجاز است."""
    if not enabled:
        return True
    m = method_of(t)
    return m is not None and m in enabled


_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ITEM = re.compile(r"(\d[\d,٬،.]*)\s*(?:=|:|=>|->|→|—|-|\s)\s*(\d+(?:\.\d+)?)\s*(?:%|٪)?")


# ───────────────────────── منطقِ خالص ─────────────────────────

def parse_tiers(text: str) -> list[tuple[int, float]]:
    """«300000=20» (هر پله در یک خط یا با ؛) ⇒ [(300000, 20.0)] مرتب بر اساسِ مبلغ. نامعتبرها نادیده."""
    out: dict[int, float] = {}
    t = (text or "").translate(_FA)
    for chunk in re.split(r"[\n;؛|]+", t):
        m = _ITEM.search(chunk.strip())
        if not m:
            continue
        try:
            amt = int(re.sub(r"[^\d]", "", m.group(1)))
            pct = float(m.group(2))
        except ValueError:
            continue
        if amt > 0 and 0 < pct <= 1000:
            out[amt] = pct
    return sorted(out.items())


def bonus_for(amount: float, tiers: list[tuple[int, float]], cap: int = 0) -> tuple[int, float]:
    """(مبلغِ بونوس به تومان، درصدِ اعمال‌شده). بالاترین پله‌ای که مبلغ به آن می‌رسد."""
    pct = 0.0
    for min_amt, p in tiers:
        if amount >= min_amt:
            pct = p
    if pct <= 0:
        return 0, 0.0
    b = int(amount * pct / 100)
    if cap and cap > 0:
        b = min(b, int(cap))
    return max(0, b), pct


def tiers_to_text(tiers: list[tuple[int, float]]) -> str:
    return "\n".join(f"{a}={p:g}" for a, p in tiers)


def _pretty(tiers: list[tuple[int, float]]) -> str:
    return "\n".join(f"• از <b>{a:,}</b> تومان ⇒ <b>{p:g}٪</b> بونوس" for a, p in tiers) or "—"


def _is_topup(t) -> bool:
    try:
        if float(t.amount or 0) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    k = t.kind or ""
    if k in TOPUP_KINDS:
        return True
    return k == "charge" and (t.note or "").startswith(TOPUP_NOTE_PREFIXES)


# ───────────────────────── هوکِ دیتابیس ─────────────────────────

def _apply(session, t) -> None:
    rows = session.execute(select(Setting.key, Setting.value).where(
        Setting.bot_id == t.bot_id, Setting.key.in_((KEY_ON, KEY_TIERS, KEY_CAP, KEY_METHODS)))).all()
    cfg = {k: v for k, v in rows}
    if cfg.get(KEY_ON) != "1":
        return
    if not method_allowed(t, parse_methods(cfg.get(KEY_METHODS))):
        return                                   # PATCH_CHARGE_BONUS_METHODS_V1: این روش پرداخت بونوس ندارد
    try:
        cap = int(float(cfg.get(KEY_CAP) or 0))
    except ValueError:
        cap = 0
    bonus, pct = bonus_for(float(t.amount), parse_tiers(cfg.get(KEY_TIERS, "")), cap)
    if bonus <= 0:
        return
    session.execute(update(BotUser).where(BotUser.id == t.user_id)
                    .values(balance=BotUser.balance + bonus)
                    .execution_options(synchronize_session=False))
    session.execute(insert(WalletTxn).values(
        bot_id=t.bot_id, user_id=t.user_id, amount=bonus, kind=BONUS_KIND,
        note=f"بونوس شارژ {pct:g}٪ (شارژ {int(float(t.amount)):,} تومان)"))
    obj = session.identity_map.get(identity_key(BotUser, t.user_id))
    if obj is not None:    # نسخهٔ در حافظه هم همگام شود تا فلاشِ بعدی مقدارِ قدیمی را برنگرداند
        set_committed_value(obj, "balance", float(obj.balance or 0) + bonus)
    row = session.execute(select(BotUser.telegram_id, BotUser.balance)
                          .where(BotUser.id == t.user_id)).first()
    if row:
        session.info.setdefault("_chg_bonus", []).append({
            "bot_id": t.bot_id, "tg": int(row[0]), "bonus": bonus, "pct": pct,
            "amount": int(float(t.amount)), "balance": float(row[1] or 0)})


@event.listens_for(SyncSession, "after_flush")
def _after_flush(session, flush_context):
    try:
        topups = [o for o in session.new if isinstance(o, WalletTxn) and _is_topup(o)]
        for t in topups:
            _apply(session, t)
    except Exception:   # نباید شارژِ اصلی را خراب کند
        logger.exception("charge_bonus: اعمالِ بونوس ناموفق بود")


@event.listens_for(SyncSession, "after_commit")
def _after_commit(session):
    items = session.info.pop("_chg_bonus", None)
    if not items:
        return
    try:
        asyncio.get_running_loop().create_task(_notify(items))
    except RuntimeError:
        logger.debug("charge_bonus: حلقهٔ asyncio در دسترس نبود؛ پیامِ بونوس نرفت")


@event.listens_for(SyncSession, "after_rollback")
def _after_rollback(session):
    session.info.pop("_chg_bonus", None)


async def _notify(items: list[dict]) -> None:
    from config import Session
    for it in items:
        try:
            async with Session() as s:
                brow = await s.get(BotRow, it["bot_id"])
            if not brow:
                continue
            tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                await tbot.send_message(
                    it["tg"],
                    f"🎁 <b>بونوسِ شارژ!</b>\n\nبه‌خاطرِ شارژِ {it['amount']:,} تومانی، "
                    f"<b>{it['bonus']:,} تومان</b> ({it['pct']:g}٪) بونوس به کیف‌پولت اضافه شد.\n"
                    f"💰 موجودیِ فعلی: <b>{int(it['balance']):,} تومان</b>")
            finally:
                await tbot.session.close()
        except Exception:
            logger.info("charge_bonus: پیامِ بونوس برایِ %s نرفت", it.get("tg"))


# ───────────────────────── نمایش به کاربر ─────────────────────────

async def wallet_hint(bot_id: int) -> str:
    """یک خطِ معرفی در صفحهٔ کیف پول (خالی اگر خاموش است)."""
    from tenant import get_setting
    if await get_setting(bot_id, KEY_ON, "") != "1":
        return ""
    tiers = parse_tiers(await get_setting(bot_id, KEY_TIERS, ""))
    if not tiers:
        return ""
    lines = "\n".join(f"   ◂ شارژ {a:,} تومان به بالا: <b>{p:g}٪</b> بونوس" for a, p in tiers)
    en = parse_methods(await get_setting(bot_id, KEY_METHODS, ""))
    only = ("\n   ⚠️ فقط با: " + "، ".join(METHODS[m][0] for m in METHODS if m in en)) if en else ""
    return f"\n\n🎁 <b>بونوس شارژ</b>\n{lines}{only}"


# ───────────────────────── پنلِ مدیریت ─────────────────────────

class ChargeBonusFSM(StatesGroup):
    tiers = State()
    cap = State()


def _methods_text(en: set[str]) -> str:
    return "همهٔ روش‌ها" if not en else "، ".join(METHODS[m][0] for m in METHODS if m in en)


async def methods_page(bot_id: int):
    from tenant import get_setting
    en = parse_methods(await get_setting(bot_id, KEY_METHODS, ""))
    rows, row = [], []
    for key, (lbl, _k, _p) in METHODS.items():
        on = (not en) or key in en
        row.append(InlineKeyboardButton(text=("✅ " if on else "⬜️ ") + lbl, callback_data=f"cbn:m:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✅ همهٔ روش‌ها", callback_data="cbn:m:all", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:chgbonus", style=ButtonStyle.DANGER)])
    txt = ("🎛 <b>روش‌های پرداخت بونوس‌دار</b>\n\nفقط شارژهایی که با روش‌های علامت‌دار (✅) انجام شود بونوس می‌گیرند. "
           "هر روش را بزن تا روشن/خاموش شود.\n\n"
           f"الان: <b>{_methods_text(en)}</b>\n\n"
           "<i>اگر همه را خاموش کنی دوباره «همهٔ روش‌ها» فعال می‌شود (بونوس برای همه).</i>")
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def page(bot_id: int):
    from tenant import get_setting
    on = await get_setting(bot_id, KEY_ON, "") == "1"
    tiers = parse_tiers(await get_setting(bot_id, KEY_TIERS, ""))
    try:
        cap = int(float(await get_setting(bot_id, KEY_CAP, "0") or 0))
    except ValueError:
        cap = 0
    en_methods = parse_methods(await get_setting(bot_id, KEY_METHODS, ""))
    txt = (
        "🎁 <b>بونوسِ شارژِ کیف پول</b>\n\n"
        "وقتی کاربر کیف‌پولش را (با هر درگاهی) شارژ کند و مبلغ به یکی از پله‌هایِ زیر برسد، درصدِ بونوسِ "
        "همان پله هم خودکار به موجودی‌اش اضافه می‌شود و برایش پیام می‌آید.\n\n"
        f"وضعیت: <b>{'✅ روشن' if on else '❌ خاموش'}</b>\n\n"
        f"<b>پله‌ها:</b>\n{_pretty(tiers)}\n\n"
        f"سقف بونوس هر شارژ: <b>{f'{cap:,} تومان' if cap else 'بدون سقف'}</b>\n"
        f"روش‌های بونوس‌دار: <b>{_methods_text(en_methods)}</b>\n\n"
        "<i>مثال: شارژِ ۳۰۰٬۰۰۰ تومانی با پلهٔ «۳۰۰٬۰۰۰ ⇒ ۲۰٪» می‌شود ۳۶۰٬۰۰۰ تومان موجودی. "
        "بازگشتِ وجه، جایزهٔ بازی و تنظیمِ دستیِ ادمین بونوس نمی‌گیرند.</i>")
    rows = [
        [InlineKeyboardButton(text=("🔴 خاموش کردن" if on else "🟢 روشن کردن"), callback_data="cbn:toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="✏️ تنظیمِ پله‌ها", callback_data="cbn:tiers")],
        [InlineKeyboardButton(text="🔝 سقفِ بونوس", callback_data="cbn:cap")],
        [InlineKeyboardButton(text="🎛 روش‌های پرداخت بونوس‌دار", callback_data="cbn:methods", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb_or_msg, bot_id: int, edit: bool) -> None:
    txt, kb = await page(bot_id)
    m = cb_or_msg.message if hasattr(cb_or_msg, "message") else cb_or_msg
    if edit:
        try:
            await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            logger.debug("charge_bonus: ویرایشِ صفحه ممکن نشد؛ پیامِ تازه می‌فرستم")
    await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@charge_bonus_router.callback_query(F.data == "tadm:chgbonus")
async def tadm_chgbonus(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await _show(cb, row.id, edit=True)
    await cb.answer()


@charge_bonus_router.callback_query(F.data == "cbn:methods")
async def cbn_methods(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    txt, kb = await methods_page(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@charge_bonus_router.callback_query(F.data.regexp(r"^cbn:m:[a-z]+$"))
async def cbn_method_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.rsplit(":", 1)[1]
    en = parse_methods(await get_setting(row.id, KEY_METHODS, ""))
    if key == "all":
        en = set()
    elif key in METHODS:
        cur = en or set(METHODS)                 # «همه» ⇒ از مجموعهٔ کامل شروع کن
        cur = cur - {key} if key in cur else cur | {key}
        en = set() if (not cur or cur == set(METHODS)) else cur
    else:
        return await cb.answer()
    await set_setting(row.id, KEY_METHODS, ",".join(m for m in METHODS if m in en))
    txt, kb = await methods_page(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.debug("charge_bonus: ویرایش صفحهٔ روش‌ها ممکن نشد (بی‌اهمیت)")
    await cb.answer("ثبت شد")


@charge_bonus_router.callback_query(F.data == "cbn:toggle")
async def cbn_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, KEY_ON, "") == "1"
    if not cur and not parse_tiers(await get_setting(row.id, KEY_TIERS, "")):
        await cb.answer("اول پله‌ها را تنظیم کن.", show_alert=True)
        return
    await set_setting(row.id, KEY_ON, "" if cur else "1")
    await _show(cb, row.id, edit=True)
    await cb.answer("خاموش شد." if cur else "روشن شد.")


@charge_bonus_router.callback_query(F.data == "cbn:tiers")
async def cbn_tiers_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(ChargeBonusFSM.tiers)
    cur = tiers_to_text(parse_tiers(await get_setting(row.id, KEY_TIERS, "")))
    await cb.message.answer(
        "✏️ پله‌ها را بفرست؛ <b>هر خط یک پله</b>: «حداقلِ مبلغِ شارژ (تومان) = درصد».\n\n"
        "<code>300000=20\n1000000=25</code>\n\n"
        + (f"الان:\n<code>{cur}</code>\n\n" if cur else "")
        + "<i>برایِ انصراف «لغو» بنویس. برایِ حذفِ همهٔ پله‌ها «حذف» بنویس.</i>", parse_mode="HTML")
    await cb.answer()


@charge_bonus_router.message(ChargeBonusFSM.tiers, F.text)
async def cbn_tiers_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    t = (msg.text or "").strip()
    if t in {"لغو", "❌ لغو", "انصراف", "/cancel"}:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if t == "حذف":
        await set_setting(row.id, KEY_TIERS, "")
        await set_setting(row.id, KEY_ON, "")
    else:
        tiers = parse_tiers(t)
        if not tiers:
            await msg.answer("❌ قالب نامعتبر است. هر خط مثل <code>300000=20</code> باشد یا «لغو» بنویس.",
                             parse_mode="HTML")
            return
        await set_setting(row.id, KEY_TIERS, tiers_to_text(tiers))
    await state.clear()
    await msg.answer("✅ ثبت شد.")
    await _show(msg, row.id, edit=False)


@charge_bonus_router.callback_query(F.data == "cbn:cap")
async def cbn_cap_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(ChargeBonusFSM.cap)
    await cb.message.answer(
        "🔝 حداکثرِ بونوسِ هر شارژ را به تومان بفرست (مثلاً <code>200000</code>؛ ۰ یعنی بدونِ سقف).\n\n"
        "<i>برایِ انصراف «لغو» بنویس.</i>", parse_mode="HTML")
    await cb.answer()


@charge_bonus_router.message(ChargeBonusFSM.cap, F.text)
async def cbn_cap_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    t = (msg.text or "").strip()
    if t in {"لغو", "❌ لغو", "انصراف", "/cancel"}:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    d = re.sub(r"[^\d]", "", t.translate(_FA))
    if not d:
        await msg.answer("❌ عددِ معتبر بفرست (مثلاً 200000) یا «لغو» بنویس.")
        return
    await set_setting(row.id, KEY_CAP, str(int(d)))
    await state.clear()
    await msg.answer("✅ ثبت شد.")
    await _show(msg, row.id, edit=False)
