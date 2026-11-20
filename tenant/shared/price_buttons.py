"""price_buttons.py — دکمه‌های شیشه‌ای زیر پیام قیمت (PATCH_PRICE_BUTTONS_EDITOR_V1).

ذخیره به‌صورت متن ساده (سازگار با قبل): هر خط یک ردیف، دکمه‌ها با «&&» جدا، هر دکمه «متن | لینک» یا «متن | لینک | رنگ».
رنگ: blue / green / red (یا آبی / سبز / قرمز). ویرایشگر دکمه‌ای (افزودن لینک یا دکمهٔ صفحهٔ اصلی، جابه‌جایی، رنگ،
کنار هم/ردیف جدا، حذف) هم برای ربات‌های مشتری و هم برای گروه بات‌ساز اصلی از همین توابع استفاده می‌کند.
"""
from __future__ import annotations

import re

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

MAX_ROWS, MAX_PER_ROW, MAX_BTNS = 8, 3, 20
COLORS = ("", "primary", "success", "danger")
COLOR_FA = {"": "بی‌رنگ", "primary": "آبی", "success": "سبز", "danger": "قرمز"}
COLOR_EMOJI = {"": "⚪️", "primary": "🔵", "success": "🟢", "danger": "🔴"}
_COLOR_IN = {"blue": "primary", "آبی": "primary", "primary": "primary", "green": "success", "سبز": "success",
             "success": "success", "red": "danger", "قرمز": "danger", "danger": "danger"}
_URL = re.compile(r"^(https?://|tg://)\S+$")


def valid_url(u: str) -> bool:
    return bool(_URL.match((u or "").strip()))


def parse(raw: str) -> list[list[list[str]]]:
    """⇒ ردیف‌ها؛ هر دکمه [متن، لینک، رنگ]."""
    rows = []
    for line in (raw or "").splitlines():
        row = []
        for part in line.split("&&"):
            bits = [x.strip() for x in part.split("|")]
            if len(bits) < 2 or not bits[0] or not valid_url(bits[1]):
                continue
            color = _COLOR_IN.get(bits[2].lower(), "") if len(bits) > 2 else ""
            row.append([bits[0][:40], bits[1][:300], color])
        if row:
            rows.append(row[:MAX_PER_ROW])
    return rows[:MAX_ROWS]


def dump(rows) -> str:
    def one(b):
        t, u = b[0], b[1]
        c = {"primary": "blue", "success": "green", "danger": "red"}.get(b[2] if len(b) > 2 else "", "")
        return f"{t} | {u}" + (f" | {c}" if c else "")
    return "\n".join(" && ".join(one(b) for b in r) for r in rows if r)


def keyboard(rows) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    out = []
    for r in rows:
        line = []
        for b in r:
            kw = {"style": ButtonStyle(b[2])} if len(b) > 2 and b[2] else {}
            line.append(InlineKeyboardButton(text=b[0], url=b[1], **kw))
        out.append(line)
    return InlineKeyboardMarkup(inline_keyboard=out)


# ───────── مدل تخت برای ویرایش: هر دکمه [متن، لینک، رنگ، کنار_قبلی] ─────────
def flat(rows) -> list[list]:
    out = []
    for r in rows:
        for j, b in enumerate(r):
            out.append([b[0], b[1], b[2] if len(b) > 2 else "", j > 0])
    return out


def unflat(items) -> list[list[list[str]]]:
    rows: list[list[list[str]]] = []
    for t, u, c, join in items:
        if join and rows and len(rows[-1]) < MAX_PER_ROW:
            rows[-1].append([t, u, c])
        else:
            rows.append([[t, u, c]])
    return rows[:MAX_ROWS]


def apply(rows, action: str, i: int):
    """up / down / color / join / del ⇒ ردیف‌های جدید."""
    items = flat(rows)
    if not 0 <= i < len(items):
        return rows
    if action == "up" and i > 0:
        items[i - 1], items[i] = items[i], items[i - 1]
    elif action == "down" and i < len(items) - 1:
        items[i + 1], items[i] = items[i], items[i + 1]
    elif action == "color":
        items[i][2] = COLORS[(COLORS.index(items[i][2]) + 1) % len(COLORS)] if items[i][2] in COLORS else ""
    elif action == "join" and i > 0:
        items[i][3] = not items[i][3]
    elif action == "del":
        items.pop(i)
    if items:
        items[0][3] = False
    return unflat(items)


def add(rows, text: str, url: str, color: str = "", same_row: bool = False):
    items = flat(rows)
    if len(items) >= MAX_BTNS:
        return rows, False
    items.append([text[:40], url[:300], color, bool(same_row and items)])
    new = unflat(items)
    return new, sum(len(r) for r in new) == len(items)


