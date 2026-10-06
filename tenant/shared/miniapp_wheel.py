"""
miniapp_wheel.py — گردونهٔ شانسِ مینی‌اپ (PATCH_MINIAPP_WHEEL_V1)

• تنظیمات (JSON در Setting «wheel_cfg»): روشن/خاموش، عنوان، تعدادِ اسلایدها،
  جایزه و شانسِ هر اسلاید، «چند بار در هر چند ساعت»، و سقفِ کلِ دفعاتِ هر کاربر.
• جایزه‌ها: پوچ | موجودیِ کیف‌پول | کدِ تخفیفِ درصدی | کدِ تخفیفِ مبلغی.
• نتیجه را سرور تعیین می‌کند (نه مرورگر)؛ کیف‌پول/کد تخفیف همان‌جا و اتمیک ثبت می‌شود.
• روت‌ها رویِ miniapp_router ثبت می‌شوند؛ پنلِ مدیریت در رباتِ خودِ مالک (wheel:*).
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from config import Session
from models import BotUser, DiscountCode, WalletTxn, WheelSpin

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

wheel_admin_router = Router()

MAX_SLICES = 12
MIN_SLICES = 2
_TYPES = ("none", "balance", "pct", "amt")

_DEFAULT_SLICES = [
    {"t": "none", "v": 0, "w": 35},
    {"t": "balance", "v": 1000, "w": 25},
    {"t": "none", "v": 0, "w": 20},
    {"t": "balance", "v": 5000, "w": 10},
    {"t": "pct", "v": 10, "w": 8},
    {"t": "balance", "v": 10000, "w": 2},
]


def _default_cfg() -> dict:
    return {"enabled": False, "title": "🎡 گردونهٔ شانس", "per_period": 1,
            "period_hours": 24, "max_total": 0,
            "slices": [dict(x) for x in _DEFAULT_SLICES]}


def slice_label(sl: dict) -> str:
    t, v = sl.get("t"), int(sl.get("v") or 0)
    if t == "balance":
        return f"{v:,} تومان"
    if t == "pct":
        return f"{v}٪ تخفیف"
    if t == "amt":
        return f"{v:,} تخفیف"
    return "پوچ 😅"


def _norm(cfg: dict) -> dict:
    base = _default_cfg()
    if not isinstance(cfg, dict):
        return base
    out = dict(base)
    out["enabled"] = bool(cfg.get("enabled"))
    out["title"] = (str(cfg.get("title") or base["title"]))[:40]
    for k, lo, hi in (("per_period", 1, 100), ("period_hours", 1, 24 * 90), ("max_total", 0, 100000)):
        try:
            out[k] = max(lo, min(hi, int(cfg.get(k, base[k]))))
        except Exception:
            out[k] = base[k]
    sl = []
    for x in (cfg.get("slices") or []):
        if not isinstance(x, dict) or x.get("t") not in _TYPES:
            continue
        try:
            v = max(0, int(x.get("v") or 0))
            w = max(0, int(x.get("w") if x.get("w") is not None else 1))
        except Exception:
            continue
        sl.append({"t": x["t"], "v": v, "w": w})
    out["slices"] = sl[:MAX_SLICES] if len(sl) >= MIN_SLICES else base["slices"]
    return out


async def load_cfg(bot_id: int) -> dict:
    from tenant import get_setting
    raw = await get_setting(bot_id, "wheel_cfg", "")
    try:
        return _norm(json.loads(raw)) if raw else _default_cfg()
    except Exception:
        return _default_cfg()


async def save_cfg(bot_id: int, cfg: dict) -> None:
    from tenant import set_setting
    await set_setting(bot_id, "wheel_cfg", json.dumps(_norm(cfg), ensure_ascii=False))


async def public_flag(bot_id: int) -> dict:
    """برایِ /home: فقط وقتی روشن است تایل نشان داده شود."""
    try:
        c = await load_cfg(bot_id)
        return {"enabled": bool(c["enabled"]), "title": c["title"]}
    except Exception:
        return {"enabled": False, "title": ""}


# ─────────────────────────── منطقِ چرخش ───────────────────────────
def _fmt_wait(sec: int) -> str:
    sec = max(0, int(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    if h >= 24:
        return f"{h // 24} روز و {h % 24} ساعت"
    if h:
        return f"{h} ساعت و {m} دقیقه"
    return f"{max(1, m)} دقیقه"


async def _state_for(s, bot_id: int, user_id: int, cfg: dict) -> dict:
    """وضعیتِ کاربر: چند چرخشِ مجاز مانده و کی نوبتِ بعدی است."""
    now = datetime.utcnow()
    since = now - timedelta(hours=cfg["period_hours"])
    recent = (await s.scalars(select(WheelSpin.created_at).where(
        WheelSpin.bot_id == bot_id, WheelSpin.user_id == user_id,
        WheelSpin.created_at > since).order_by(WheelSpin.created_at))).all()
    total = await s.scalar(select(func.count(WheelSpin.id)).where(
        WheelSpin.bot_id == bot_id, WheelSpin.user_id == user_id)) or 0
    left = max(0, cfg["per_period"] - len(recent))
    wait = 0
    if left == 0 and recent:
        wait = int((recent[0] + timedelta(hours=cfg["period_hours"]) - now).total_seconds())
    exhausted = bool(cfg["max_total"] and total >= cfg["max_total"])
    return {"left": left, "wait": max(0, wait), "total": int(total), "exhausted": exhausted}


def _pick(slices: list[dict]) -> int:
    ws = [max(0, int(x["w"])) for x in slices]
    tot = sum(ws)
    if tot <= 0:
        return secrets.randbelow(len(slices))
    r = secrets.randbelow(tot)
    acc = 0
    for i, w in enumerate(ws):
        acc += w
        if r < acc:
            return i
    return len(slices) - 1


async def _new_code(s, bot_id: int) -> str:
    for _ in range(8):
        code = "WH" + secrets.token_hex(3).upper()
        if not await s.scalar(select(DiscountCode.id).where(
                DiscountCode.bot_id == bot_id, func.upper(DiscountCode.code) == code)):
            return code
    return "WH" + secrets.token_hex(5).upper()


async def spin(bot_id: int, user_id: int) -> dict:
    cfg = await load_cfg(bot_id)
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="wheel_disabled")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.id == user_id).with_for_update())
        if not u or u.bot_id != bot_id:
            raise HTTPException(status_code=404, detail="user")
        st = await _state_for(s, bot_id, user_id, cfg)
        if st["exhausted"]:
            raise HTTPException(status_code=429, detail="wheel_exhausted")
        if st["left"] <= 0:
            raise HTTPException(status_code=429, detail=f"wheel_wait:{st['wait']}")
        idx = _pick(cfg["slices"])
        sl = cfg["slices"][idx]
        t, v = sl["t"], int(sl["v"])
        code = None
        if t == "balance" and v > 0:
            u.balance = float(u.balance or 0) + v
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=v, kind="charge",
                            note="جایزهٔ گردونهٔ شانس"))
        elif t in ("pct", "amt") and v > 0:
            code = await _new_code(s, bot_id)
            s.add(DiscountCode(
                bot_id=bot_id, code=code, max_uses=1, is_active=True,
                disc_type=("percent" if t == "pct" else "amount"),
                percent=(min(v, 100) if t == "pct" else 0),
                amount=(v if t == "amt" else 0),
                expires_at=datetime.utcnow() + timedelta(days=30)))
        s.add(WheelSpin(bot_id=bot_id, user_id=u.id, slice_idx=idx,
                        prize_type=t, value=v, code=code))
        await s.commit()
        bal = float(u.balance or 0)
        st2 = await _state_for(s, bot_id, user_id, cfg)
    return {"index": idx, "type": t, "value": v, "label": slice_label(sl), "code": code,
            "balance": bal, "left": st2["left"], "wait": st2["wait"],
            "exhausted": st2["exhausted"]}


# ─────────────────────────── API مینی‌اپ ───────────────────────────
def _install_routes() -> None:
    from tenant.shared.miniapp import miniapp_router, _authed_context, _rial

    @miniapp_router.get("/api/miniapp/{bot_id}/wheel")
    async def api_wheel_info(bot_id: int, x_init_data: str | None = Header(default=None),
                             x_kb_token: str | None = Header(default=None)):
        row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
        cfg = await load_cfg(bot_id)
        if not cfg["enabled"]:
            return JSONResponse({"ok": True, "enabled": False})
        async with Session() as s:
            st = await _state_for(s, bot_id, u.id, cfg)
        return JSONResponse({
            "ok": True, "enabled": True, "title": cfg["title"],
            "slices": [{"label": slice_label(x)} for x in cfg["slices"]],
            "left": st["left"], "wait": st["wait"], "exhausted": st["exhausted"],
            "per_period": cfg["per_period"], "period_hours": cfg["period_hours"],
            "max_total": cfg["max_total"], "total": st["total"],
        })

    @miniapp_router.post("/api/miniapp/{bot_id}/wheel/spin")
    async def api_wheel_spin(bot_id: int, x_init_data: str | None = Header(default=None),
                             x_kb_token: str | None = Header(default=None)):
        row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
        try:
            r = await spin(bot_id, u.id)
        except HTTPException as e:
            d = str(e.detail)
            if d.startswith("wheel_wait:"):
                try:
                    w = int(d.split(":", 1)[1])
                except Exception:
                    w = 0
                return JSONResponse({"ok": False, "error": "wait", "wait": w,
                                     "msg": f"نوبتِ بعدی: {_fmt_wait(w)} دیگر"}, status_code=429)
            if d == "wheel_exhausted":
                return JSONResponse({"ok": False, "error": "exhausted",
                                     "msg": "سقفِ دفعاتِ چرخشِ تو تمام شده است."}, status_code=429)
            raise
        r["balance_fa"] = _rial(r.pop("balance"))
        r["ok"] = True
        return JSONResponse(r)


# ─────────────────────────── پنلِ مدیریت (ربات) ───────────────────────────
class WheelFSM(StatesGroup):
    title = State()
    per = State()
    hours = State()
    maxt = State()
    slice_val = State()


async def _owner(cb_or_msg, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    uid = cb_or_msg.from_user.id
    if not row or not await is_owner_or_admin(row.id, uid, row.owner_id):
        if isinstance(cb_or_msg, CallbackQuery):
            await cb_or_msg.answer()
        return None
    return row


def _btn(text, cd, style=ButtonStyle.PRIMARY):
    return InlineKeyboardButton(text=text, callback_data=cd, style=style)


async def _menu(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    c = await load_cfg(bot_id)
    n = len(c["slices"])
    tw = sum(x["w"] for x in c["slices"]) or 1
    lines = [f"🎡 <b>گردونهٔ شانسِ مینی‌اپ</b>\n",
             f"وضعیت: {'✅ روشن' if c['enabled'] else '❌ خاموش'}",
             f"عنوان: {c['title']}",
             f"تعداد اسلاید: <b>{n}</b>",
             f"هر کاربر: <b>{c['per_period']}</b> بار در هر <b>{c['period_hours']}</b> ساعت",
             f"سقفِ کلِ دفعاتِ هر کاربر: <b>{c['max_total'] or 'نامحدود'}</b>\n",
             "اسلایدها (شانس):"]
    for i, sl in enumerate(c["slices"], 1):
        lines.append(f"{i}. {slice_label(sl)} — {sl['w'] * 100 / tw:.1f}٪")
    rows = [
        [_btn("🔴 خاموش‌کردن" if c["enabled"] else "🟢 روشن‌کردن", "wheel:toggle",
              ButtonStyle.DANGER if c["enabled"] else ButtonStyle.SUCCESS)],
        [_btn("➖ کم‌کردنِ اسلاید", "wheel:n:-1"), _btn("➕ اضافه‌کردنِ اسلاید", "wheel:n:1")],
    ]
    for i, sl in enumerate(c["slices"]):
        rows.append([_btn(f"✏️ {i + 1}. {slice_label(sl)}  (وزن {sl['w']})", f"wheel:s:{i}")])
    rows += [
        [_btn("🔁 تعدادِ دفعات در هر بازه", "wheel:per"), _btn("⏱ طولِ بازه (ساعت)", "wheel:hours")],
        [_btn("🔢 سقفِ کلِ دفعاتِ هر کاربر", "wheel:maxt")],
        [_btn("🏷 عنوان", "wheel:title")],
        [_btn("♻️ بازگشت به پیش‌فرض", "wheel:reset", ButtonStyle.DANGER)],
        [_btn("⬅️ بازگشت", "miniapp:panel", ButtonStyle.DANGER)],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb: CallbackQuery, bot_id: int):
    text, kb = await _menu(bot_id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@wheel_admin_router.callback_query(F.data == "wheel:menu")
async def wheel_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    await _show(cb, row.id)
    await cb.answer()


@wheel_admin_router.callback_query(F.data == "wheel:toggle")
async def wheel_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    c = await load_cfg(row.id)
    c["enabled"] = not c["enabled"]
    await save_cfg(row.id, c)
    await _show(cb, row.id)
    await cb.answer("روشن شد" if c["enabled"] else "خاموش شد")


@wheel_admin_router.callback_query(F.data == "wheel:reset")
async def wheel_reset(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    c = _default_cfg()
    old = await load_cfg(row.id)
    c["enabled"] = old["enabled"]
    await save_cfg(row.id, c)
    await _show(cb, row.id)
    await cb.answer("به پیش‌فرض برگشت")


@wheel_admin_router.callback_query(F.data.startswith("wheel:n:"))
async def wheel_count(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    d = int(cb.data.split(":")[2])
    c = await load_cfg(row.id)
    n = len(c["slices"]) + d
    if n < MIN_SLICES or n > MAX_SLICES:
        await cb.answer(f"تعدادِ اسلاید باید بین {MIN_SLICES} تا {MAX_SLICES} باشد.", show_alert=True)
        return
    if d > 0:
        c["slices"].append({"t": "none", "v": 0, "w": 10})
    else:
        c["slices"].pop()
    await save_cfg(row.id, c)
    await _show(cb, row.id)
    await cb.answer()


_TYPE_LBL = {"none": "😅 پوچ", "balance": "💰 موجودیِ کیف‌پول", "pct": "🏷 تخفیفِ درصدی", "amt": "💵 تخفیفِ مبلغی"}


@wheel_admin_router.callback_query(F.data.regexp(r"^wheel:s:\d+$"))
async def wheel_slice(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    i = int(cb.data.split(":")[2])
    c = await load_cfg(row.id)
    if i >= len(c["slices"]):
        await cb.answer()
        return
    rows = [[_btn(lbl, f"wheel:t:{i}:{t}")] for t, lbl in _TYPE_LBL.items()]
    rows.append([_btn("🎲 فقط تغییرِ وزنِ شانس", f"wheel:w:{i}")])
    rows.append([_btn("⬅️ بازگشت", "wheel:menu", ButtonStyle.DANGER)])
    await cb.message.edit_text(
        f"✏️ اسلایدِ {i + 1}: <b>{slice_label(c['slices'][i])}</b> (وزن {c['slices'][i]['w']})\n\n"
        "نوعِ جایزه را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@wheel_admin_router.callback_query(F.data.regexp(r"^wheel:t:\d+:\w+$"))
async def wheel_slice_type(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, i_s, t = cb.data.split(":")
    i = int(i_s)
    if t not in _TYPES:
        await cb.answer()
        return
    c = await load_cfg(row.id)
    if i >= len(c["slices"]):
        await cb.answer()
        return
    if t == "none":
        c["slices"][i].update(t="none", v=0)
        await save_cfg(row.id, c)
        await _show(cb, row.id)
        await cb.answer("ذخیره شد")
        return
    await state.set_state(WheelFSM.slice_val)
    await state.update_data(w_i=i, w_t=t, w_mode="val")
    hint = {"balance": "مبلغِ جایزه (تومان)", "pct": "درصدِ تخفیف (۱ تا ۱۰۰)",
            "amt": "مبلغِ تخفیف (تومان)"}[t]
    await cb.message.answer(f"✏️ {hint} را بفرست (فقط عدد). برایِ لغو /cancel:")
    await cb.answer()


@wheel_admin_router.callback_query(F.data.regexp(r"^wheel:w:\d+$"))
async def wheel_slice_weight(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    i = int(cb.data.split(":")[2])
    await state.set_state(WheelFSM.slice_val)
    await state.update_data(w_i=i, w_t=None, w_mode="weight")
    await cb.message.answer("🎲 وزنِ شانسِ این اسلاید را بفرست (عددِ صحیح؛ هرچه بزرگ‌تر، شانس بیشتر. "
                            "مثلاً ۱۰ در برابرِ ۴۰ یعنی یک‌چهارم). برایِ لغو /cancel:")
    await cb.answer()


def _num(text: str) -> int | None:
    t = (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬,", "01234567890123456789  "))
    t = t.replace(" ", "")
    return int(t) if t.isdigit() else None


@wheel_admin_router.message(WheelFSM.slice_val, F.text)
async def wheel_slice_val_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    if (msg.text or "").strip().startswith("/cancel"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    n = _num(msg.text)
    d = await state.get_data()
    i, t, mode = d.get("w_i"), d.get("w_t"), d.get("w_mode")
    if n is None or n < 0 or (mode == "val" and n <= 0) or (t == "pct" and n > 100):
        await msg.answer("❌ عددِ نامعتبر. دوباره بفرست (یا /cancel).")
        return
    c = await load_cfg(row.id)
    if i is None or i >= len(c["slices"]):
        await state.clear()
        return
    if mode == "weight":
        c["slices"][i]["w"] = n
    else:
        c["slices"][i].update(t=t, v=n)
    await save_cfg(row.id, c)
    await state.clear()
    text, kb = await _menu(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


_NUM_FIELDS = {"per": ("per_period", "تعدادِ دفعاتِ مجاز در هر بازه (مثلاً 1)", 1),
               "hours": ("period_hours", "طولِ بازه به ساعت (مثلاً 24 = روزی یک‌بار)", 1),
               "maxt": ("max_total", "سقفِ کلِ دفعاتِ هر کاربر (0 = نامحدود)", 0)}


@wheel_admin_router.callback_query(F.data.in_({"wheel:per", "wheel:hours", "wheel:maxt"}))
async def wheel_num_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    k = cb.data.split(":")[1]
    await state.set_state(getattr(WheelFSM, k))
    await cb.message.answer(f"✏️ {_NUM_FIELDS[k][1]} را بفرست. برایِ لغو /cancel:")
    await cb.answer()


async def _num_save(msg: Message, bot: Bot, state: FSMContext, k: str):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    if (msg.text or "").strip().startswith("/cancel"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    n = _num(msg.text)
    field, _, lo = _NUM_FIELDS[k]
    if n is None or n < lo:
        await msg.answer("❌ عددِ نامعتبر. دوباره بفرست (یا /cancel).")
        return
    c = await load_cfg(row.id)
    c[field] = n
    await save_cfg(row.id, c)
    await state.clear()
    text, kb = await _menu(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@wheel_admin_router.message(WheelFSM.per, F.text)
async def wheel_per_save(msg: Message, bot: Bot, state: FSMContext):
    await _num_save(msg, bot, state, "per")


@wheel_admin_router.message(WheelFSM.hours, F.text)
async def wheel_hours_save(msg: Message, bot: Bot, state: FSMContext):
    await _num_save(msg, bot, state, "hours")


@wheel_admin_router.message(WheelFSM.maxt, F.text)
async def wheel_maxt_save(msg: Message, bot: Bot, state: FSMContext):
    await _num_save(msg, bot, state, "maxt")


@wheel_admin_router.callback_query(F.data == "wheel:title")
async def wheel_title_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(WheelFSM.title)
    await cb.message.answer("✏️ عنوانِ گردونه را بفرست (حداکثر ۴۰ حرف). برایِ لغو /cancel:")
    await cb.answer()


@wheel_admin_router.message(WheelFSM.title, F.text)
async def wheel_title_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    if (msg.text or "").strip().startswith("/cancel"):
        await msg.answer("لغو شد.")
        return
    c = await load_cfg(row.id)
    c["title"] = (msg.text or "").strip()[:40] or c["title"]
    await save_cfg(row.id, c)
    text, kb = await _menu(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")
