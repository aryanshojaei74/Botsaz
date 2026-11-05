"""vs_grace.py — «🤝 ارفاق خرید» برایِ ربات‌هایِ خدماتِ مجازی (PATCH_VS_GRACE_V1، درخواستِ SINA).

وقتی موجودیِ کاربر برایِ یک سفارش «کمی» کم است، فروشگاه اختلاف را پوشش می‌دهد و سفارش بدونِ نیاز به شارژِ اضافه ثبت می‌شود.
• پله‌ها (تا ۱۰ تا): «سفارش از X تومان به بالا ⇒ تا Y تومان کمبود را پوشش بده». بالاترین پله‌ای که سفارش به آن رسیده ملاک است.
• روشن/خاموش (پیش‌فرض خاموش).
• نحوهٔ اعمال: مبلغِ کسرشده از کیف پول = موجودی (کمبود از جیبِ فروشگاه) — در همهٔ مسیرهایِ پرداختِ کیف‌پول فقط با یک فراخوانی
  `price, _ = await apply(...)` قبل از چکِ موجودی؛ سفارش با قیمتِ کاهش‌یافته (= پرداختِ واقعی) ثبت می‌شود، پس بازگشتِ وجه هم عادلانه است.
• پیام: متغیرِ `{{grace}}` در متنِ «ثبت سفارش» (تنظیمِ متن) هر جا گذاشته شود، فقط وقتی ارفاق اعمال شده به‌صورتِ نقلِ‌قولِ جمع‌شونده
  نشان داده می‌شود و در غیرِ این صورت کاملاً خالی است. در پیامِ پیش‌فرض (بدونِ متنِ دلخواه) بلوک به انتهای پیام اضافه می‌شود.
"""
from __future__ import annotations

import html
import json
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
vs_grace_router = Router(name="vs_grace")

KEY_ON, KEY_TIERS, KEY_TEXT, KEY_TOTAL = "vs_grace_on", "vs_grace_tiers", "vs_grace_text", "vs_grace_total"
MAX_TIERS = 10
DEFAULT_TEXT = ("به‌خاطر همراهیِ شما، همین اختلاف را پوشش دادیم و سفارشتان بدونِ نیاز به شارژِ اضافه تأیید و انجام شد.")
_TTL = 600                                   # ثانیه: بلوکِ ارفاق تا این‌قدر بعد از اعمال برایِ پیامِ ثبتِ همان کاربر می‌ماند
_PENDING: dict[tuple[int, int], tuple[float, str]] = {}
_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