def editor(rows, prefix: str, back_cb: str, main_buttons: bool) -> tuple[str, InlineKeyboardMarkup]:
    items = flat(rows)
    lines = ["🔘 <b>دکمه‌های زیر پیام قیمت</b>", ""]
    if not items:
        lines.append("هنوز دکمه‌ای اضافه نشده.")
    for n, (t, u, c, join) in enumerate(items, 1):
        lines.append(f"{n}. {COLOR_EMOJI.get(c, '⚪️')} {t}{'  ↔️ کنار قبلی' if join else ''}")
    lines += ["", "⬆️⬇️ جابه‌جایی · 🎨 رنگ (بی‌رنگ، آبی، سبز، قرمز) · ↔️ کنار دکمهٔ قبلی/ردیف جدا · 🗑 حذف",
              f"حداکثر {MAX_BTNS} دکمه، {MAX_ROWS} ردیف و {MAX_PER_ROW} دکمه در هر ردیف."]
    kb = []
    for i, (t, _u, c, join) in enumerate(items):
        kb.append([InlineKeyboardButton(text=f"{i + 1}. {t}"[:24], callback_data=f"{prefix}:nop:{i}"),
                   InlineKeyboardButton(text="⬆️", callback_data=f"{prefix}:up:{i}"),
                   InlineKeyboardButton(text="⬇️", callback_data=f"{prefix}:down:{i}"),
                   InlineKeyboardButton(text=COLOR_EMOJI.get(c, "⚪️"), callback_data=f"{prefix}:color:{i}"),
                   InlineKeyboardButton(text="↔️" if join else "⤵️", callback_data=f"{prefix}:join:{i}"),
                   InlineKeyboardButton(text="🗑", callback_data=f"{prefix}:del:{i}")])
    kb.append([InlineKeyboardButton(text="➕ دکمهٔ لینک", callback_data=f"{prefix}:addl:0", style=ButtonStyle.SUCCESS)])
    if main_buttons:
        kb.append([InlineKeyboardButton(text="➕ دکمه از صفحهٔ اصلی ربات", callback_data=f"{prefix}:addm:0",
                                        style=ButtonStyle.SUCCESS)])
    kb.append([InlineKeyboardButton(text="✍️ ویرایش متنی (همه با هم)", callback_data=f"{prefix}:text:0"),
               InlineKeyboardButton(text="👁 پیش‌نمایش", callback_data=f"{prefix}:prev:0")])
    kb.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def color_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{COLOR_EMOJI[c]} {COLOR_FA[c]}", callback_data=f"{prefix}:pick:{n}",
                              **({"style": ButtonStyle(c)} if c else {})) for n, c in enumerate(COLORS[:2])],
        [InlineKeyboardButton(text=f"{COLOR_EMOJI[c]} {COLOR_FA[c]}", callback_data=f"{prefix}:pick:{n + 2}",
                              style=ButtonStyle(c)) for n, c in enumerate(COLORS[2:])]])


def row_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⤵️ ردیف جدا", callback_data=f"{prefix}:row:0"),
        InlineKeyboardButton(text="↔️ کنار دکمهٔ قبلی", callback_data=f"{prefix}:row:1")]])


TEXT_HELP = ("دکمه‌ها را یک‌جا بفرست؛ هر خط یک ردیف و هر دکمه «متن | لینک» (رنگ اختیاری: <code>| blue</code>، "
             "<code>| green</code> یا <code>| red</code>). چند دکمه کنار هم: بینشان <code>&&</code>. مثال:\n"
             "<code>خرید استارز | https://t.me/mybot?start=stars | green</code>\n"
             "<code>کانال | https://t.me/x && پشتیبانی | https://t.me/y | blue</code>\n\n"
             "حذف همه: «پاک» · انصراف: /cancel")


# ───────────────────────── موتور هندلرها (مشترک ربات مشتری و بات‌ساز اصلی) ─────────────────────────
from aiogram.fsm.state import State, StatesGroup  # noqa: E402


class PbFSM(StatesGroup):
    text = State()      # ویرایش متنی همه
    label = State()     # متن دکمهٔ تازه
    url = State()       # لینک دکمهٔ تازه


class Store:
    """get/set متن خام دکمه‌ها؛ main_buttons ⇒ [(عنوان، لینک)] برای «دکمه از صفحهٔ اصلی» (یا [] اگر ندارد)."""

    async def get(self) -> str:
        raise NotImplementedError

    async def set(self, raw: str) -> None:
        raise NotImplementedError

    async def main_buttons(self) -> list[tuple[str, str]]:
        return []


async def show_editor(target, store: Store, prefix: str, back_cb: str, allow_main: bool, edit: bool = True):
    text, kb = editor(parse(await store.get()), prefix, back_cb, allow_main)
    if edit and getattr(target, "edit_text", None):
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:  # noqa: BLE001
            logger.debug("price_buttons: ویرایش پیام ممکن نشد؛ پیام تازه", exc_info=True)
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


