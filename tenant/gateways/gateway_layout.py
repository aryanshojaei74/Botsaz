"""
gateway_layout.py — «🎛 نام و ترتیبِ دکمه‌هایِ پرداخت» (PATCH_GATEWAY_LAYOUT_V1).

درخواستِ مالک‌ها: اسمِ دکمهٔ درگاه (مثلاً زرین‌پال) را عوض کنند و دکمه‌هایِ پرداخت را
جابه‌جا کنند. مسیر: پنل مدیریت ← مالی و کیف پول ← 🎛 نام و ترتیبِ دکمه‌هایِ پرداخت.

  • ترتیب در تنظیمِ gw_order (کلیدهایِ درگاه با کاما) ذخیره می‌شود؛ خالی = ترتیبِ پیش‌فرض.
  • اسم در همان کلیدِ «<درگاه>_display_name» (GATEWAY_LABELS) که بقیهٔ کد از قبل می‌خواند.
  • ترتیب هم در صفحهٔ «کیف پول» و هم در دکمه‌هایِ شارژِ داخلِ صفحهٔ خرید اعمال می‌شود؛
    فقط جایِ دکمه‌هایِ درگاه با هم عوض می‌شود، بقیهٔ دکمه‌ها (تاریخچه، احراز و …) سرِ جایشان می‌مانند.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
gateway_layout_router = Router(name="gateway_layout")

# (کلید، callback_data دکمهٔ کیف پول، عنوان، قابلِ تغییرِ نام؟)
ITEMS: list[tuple[str, str, str, bool]] = [
    ("card",        "w:charge",      "💳 کارت‌به‌کارت",   True),
    ("zarinpal",    "w:zarinpal",    "🔐 زرین‌پال",       True),
    ("zibal",       "w:zibal",       "🔐 زیبال",          True),
    ("uniquepay",   "w:uniquepay",   "💳 یونیک‌پی",       True),
    ("apay",        "w:apay",        "💳 آپی",            True),
    ("abangateway", "w:abangateway", "💳 آبان گیت‌وی",    True),
    ("hooshpay",    "w:hooshpay",    "💳 هوش‌پی",         True),     # PATCH_HOOSHPAY_GATEWAY_V1
    ("blupal",      "w:blupal",      "💳 بلوپال",         True),     # PATCH_BLUPAL_TENANT_V1
    ("tonpays",     "w:tonpays",     "💠 تون‌پیز",        True),
    ("hesabpay",    "w:hesabpay",    "🇦🇫 حساب‌پی",       False),
    ("swap",        "w:swap",        "🟦 سواپ‌ولت",       True),
    ("crypto",      "w:crypto",      "🪙 ارز دیجیتال",    False),
    ("cdirect",     "w:cdirect",     "💎 واریز مستقیم ارز", True),     # PATCH_CDIRECT_LAYOUT_V1 (TON/ترون/تتر؛ هر سه شکلِ دکمه)
    ("stars",       "w:stars",       "⭐ استارز",         False),
]
_BY_KEY = {k: (cb, title, ren) for k, cb, title, ren in ITEMS}
_KEY_BY_CB = {cb: k for k, cb, _t, _r in ITEMS}


class GwLayoutFSM(StatesGroup):
    name = State()


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def get_order(bot_id: int) -> list[str]:
    """ترتیبِ فعلی: آنچه مالک ذخیره کرده + درگاه‌هایِ جدید/ذخیره‌نشده در انتها با ترتیبِ پیش‌فرض."""
    raw = (await _get(bot_id, "gw_order", "") or "").strip()
    saved = [k for k in raw.split(",") if k in _BY_KEY]
    seen = set(saved)
    return saved + [k for k, *_ in ITEMS if k not in seen]


def _reorder(seq: list, cb_of, order: list[str]) -> list:
    """فقط اعضایِ «درگاه» را طبقِ order مرتب می‌کند و در همان جایگاه‌هایِ قبلی می‌گذارد."""
    idxs = [i for i, it in enumerate(seq) if _gw_key_of_cb(cb_of(it) or "") is not None]
    if len(idxs) < 2:
        return seq
    rank = {k: n for n, k in enumerate(order)}
    gw_items = sorted((seq[i] for i in idxs), key=lambda it: rank.get(_gw_key_of_cb(cb_of(it) or ""), 999))
    out = list(seq)
    for i, it in zip(idxs, gw_items):
        out[i] = it
    return out


async def apply_order_options(bot_id: int, options: list) -> list:
    """options = [(label, callback_data, style), ...] (صفحهٔ کیف پول)."""
    try:
        if not (await _get(bot_id, "gw_order", "") or "").strip():
            return options
        return _reorder(options, lambda it: it[1], await get_order(bot_id))
    except Exception:
        logger.exception("gateway_layout: اعمالِ ترتیب روی کیف پول ناموفق بود")
        return options


async def apply_order_rows(bot_id: int, rows: list) -> list:
    """rows = [[InlineKeyboardButton], ...] (دکمه‌هایِ شارژِ داخلِ صفحهٔ خرید)."""
    try:
        if not (await _get(bot_id, "gw_order", "") or "").strip():
            return rows

        def _cb(r):
            try:
                c = r[0].callback_data or ""
            except Exception:
                return ""
            return c

        return _reorder(rows, _cb, await get_order(bot_id))
    except Exception:
        logger.exception("gateway_layout: اعمالِ ترتیب روی دکمه‌هایِ شارژ ناموفق بود")
        return rows


async def gw_custom_label(bot_id: int, key: str, default_label: str) -> str:
    """اگر مالک اسمِ دلخواه گذاشته همان را *عیناً* برمی‌گرداند، وگرنه برچسبِ پیش‌فرضِ خودِ صفحه.

    عیناً، چون دیسپچرِ حالتِ کیبوردی (gw_label_set) همین شکل را می‌شناسد؛ اگر ایموجی یا
    قالبِ دیگری اضافه شود، تپ‌کردنِ دکمه در حالتِ کیبوردی بی‌واکنش می‌ماند."""
    from tenant import GATEWAY_LABELS
    _d, setting_key = GATEWAY_LABELS.get(key, (key, f"{key}_display_name"))
    custom = ((await _get(bot_id, setting_key, "")) or "").strip()[:64]
    return custom or default_label


# === PATCH_GATEWAY_COLORS_V1 === (فرزاد) رنگِ دکمهٔ هر درگاه را مالک انتخاب کند
_COLOR_CYCLE = ["", "success", "primary", "danger"]
_COLOR_EMOJI = {"": "⚪️", "success": "🟢", "primary": "🔵", "danger": "🔴"}


# === PATCH_CARD_RENAME_COLOR_V1 === (MidnightSoul) کارت‌به‌کارت هم اسم و رنگِ دلخواه بگیرد؛ دکمه‌هایِ کارتِ صفحه‌هایِ خرید
CARD_CB_PREFIXES = ("pay:card:", "vpay:card:", "shop:buy:card:", "gift:cardpay:", "star:cardpay:",
                    "vc:cardpay:", "mcfg:paycard:")


def _is_card_cb(cb: str) -> bool:
    return (cb or "").startswith(CARD_CB_PREFIXES)


def has_gateway_buttons(markup) -> bool:
    """آیا این کیبورد دکمهٔ درگاه/کارت‌به‌کارت دارد (برایِ فعال‌شدنِ هوکِ رنگ/نام/ترتیب در لایهٔ ارسال)؟"""
    try:
        return any((b.callback_data or "").startswith("w:") or _is_card_cb(b.callback_data or "")
                   or _is_cdirect_cb(b.callback_data or "") for r in markup.inline_keyboard for b in r)
    except Exception:
        return False


def _is_cdirect_cb(cb: str) -> bool:
    """واریز مستقیمِ ارز: w:cdirect (انتخابِ ارز)، w:ton_direct، trxd:ent:{ارز} (وقتی فقط یک ارز فعال است)."""
    return cb in ("w:cdirect", "w:ton_direct") or (cb or "").startswith("trxd:ent:")


def _gw_key_of_cb(cb: str) -> str | None:
    if _is_card_cb(cb):
        return "card"
    if _is_cdirect_cb(cb):
        return "cdirect"
    for key, base, _t, _r in ITEMS:
        if cb == base or cb.startswith(base + ":"):
            return key
    return None


async def apply_gateway_colors(bot_id: int, markup):
    """دکمه‌هایِ درگاه (w:apay، w:zarinpal، …) را با رنگِ انتخابِ مالک می‌سازد؛ اگر رنگی تنظیم نشده یا پلنِ ربات
    «دکمهٔ رنگی» ندارد همان markupِ ورودی (همان شیء) برمی‌گردد."""
    try:
        from aiogram.types import InlineKeyboardMarkup as _IKM
        if not isinstance(markup, _IKM):
            return markup
        hits = [(i, j, _gw_key_of_cb(b.callback_data or "")) for i, r in enumerate(markup.inline_keyboard)
                for j, b in enumerate(r) if b.callback_data and (b.callback_data.startswith("w:") or _is_card_cb(b.callback_data)
                                                                   or _is_cdirect_cb(b.callback_data))]
        hits = [h for h in hits if h[2]]
        if not hits:
            return markup
        from tenant import _ibtn_colored_allowed
        if not await _ibtn_colored_allowed(bot_id):
            return markup
        from tenant import _IBTN_COLOR_STYLE
        colors = {}
        for key in {h[2] for h in hits}:
            v = ((await _get(bot_id, f"gw_color_{key}", "")) or "").strip()
            if v in _IBTN_COLOR_STYLE:
                colors[key] = _IBTN_COLOR_STYLE[v]
        if not colors:
            return markup
        rows = [list(r) for r in markup.inline_keyboard]
        for i, j, key in hits:
            if key in colors:
                rows[i][j] = rows[i][j].model_copy(update={"style": colors[key]})
        return _IKM(inline_keyboard=rows)
    except Exception:
        logger.exception("gateway_layout: اعمالِ رنگِ درگاه‌ها ناموفق بود")
        return markup


# === PATCH_GATEWAY_DIRECT_FIRST_V1 === (فرزاد) دکمهٔ پرداختِ مستقیمِ درگاه (مثلاً «کارت به کارت آنی») بالایِ «پرداخت از کیف پول»
def _is_direct_gw_cb(cb: str) -> bool:
    """w:apay:vpn:5:1000 / w:apay:12 → پرداختِ مستقیمِ همین سفارش؛ خودِ w:apay (شارژِ کیف پول) نه."""
    key = _gw_key_of_cb(cb or "")
    return bool(key) and key != "cdirect" and (cb or "").startswith("w:") and cb != _BY_KEY[key][0]


async def apply_direct_first(bot_id: int, markup):
    """اگر مالک روشن کرده باشد، ردیف‌هایِ پرداختِ مستقیمِ درگاه (به ترتیبِ gw_order) می‌روند بالایِ صفحهٔ خرید."""
    try:
        from aiogram.types import InlineKeyboardMarkup as _IKM
        if not isinstance(markup, _IKM):
            return markup
        rows = [list(r) for r in markup.inline_keyboard]
        idx = [i for i, r in enumerate(rows) if any(_is_direct_gw_cb(b.callback_data or "") for b in r)]
        if not idx or idx == list(range(len(idx))):
            return markup
        if (await _get(bot_id, "gw_direct_first", "0")) != "1":
            return markup
        rank = {k: n for n, k in enumerate(await get_order(bot_id))}
        direct = sorted((rows[i] for i in idx), key=lambda r: rank.get(
            next((_gw_key_of_cb(b.callback_data or "") for b in r if _is_direct_gw_cb(b.callback_data or "")), ""), 999))
        rest = [r for i, r in enumerate(rows) if i not in set(idx)]
        return _IKM(inline_keyboard=direct + rest)
    except Exception:
        logger.exception("gateway_layout: بالا آوردنِ دکمهٔ پرداختِ مستقیم ناموفق بود")
        return markup


# === PATCH_GATEWAY_PAIR_ROWS_V1 === (Nexivo) دو درگاه کنارِ هم در یک ردیف، نه هر درگاه یک ردیف
def pair_rows(rows: list[list], flags: dict[str, bool]) -> list[list]:
    """ردیفِ تک‌دکمه‌ایِ درگاهِ «جفت‌شده» را به ردیفِ درگاهِ تک‌دکمه‌ایِ دقیقاً قبلی می‌چسباند (حداکثر دو دکمه در ردیف).
    فقط ردیف‌هایِ متوالیِ درگاه؛ ردیفِ غیرِدرگاه زنجیره را می‌شکند."""
    out: list[list] = []
    prev_ok = False                       # آخرین ردیفِ خروجی یک درگاهِ تک‌دکمه‌ایِ قابلِ‌چسبیدن است؟
    for r in rows:
        key = None
        if len(r) == 1:
            key = _gw_key_of_cb(getattr(r[0], "callback_data", None) or "")
        if key is None:
            out.append(list(r))
            prev_ok = False
            continue
        if flags.get(key) and prev_ok and len(out[-1]) == 1:
            out[-1] = out[-1] + list(r)
            prev_ok = False               # ردیف پر شد (۲ دکمه)
        else:
            out.append(list(r))
            prev_ok = True
    return out


async def apply_pairs(bot_id: int, markup):
    try:
        from aiogram.types import InlineKeyboardMarkup as _IKM
        if not isinstance(markup, _IKM):
            return markup
        keys = {_gw_key_of_cb(b.callback_data or "") for r in markup.inline_keyboard for b in r} - {None}
        flags = {}
        for k in keys:
            flags[k] = (await _get(bot_id, f"gw_pair_{k}", "")) == "1"
        if not any(flags.values()):
            return markup
        rows = pair_rows([list(r) for r in markup.inline_keyboard], flags)
        return _IKM(inline_keyboard=rows)
    except Exception:
        logger.exception("gateway_layout: جفت‌کردنِ دکمه‌هایِ درگاه ناموفق بود")
        return markup


async def apply_card_rename(bot_id: int, markup):
    """اسمِ دلخواهِ «کارت‌به‌کارت» (card_display_name) را عیناً رویِ دکمه‌هایِ کارتِ صفحه‌هایِ خرید/کیف پول می‌گذارد."""
    try:
        from aiogram.types import InlineKeyboardMarkup as _IKM
        if not isinstance(markup, _IKM):
            return markup
        def _kind(cb: str) -> str | None:
            if cb == "w:charge" or _is_card_cb(cb):
                return "card"
            return "cdirect" if _is_cdirect_cb(cb) else None
        kinds = {_kind(b.callback_data or "") for r in markup.inline_keyboard for b in r} - {None}
        if not kinds:
            return markup
        names = {}
        for k in kinds:
            v = ((await _get(bot_id, f"{k}_display_name", "")) or "").strip()[:64]
            if v:
                names[k] = v
        if not names:
            return markup
        rows = [[b.model_copy(update={"text": names[_kind(b.callback_data or "")]})
                 if _kind(b.callback_data or "") in names else b for b in r] for r in markup.inline_keyboard]
        return _IKM(inline_keyboard=rows)
    except Exception:
        logger.exception("gateway_layout: اعمالِ اسمِ کارت‌به‌کارت ناموفق بود")
        return markup


# ═══════════════════ صفحهٔ مدیریت ═══════════════════

async def _menu_content(bot_id: int):
    """هر درگاه دو ردیف: ردیفِ اول فقط نامِ درگاه (به رنگِ انتخابی)، ردیفِ دوم ابزارها: ⬆️ ⬇️ 🏷 🎨."""
    from tenant import GATEWAY_LABELS, _IBTN_COLOR_STYLE
    order = await get_order(bot_id)
    text = ("🎛 <b>نام و ترتیبِ دکمه‌هایِ پرداخت</b>\n\n"
            "زیرِ نامِ هر درگاه:\n"
            "⬆️ ⬇️ جابه‌جایی  •  🏷 تغییرِ اسم  •  🎨 تغییرِ رنگ  •  ↔️ کنارِ درگاهِ قبلی (دو درگاه در یک ردیف)\n\n"
            "🔝 با روشن‌کردنِ گزینهٔ پایین، در صفحهٔ خرید دکمهٔ پرداختِ مستقیمِ درگاه (مثلاً «کارت به کارت آنی») "
            "بالایِ «پرداخت از کیف پول» می‌آید.\n"
            "رنگ فقط در پلن‌هایِ دارایِ «دکمهٔ رنگی» اعمال می‌شود. ترتیب در «کیف پول» و دکمه‌هایِ شارژِ صفحهٔ خرید "
            "دیده می‌شود؛ درگاهِ غیرفعال به کاربر نشان داده نمی‌شود.")
    rows = []
    for n, key in enumerate(order):
        cb, title, renamable = _BY_KEY[key]
        shown = title
        if renamable:
            _d, sk = GATEWAY_LABELS.get(key, (key, f"{key}_display_name"))
            cur = ((await _get(bot_id, sk, "")) or "").strip()
            if cur:
                shown = f"{title} ← «{cur}»"
        col = ((await _get(bot_id, f"gw_color_{key}", "")) or "").strip()
        head = {"text": f"{n + 1}. {shown}", "callback_data": "gwl:noop"}
        if col in _IBTN_COLOR_STYLE:
            head["style"] = _IBTN_COLOR_STYLE[col]
        rows.append([InlineKeyboardButton(**head)])
        tools = [InlineKeyboardButton(text="⬆️", callback_data=f"gwl:up:{key}"),
                 InlineKeyboardButton(text="⬇️", callback_data=f"gwl:dn:{key}")]
        if renamable:
            tools.append(InlineKeyboardButton(text="🏷 اسم", callback_data=f"gwl:name:{key}"))
        tools.append(InlineKeyboardButton(text=f"🎨 رنگ {_COLOR_EMOJI.get(col, '⚪️')}", callback_data=f"gwl:col:{key}"))
        _pair_on = (await _get(bot_id, f"gw_pair_{key}", "")) == "1"
        if n > 0:                                         # اولین درگاه «قبلی» ندارد
            tools.append(InlineKeyboardButton(text=f"↔️ کنارِ قبلی {'✅' if _pair_on else '⬜️'}",
                                              callback_data=f"gwl:pair:{key}",
                                              style=ButtonStyle.SUCCESS if _pair_on else None))
        rows.append(tools)
    _first = (await _get(bot_id, "gw_direct_first", "0")) == "1"
    rows.append([InlineKeyboardButton(
        text=f"🔝 پرداختِ مستقیمِ درگاه بالایِ «پرداخت از کیف پول»: {'✅' if _first else '❌'}",
        callback_data="gwl:first")])
    rows.append([InlineKeyboardButton(text="♻️ برگشت به ترتیب، نام و رنگِ پیش‌فرض", callback_data="gwl:reset",
                                      style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance",
                                      style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb: CallbackQuery, bot_id: int) -> None:
    text, kb = await _menu_content(bot_id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def gwl_menu(cb: CallbackQuery, bot: Bot, state: FSMContext | None = None):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if state is not None:
        await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@gateway_layout_router.callback_query(F.data == "gwl:menu")
async def gwl_menu_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await gwl_menu(cb, bot, state)


@gateway_layout_router.callback_query(F.data == "gwl:noop")
async def gwl_noop(cb: CallbackQuery):
    await cb.answer()


@gateway_layout_router.callback_query(F.data.regexp(r"^gwl:(up|dn):"))
async def gwl_move(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, direction, key = cb.data.split(":", 2)
    order = await get_order(row.id)
    if key not in order:
        await cb.answer()
        return
    i = order.index(key)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
        await set_setting(row.id, "gw_order", ",".join(order))
    await _show(cb, row.id)
    await cb.answer()


@gateway_layout_router.callback_query(F.data.startswith("gwl:col:"))
async def gwl_color(cb: CallbackQuery, bot: Bot):
    """PATCH_GATEWAY_COLORS_V1 — چرخشِ رنگ: بی‌رنگ ← سبز ← آبی ← قرمز."""
    from tenant import _owner_ok, set_setting, _ibtn_colored_allowed
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if key not in _BY_KEY:
        return await cb.answer()
    if not await _ibtn_colored_allowed(row.id):
        return await cb.answer("🔒 رنگِ دکمه‌ها در پلنِ فعلیِ ربات فعال نیست.", show_alert=True)
    cur = ((await _get(row.id, f"gw_color_{key}", "")) or "").strip()
    nxt = _COLOR_CYCLE[(_COLOR_CYCLE.index(cur) + 1) % len(_COLOR_CYCLE)] if cur in _COLOR_CYCLE else "success"
    await set_setting(row.id, f"gw_color_{key}", nxt)
    await _show(cb, row.id)
    await cb.answer(f"رنگ: {_COLOR_EMOJI[nxt]}")


@gateway_layout_router.callback_query(F.data.startswith("gwl:pair:"))
async def gwl_pair(cb: CallbackQuery, bot: Bot):
    """PATCH_GATEWAY_PAIR_ROWS_V1 — این درگاه کنارِ درگاهِ بالاییِ خودش (در یک ردیف) نمایش داده شود/نشود."""
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if key not in _BY_KEY:
        return await cb.answer()
    new = "0" if (await _get(row.id, f"gw_pair_{key}", "")) == "1" else "1"
    await set_setting(row.id, f"gw_pair_{key}", new)
    await _show(cb, row.id)
    await cb.answer("کنارِ درگاهِ قبلی ✅" if new == "1" else "ردیفِ جدا ⬜️")


@gateway_layout_router.callback_query(F.data == "gwl:first")
async def gwl_first(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    new = "0" if (await _get(row.id, "gw_direct_first", "0")) == "1" else "1"
    await set_setting(row.id, "gw_direct_first", new)
    await _show(cb, row.id)
    await cb.answer("روشن شد ✅" if new == "1" else "خاموش شد ❌")


@gateway_layout_router.callback_query(F.data == "gwl:reset")
async def gwl_reset(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting, GATEWAY_LABELS
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await set_setting(row.id, "gw_order", "")
    await set_setting(row.id, "gw_direct_first", "0")
    for key, _cb, _t, renamable in ITEMS:
        await set_setting(row.id, f"gw_color_{key}", "")       # PATCH_GATEWAY_COLORS_V1
        await set_setting(row.id, f"gw_pair_{key}", "")        # PATCH_GATEWAY_PAIR_ROWS_V1
        if renamable:
            _d, sk = GATEWAY_LABELS.get(key, (key, f"{key}_display_name"))
            await set_setting(row.id, sk, "")
    await _show(cb, row.id)
    await cb.answer("به پیش‌فرض برگشت.")


@gateway_layout_router.callback_query(F.data.startswith("gwl:name:"))
async def gwl_name_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if key not in _BY_KEY or not _BY_KEY[key][2]:
        await cb.answer()
        return
    await state.set_state(GwLayoutFSM.name)
    await state.update_data(gwl_key=key)
    await cb.message.answer(
        f"🏷 اسمِ جدیدِ دکمهٔ <b>{_BY_KEY[key][1]}</b> را بفرست (حداکثر ۴۰ حرف، برایِ کارت‌به‌کارت و واریزِ مستقیم ۶۴؛ همان‌طور که "
        "نوشتی نمایش داده می‌شود).\nبرایِ برگشت به اسمِ پیش‌فرض: <code>پیش‌فرض</code>",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@gateway_layout_router.message(GwLayoutFSM.name, F.text)
async def gwl_name_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting, GATEWAY_LABELS
    row = await _owner_ok_msg(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    key = data.get("gwl_key")
    if key not in _BY_KEY:
        return
    txt = (msg.text or "").strip()
    _d, sk = GATEWAY_LABELS.get(key, (key, f"{key}_display_name"))
    from tenant import is_default_word
    if is_default_word(txt):
        await set_setting(row.id, sk, "")
        await msg.answer("✅ به اسمِ پیش‌فرض برگشت.")
    elif not txt:
        await msg.answer("اسمِ نامعتبر.")
        return
    else:
        _cap = 64 if key in ("card", "cdirect") else 40     # کارت‌به‌کارت اسمِ بلندتر (مثلاً «… تأیید آنی و خودکار با هوش مصنوعی») می‌خواهد
        await set_setting(row.id, sk, txt[:_cap])
        await msg.answer(f"✅ اسمِ دکمه روی «{txt[:_cap]}» تنظیم شد.")
    await msg.answer("برگشت به صفحهٔ نام و ترتیب:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎛 نام و ترتیبِ دکمه‌ها", callback_data="gwl:menu")]]))