async def _gs(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _ss(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def _t(n) -> str:
    return f"{int(n):,} تومان"


async def load_tiers(bot_id: int) -> list[dict]:
    try:
        raw = json.loads(await _gs(bot_id, KEY_TIERS, "[]") or "[]")
    except ValueError:
        raw = []
    out = []
    for t in raw if isinstance(raw, list) else []:
        try:
            mn, mx = int(t["min"]), int(t["max"])
        except (KeyError, TypeError, ValueError):
            continue
        if mn >= 0 and mx > 0:
            out.append({"min": mn, "max": mx})
    return sorted(out, key=lambda x: x["min"])[:MAX_TIERS]


def pick_tier(tiers: list[dict], price: float) -> dict | None:
    """بالاترین پله‌ای که قیمتِ سفارش به حداقلش رسیده."""
    best = None
    for t in tiers:
        if price >= t["min"]:
            best = t
    return best


def build_block(text: str, invoice: int, balance: int, diff: int) -> str:
    body = (text or DEFAULT_TEXT).replace("{invoice}", f"{invoice:,}").replace("{balance}", f"{balance:,}").replace("{diff}", f"{diff:,}")
    return ("<blockquote expandable>🌟 <b>ارفاقِ ویژه برایِ شما</b>\n"
            f"💰 مبلغِ فاکتور: <b>{invoice:,}</b> تومان\n"
            f"👛 موجودیِ شما: <b>{balance:,}</b> تومان\n"
            f"📊 اختلاف: <b>{diff:,}</b> تومان\n\n"
            f"✔️ {html.escape(body, quote=False)}</blockquote>")


async def apply(bot_id: int, tg_id: int, balance, price):
    """اگر کمبودِ موجودی در محدودهٔ ارفاق است: (قیمتِ جدید = موجودی، True)؛ وگرنه (قیمتِ بدونِ تغییر، False).
    هرگز exception نمی‌دهد؛ هر خطا = بدونِ ارفاق."""
    try:
        if await _gs(bot_id, KEY_ON, "0") != "1":
            return price, False
        bal, pr = float(balance), float(price)
        short = pr - bal
        if short <= 0 or bal <= 0:
            return price, False
        tier = pick_tier(await load_tiers(bot_id), pr)
        if not tier or short > tier["max"]:
            return price, False
        text = await _gs(bot_id, KEY_TEXT, "")
        _PENDING[(bot_id, tg_id)] = (time.time(), build_block(text, int(round(pr)), int(bal), int(round(short))))
        if len(_PENDING) > 5000:
            for k in [k for k, (ts, _b) in _PENDING.items() if time.time() - ts > _TTL][:2000]:
                _PENDING.pop(k, None)
        try:
            tot = int(await _gs(bot_id, KEY_TOTAL, "0") or 0) + int(round(short))
            await _ss(bot_id, KEY_TOTAL, str(tot))
        except Exception:      # noqa: BLE001
            logger.debug("vs_grace: شمارندهٔ جمعِ ارفاق به‌روز نشد")
        logger.info("vs_grace: bot=%s tg=%s فاکتور=%s موجودی=%s ارفاق=%s", bot_id, tg_id, int(pr), int(bal), int(short))
        return (int(bal) if isinstance(price, int) else bal), True
    except Exception:      # noqa: BLE001
        logger.exception("vs_grace.apply ناموفق بود (بدونِ ارفاق ادامه می‌دهد)")
        return price, False


def peek_block(bot_id: int, tg_id: int | None) -> str:
    """بلوکِ ارفاقِ اخیرِ این کاربر (یا ""). مصرف نمی‌شود تا چند پیامِ پشتِ‌سرهم هم بتوانند استفاده کنند؛ TTL دارد."""
    if tg_id is None:
        return ""
    ent = _PENDING.get((bot_id, int(tg_id)))
    if not ent:
        return ""
    if time.time() - ent[0] > _TTL:
        _PENDING.pop((bot_id, int(tg_id)), None)
        return ""
    return ent[1]


def decorate(bot_id: int, chat_id: int, text: str, *, is_default_placed: bool) -> str:
    """در پیامِ خروجیِ «ثبت سفارش»: `{{grace}}`/`{grace}` ⇒ بلوک یا خالی؛ پیامِ پیش‌فرض ⇒ بلوک در انتها (اگر ارفاق اعمال شده)."""
    blk = peek_block(bot_id, chat_id)
    if "{{grace}}" in text or "{grace}" in text:
        return text.replace("{{grace}}", blk).replace("{grace}", blk)
    if is_default_placed and blk and blk not in text:
        return text + "\n\n" + blk
    return text


# ───────────────────────── پنلِ مدیریت ─────────────────────────

class GraceFSM(StatesGroup):
    t_min = State()
    t_max = State()
    text = State()


async def _owner(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return None
    return row


async def _menu_view(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    on = await _gs(bot_id, KEY_ON, "0") == "1"
    tiers = await load_tiers(bot_id)
    total = int(await _gs(bot_id, KEY_TOTAL, "0") or 0)
    lines = ["🤝 <b>ارفاق خرید</b>", "",
             "وقتی موجودیِ مشتری برایِ یک سفارش <b>کمی</b> کم باشد، شما اختلاف را پوشش می‌دهید و سفارش بدونِ نیاز به شارژِ اضافه ثبت می‌شود.",
             "", f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}",
             f"جمعِ ارفاق‌هایِ تا الان: <b>{total:,}</b> تومان", ""]
    if tiers:
        lines.append("<b>پله‌ها:</b>")
        for i, t in enumerate(tiers, 1):
            lines.append(f"{i}. سفارشِ <b>{t['min']:,}</b> تومان به بالا ⇒ تا <b>{t['max']:,}</b> تومان کمبود پوشش داده می‌شود")
    else:
        lines.append("هنوز پله‌ای نساخته‌ای؛ تا پله‌ای نباشد ارفاق اعمال نمی‌شود.")
    lines += ["", "📌 <b>نمایش در پیام:</b> متغیرِ <code>{{grace}}</code> را هر جایِ متنِ «ثبتِ سفارش» (از «تنظیمِ متن‌ها») بگذار؛ "
              "فقط وقتی ارفاق اعمال شده به‌صورتِ نقلِ‌قولِ جمع‌شونده نشان داده می‌شود و در غیرِ این صورت کاملاً نادیده است. "
              "اگر متنِ دلخواه نگذاشته باشی، بلوک خودکار به انتهای پیامِ ثبت اضافه می‌شود."]
    rows = [[InlineKeyboardButton(text=("🔴 خاموش‌کردن" if on else "🟢 روشن‌کردن"), callback_data="vsg:toggle",
                                  style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)]]
    for i, t in enumerate(tiers):
        rows.append([InlineKeyboardButton(text=f"🗑 پلهٔ {i + 1}: ≥{t['min']:,} ⇒ تا {t['max']:,}", callback_data=f"vsg:del:{i}",
                                          style=ButtonStyle.DANGER)])
    if len(tiers) < MAX_TIERS:
        rows.append([InlineKeyboardButton(text=f"➕ افزودنِ پله ({len(tiers)}/{MAX_TIERS})", callback_data="vsg:add",
                                          style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="✏️ متنِ ارفاق", callback_data="vsg:text", style=ButtonStyle.PRIMARY),
                 InlineKeyboardButton(text="👁 پیش‌نمایش", callback_data="vsg:preview", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def vsg_menu_entry(cb, bot: Bot):
    """ورودیِ مشترکِ حالتِ شیشه‌ای و کیبوردی (_FakeCb)."""
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await _menu_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@vs_grace_router.callback_query(F.data == "vsg:menu")
async def vsg_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await vsg_menu_entry(cb, bot)


@vs_grace_router.callback_query(F.data == "vsg:toggle")
async def vsg_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _gs(row.id, KEY_ON, "0")
    await _ss(row.id, KEY_ON, "0" if cur == "1" else "1")
    await cb.answer("تغییر کرد")
    await vsg_menu_entry(cb, bot)


@vs_grace_router.callback_query(F.data.startswith("vsg:del:"))
async def vsg_del(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    tiers = await load_tiers(row.id)
    try:
        tiers.pop(int(cb.data.split(":")[2]))
    except (ValueError, IndexError):
        return await cb.answer("این پله دیگر نیست.", show_alert=True)
    await _ss(row.id, KEY_TIERS, json.dumps(tiers))
    await cb.answer("🗑 حذف شد")
    await vsg_menu_entry(cb, bot)


@vs_grace_router.callback_query(F.data == "vsg:add")
async def vsg_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    if len(await load_tiers(row.id)) >= MAX_TIERS:
        return await cb.answer(f"حداکثر {MAX_TIERS} پله.", show_alert=True)
    await state.set_state(GraceFSM.t_min)
    await cb.message.answer("➕ <b>پلهٔ جدید</b>\n\n۱/۲ — سفارش‌هایِ <b>از چند تومان به بالا</b> شامل این پله شوند؟ (فقط عدد؛ مثلاً 100000)",
                            parse_mode="HTML")
    await cb.answer()


def _num(txt: str | None) -> int | None:
    t = (txt or "").translate(_DIG).replace(",", "").replace("،", "").strip()
    return int(t) if t.isdigit() and len(t) <= 12 else None


@vs_grace_router.message(GraceFSM.t_min, F.text)
async def vsg_t_min(msg: Message, bot: Bot, state: FSMContext):
    n = _num(msg.text)
    if n is None:
        return await msg.answer("❌ فقط عدد بفرست (مثلاً 100000).")
    await state.update_data(mn=n)
    await state.set_state(GraceFSM.t_max)
    await msg.answer(f"۲/۲ — برایِ سفارش‌هایِ ≥ {n:,} تومان، <b>حداکثر چند تومان کمبود</b> را پوشش بدهم؟ (مثلاً 1000)", parse_mode="HTML")


@vs_grace_router.message(GraceFSM.t_max, F.text)
async def vsg_t_max(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    n = _num(msg.text)
    if n is None or n <= 0:
        return await msg.answer("❌ عددِ معتبرِ بزرگ‌تر از صفر بفرست.")
    mn = int((await state.get_data()).get("mn", 0))
    tiers = [t for t in await load_tiers(row.id) if t["min"] != mn]       # هم‌حدِ قبلی جایگزین می‌شود
    tiers.append({"min": mn, "max": n})
    tiers = sorted(tiers, key=lambda x: x["min"])[:MAX_TIERS]
    await _ss(row.id, KEY_TIERS, json.dumps(tiers))
    await state.clear()
    text, kb = await _menu_view(row.id)
    await msg.answer("✅ پله ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@vs_grace_router.callback_query(F.data == "vsg:text")
async def vsg_text(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(GraceFSM.text)
    await cb.message.answer(
        "✏️ متنِ ارفاق را بفرست (همان جمله‌ای که داخلِ نقلِ‌قول زیرِ ✔️ می‌آید).\n"
        "می‌توانی از <code>{invoice}</code> (مبلغ فاکتور)، <code>{balance}</code> (موجودیِ مشتری) و <code>{diff}</code> (اختلاف) استفاده کنی.\n"
        "برایِ بازگشت به متنِ پیش‌فرض بنویس <code>-</code>", parse_mode="HTML")
    await cb.answer()


@vs_grace_router.message(GraceFSM.text, F.text)
async def vsg_text_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    t = (msg.text or "").strip()
    await _ss(row.id, KEY_TEXT, "" if t == "-" else t[:600])
    await state.clear()
    text, kb = await _menu_view(row.id)
    await msg.answer("✅ متن ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@vs_grace_router.callback_query(F.data == "vsg:preview")
async def vsg_preview(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    blk = build_block(await _gs(row.id, KEY_TEXT, ""), 110700, 110000, 700)
    await cb.message.answer("👁 <b>نمونهٔ پیامی که مشتری می‌بیند:</b>\n\n" + blk, parse_mode="HTML")
    await cb.answer()