async def on_cb(cb, state, store: Store, prefix: str, back_cb: str, allow_main: bool):
    try:
        _p, action, arg = cb.data.split(":", 2)
        i = int(arg)
    except ValueError:
        await cb.answer()
        return
    rows = parse(await store.get())
    if action in ("up", "down", "color", "join", "del"):
        await store.set(dump(apply(rows, action, i)))
    elif action == "nop":
        b = flat(rows)[i] if i < len(flat(rows)) else None
        await cb.answer((f"{b[0]}\n{b[1]}" if b else ""), show_alert=bool(b))
        return
    elif action == "prev":
        kb = keyboard(rows)
        await cb.message.answer("👁 پیش‌نمایش دکمه‌ها:" if kb else "هنوز دکمه‌ای نیست.", reply_markup=kb)
        await cb.answer()
        return
    elif action == "text":
        await state.set_state(PbFSM.text)
        await state.update_data(pb_prefix=prefix)
        await cb.message.answer(TEXT_HELP, parse_mode="HTML")
        await cb.answer()
        return
    elif action == "addl":
        if len(flat(rows)) >= MAX_BTNS:
            await cb.answer(f"حداکثر {MAX_BTNS} دکمه", show_alert=True)
            return
        await state.set_state(PbFSM.label)
        await state.update_data(pb_prefix=prefix, pb_new=None)
        await cb.message.answer("✏️ متن دکمه را بفرست (مثلا «خرید استارز کف قیمت»). انصراف: /cancel")
        await cb.answer()
        return
    elif action == "addm":
        mb = await store.main_buttons()
        if not mb:
            await cb.answer("دکمه‌ای در صفحهٔ اصلی ربات پیدا نشد.", show_alert=True)
            return
        await state.update_data(pb_prefix=prefix, pb_main=mb[:40])
        kb = [[InlineKeyboardButton(text=t[:40], callback_data=f"{prefix}:m:{n}")] for n, (t, _u) in enumerate(mb[:40])]
        kb.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"{prefix}:open:0", style=ButtonStyle.DANGER)])
        await cb.message.answer("یکی از دکمه‌های صفحهٔ اصلی ربات را انتخاب کن؛ با زدنش کاربر وارد ربات می‌شود و همان دکمه "
                                "برایش باز می‌شود:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await cb.answer()
        return
    elif action == "m":
        mb = (await state.get_data()).get("pb_main") or []
        if i >= len(mb):
            await cb.answer("دوباره انتخاب کن", show_alert=True)
            return
        await state.update_data(pb_new=[mb[i][0], mb[i][1]])
        await cb.message.answer("🎨 رنگ دکمه:", reply_markup=color_kb(prefix))
        await cb.answer()
        return
    elif action == "pick":
        await state.update_data(pb_color=COLORS[i] if 0 <= i < len(COLORS) else "")
        await cb.message.answer("جای دکمه:", reply_markup=row_kb(prefix))
        await cb.answer()
        return
    elif action == "row":
        d = await state.get_data()
        new = d.get("pb_new")
        await state.set_state(None)
        if not new:
            await cb.answer("دوباره از «➕» شروع کن", show_alert=True)
            return
        rows2, ok = add(rows, new[0], new[1], d.get("pb_color", ""), same_row=bool(i))
        await store.set(dump(rows2))
        await state.update_data(pb_new=None)
        await cb.answer("✅ اضافه شد" if ok else "ردیف‌ها پر است", show_alert=not ok)
        await show_editor(cb.message, store, prefix, back_cb, allow_main, edit=False)
        return
    await show_editor(cb.message, store, prefix, back_cb, allow_main)
    await cb.answer()


async def on_msg(msg, state, store: Store, prefix: str, back_cb: str, allow_main: bool) -> None:
    cur = await state.get_state()
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو", "انصراف"):
        await state.set_state(None)
        await show_editor(msg, store, prefix, back_cb, allow_main, edit=False)
        return
    if cur == PbFSM.text.state:
        if raw == "پاک":
            await store.set("")
        else:
            rows = parse(raw)
            if not rows:
                await msg.answer("❌ هیچ خط درستی پیدا نشد. هر خط: «متن | https://…». دوباره بفرست یا /cancel.")
                return
            await store.set(dump(rows))
        await state.set_state(None)
        await msg.answer("✅ ذخیره شد.")
        await show_editor(msg, store, prefix, back_cb, allow_main, edit=False)
    elif cur == PbFSM.label.state:
        if not raw or len(raw) > 40:
            await msg.answer("❌ متن دکمه 1 تا 40 حرف باشد.")
            return
        await state.update_data(pb_label=raw)
        await state.set_state(PbFSM.url)
        await msg.answer("🔗 لینک دکمه را بفرست (با https:// یا t.me/…):")
    elif cur == PbFSM.url.state:
        url = raw if raw.startswith(("http://", "https://", "tg://")) else ("https://" + raw if raw.startswith("t.me/") else raw)
        if not valid_url(url):
            await msg.answer("❌ لینک معتبر نیست؛ مثل https://t.me/mychannel بفرست.")
            return
        d = await state.get_data()
        await state.update_data(pb_new=[d.get("pb_label", "دکمه"), url])
        await state.set_state(None)
        await msg.answer("🎨 رنگ دکمه:", reply_markup=color_kb(prefix))
