"""
user.py — سمتِ مشتریِ رباتِ پنل‌فروشی (PATCH_PANEL_SELLER_V1).

خرید/تمدید فقط از کیف‌پول است؛ اگر موجودی کم بود، همان «شارژ حساب» (کارت‌به‌کارت/درگاه‌ها) باز می‌شود و بعد از شارژ
دکمهٔ ثبتِ همین خرید برایِ مشتری فرستاده می‌شود. تغییرِ رمز و خاموش/روشنِ اشتراک‌ها فقط وقتی که مالک اجازه داده باشد.
callbackها: psel:b* (خرید) · psel:a* / psel:rn* / psel:pw* / psel:tg* (پنل‌هایِ من)
"""
from __future__ import annotations

import asyncio
import html as _html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from config import Session
from models import PanelSellerAccount, PanelSellerPackage, VpnPanel

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller.common import (days_text, exp_text, HOME_ROW, REASON_FA, STATUS_FA, back_row, btn, edit_or_send, esc, fa,
                                             fmt_time, gb_text, kb, n, pk_line, sales_gate, users_text)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

psel_user_router = Router(name="psel_user")

_ERR_FA = {"no_package": "این بسته دیگر موجود نیست.", "no_panel": "پنلِ این بسته فعلاً در دسترس نیست.",
           "unsupported": "این پنل فعلاً قابلِ فروش نیست.", "not_found": "پیدا نشد.",
           "inactive": "این پنل فعال نیست."}


async def get_setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting as _g
    return await _g(bot_id, key, default)


async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _perm(row, key: str) -> bool:
    from tenant import get_setting
    from tenant.bots.panel_seller.admin import PERMS
    return (await get_setting(row.id, key, PERMS[key][1])) == "1"


# ═══════════════════════ خرید ═══════════════════════

async def show_buy(event, bot: Bot, row, uid: int) -> None:
    ok, why = await sales_gate(row)
    if not ok:
        return await edit_or_send(event, f"⛔️ {why}", kb(HOME_ROW))
    async with Session() as s:
        pks = (await s.scalars(select(PanelSellerPackage).where(
            PanelSellerPackage.bot_id == row.id, PanelSellerPackage.active == True)   # noqa: E712
            .order_by(PanelSellerPackage.price_toman))).all()
        panels = {p.id: p for p in (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == row.id))).all()}
    pks = [p for p in pks if panels.get(p.panel_id) and panels[p.panel_id].enabled and AD.supports(panels[p.panel_id].panel_key)]
    rows = [[btn(f"{p.title[:28]} — {n(p.price_toman)} تومان", f"psel:b:{p.id}", style=ButtonStyle.SUCCESS)] for p in pks]
    if (await SV.custom_settings(row.id))["price"] > 0:      # PATCH_PSEL_CUSTOM_VOLUME_V1
        rows.insert(0, [btn("📦 خریدِ حجمِ دلخواه", "psel:cvb", style=ButtonStyle.SUCCESS)])
    if await SV.test_available(row.id, uid):          # PATCH_PSEL_FREE_TEST_V1
        rows.insert(0, [btn("🧪 تستِ رایگان", "psel:tst", style=ButtonStyle.PRIMARY)])
    if not rows:     # PATCH_PSEL_NO_PACKAGES_BUT_CUSTOM_V1 — «فروشِ حجمِ دلخواه فعاله ولی بات هیچی نمیاره»: بدونِ بسته هم حجمِ دلخواه/تست دیده شود
        return await edit_or_send(event, "📦 فعلاً بسته‌ای برایِ فروش نیست؛ بعداً سر بزن.", kb(HOME_ROW))
    rows.append(HOME_ROW)
    await edit_or_send(event, "🧩 <b>خریدِ پنل</b>\n\nیک بسته را انتخاب کن؛ بعد از پرداخت، ادمینِ پنل با نام‌کاربری و رمز برایت ساخته می‌شود.",
                       kb(*rows))


@psel_user_router.callback_query(F.data.regexp(r"^psel:b:\d+$"))
async def buy_detail(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(PanelSellerPackage, pid)
    if not p or p.bot_id != row.id or not p.active:
        return await cb.answer("این بسته دیگر موجود نیست.", show_alert=True)
    note = (await get_setting(row.id, "psel_note_invoice", "")).strip()          # PATCH_PSEL_NOTES_V1
    await edit_or_send(cb, f"{pk_line(p)}\n\nپرداخت از کیف‌پولِ ربات انجام می‌شود (اگر موجودی کم بود، شارژِ حساب باز می‌شود)."
                           + (f"\n\n📝 {esc(note)}" if note else ""),
                       kb([btn("✅ خرید با کیف‌پول", f"psel:bok:{pid}", style=ButtonStyle.SUCCESS)],
                          [btn("🏷 کد تخفیف دارم", f"psel:dc:p:{pid}", style=ButtonStyle.PRIMARY)], back_row("psel:buy")))
    await cb.answer()


@psel_user_router.callback_query(F.data == "psel:buy")
async def buy_back(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    row = await _row(bot)
    if row:
        await show_buy(cb, bot, row, cb.from_user.id)
    await cb.answer()


class PselBuyFSM(StatesGroup):
    username = State()       # PATCH_PSEL_PICK_USERNAME_V1
    custom_gb = State()      # PATCH_PSEL_CUSTOM_VOLUME_V1
    discount = State()       # PATCH_PSEL_DISCOUNT_V1


_TAKEN_HINTS = ("exist", "already", "409", "duplicate", "taken", "unique")


async def _provision(event, bot: Bot, row, uid: int, *, pid: int | None = None, gb: int | None = None,
                     username: str | None = None, retry_cb: str = "", discount_code: str | None = None) -> dict:
    """خریدِ بسته (pid) یا حجمِ دلخواه (gb) و نمایشِ نتیجه. event: CallbackQuery یا Message. خروجی: دیکشنریِ نتیجه (status…)."""
    r = (await SV.buy_custom(row.id, uid, int(gb), bot=bot, row=row, username=username, discount_code=discount_code) if gb is not None
         else await SV.buy(row.id, uid, int(pid), bot=bot, row=row, username=username, discount_code=discount_code))
    st = r["status"]
    msg_obj = event.message if isinstance(event, CallbackQuery) else event
    if st == "insufficient":
        from tenant.shared import vs_pay
        await vs_pay.insufficient_notice(msg_obj, row.id, uid, int(r["need"]), retry_cb or "psel:buy")
    elif st == "panel_failed":
        await tell_owner_failure(bot, row, uid, "ساختِ پنل", r.get("error"))      # PATCH_PSEL_OWNER_FAIL_NOTICE_V1
        if not (username and any(h in (r.get("error") or "").lower() for h in _TAKEN_HINTS)):
            await edit_or_send(event, "❌ ساختِ پنل با خطا روبه‌رو شد؛ مبلغی کم نشد (اگر کم شده بود برگشته).\n"
                                      "کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", kb(back_row("psel:buy")))
    elif st == "bad_discount":
        await edit_or_send(event, "❌ " + (r.get("reason") or "کد تخفیف معتبر نیست.") + "\nبدونِ کد هم می‌توانی بخری.", kb(back_row("psel:buy")))
    elif st != "ok":
        msg = {"bad_username": "نام‌کاربری نامعتبر است.", "username_taken": "این نام‌کاربری قبلاً گرفته شده.",
               "bad_amount": "مقدارِ نامعتبر.", "disabled": "این بخش فعال نیست."}.get(st) or _ERR_FA.get(st, "خرید انجام نشد.")
        await edit_or_send(event, "❌ " + msg, kb(back_row("psel:buy")))
    else:
        await edit_or_send(
            event, "✅ <b>پنلِ تو آماده است!</b>\n\n"
                   f"🌐 آدرس: <code>{esc(r['panel_url'])}</code>\n🆔 نام‌کاربری: <code>{esc(r['username'])}</code>\n"
                   f"🔑 رمز: <code>{esc(r['password'])}</code>\n🔖 کدِ پیگیری: <code>{esc(r['tracking'])}</code>\n\n"
                   "رمز را جایی امن نگه دار. وضعیتِ پنل از «🗂 پنل‌هایِ من» قابلِ مشاهده است."
                   + (f"\n\n📝 {esc(note)}" if (note := (await get_setting(row.id, "psel_note_delivery", "")).strip()) else ""),
            kb(*_login_rows(r.get("panel_url")), [btn("🗂 پنل‌هایِ من", "psel:my", style=ButtonStyle.PRIMARY)], HOME_ROW))
        if r.get("discount"):
            try:
                await msg_obj.answer(f"🏷 تخفیفِ این خرید: <b>{n(r['discount'])}</b> تومان · پرداختی: <b>{n(r['paid'])}</b> تومان", parse_mode="HTML")
            except Exception:      # noqa: BLE001
                logger.info("panel_seller: پیامِ تخفیف ارسال نشد")
    return r


def _ctx(pid=None, gb=None) -> str:
    return f"p:{pid}" if pid is not None else f"g:{gb}"


async def _disc_code(state: FSMContext, ctx: str) -> str | None:
    """کدِ تخفیفِ تأییدشده برایِ همین خرید (اگر مشتری برای خریدِ دیگری کد زده بود اعمال نمی‌شود)."""
    d = await state.get_data()
    return d.get("dc_code") if d.get("dc_ctx") == ctx else None


async def _clear_disc(state: FSMContext) -> None:
    d = await state.get_data()
    if "dc_code" in d or "dc_ctx" in d:
        d.pop("dc_code", None)
        d.pop("dc_ctx", None)
        await state.set_data(d)


def _login_rows(url: str | None) -> list:
    """PATCH_PSEL_LOGIN_BUTTON_V1 — دکمهٔ «ورود به پنل» (لینکِ مستقیم) اگر آدرسِ پنل http(s) باشد."""
    u = (url or "").strip()
    return [[btn("🌐 ورود به پنل", url=u, style=ButtonStyle.SUCCESS)]] if u.startswith(("http://", "https://")) else []


_USERNAME_PROMPT = ("🆔 <b>نام‌کاربریِ دلخواهِ پنل</b> را بفرست.\n\n"
                    "۳ تا ۳۲ نویسه؛ فقط حروفِ انگلیسی، عدد و <code>_</code> و با حرف شروع شود. مثلاً <code>shop_ali</code>")


@psel_user_router.callback_query(F.data.regexp(r"^psel:bok:\d+$"))
async def buy_do(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_or_create_user
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    pid = int(cb.data.split(":")[2])
    uid = cb.from_user.id
    await get_or_create_user(row.id, uid, cb.from_user.full_name or "", cb.from_user.username or "")
    code = await _disc_code(state, _ctx(pid=pid))                 # PATCH_PSEL_DISCOUNT_V1
    if await SV.pick_username_enabled(row.id):                    # PATCH_PSEL_PICK_USERNAME_V1
        await state.clear()
        await state.set_state(PselBuyFSM.username)
        await state.update_data(pu_pid=pid, pu_gb=None, pu_code=code)
        await edit_or_send(cb, _USERNAME_PROMPT, kb(back_row("psel:buy", "✖️ لغو")))
        return await cb.answer()
    await cb.answer("در حالِ ساختِ پنل…")
    r = await _provision(cb, bot, row, uid, pid=pid, retry_cb=cb.data, discount_code=code)
    if r["status"] in ("ok", "bad_discount"):
        await _clear_disc(state)


@psel_user_router.message(PselBuyFSM.username, F.text)
async def username_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await state.clear()
    u = (msg.text or "").strip()
    if not SV.valid_username(u):
        return await msg.answer("❌ نام‌کاربری نامعتبر است.\n\n" + _USERNAME_PROMPT, parse_mode="HTML")
    if await SV.username_taken(row.id, u):
        return await msg.answer("❌ این نام‌کاربری قبلاً گرفته شده؛ یکی دیگر بفرست.", parse_mode="HTML")
    d = await state.get_data()
    pid, gb, code = d.get("pu_pid"), d.get("pu_gb"), d.get("pu_code")
    if pid is None and gb is None:
        await state.clear()
        return
    ok, why = await sales_gate(row)
    if not ok:
        await state.clear()
        return await msg.answer(f"⛔️ {why}")
    r = await _provision(msg, bot, row, msg.from_user.id, pid=pid, gb=gb, username=u, discount_code=code,
                         retry_cb=(f"psel:cgo:{gb}" if gb is not None else f"psel:bok:{pid}"))
    if r["status"] == "panel_failed" and any(h in (r.get("error") or "").lower() for h in _TAKEN_HINTS):
        return await msg.answer("❌ این نام‌کاربری روی پنل گرفته شده؛ یکی دیگر بفرست.", parse_mode="HTML")
    await state.clear()


# ═══════════════════════ فروشِ حجمیِ دلخواه ═══════════════════════

@psel_user_router.callback_query(F.data == "psel:cvb")      # (قبلاً psel:cv بود و با دکمهٔ مدیر تداخل داشت ⇒ «فقط مدیرِ ربات»)
async def custom_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    cfg = await SV.custom_settings(row.id)
    if cfg["price"] <= 0:
        return await cb.answer("این بخش فعال نیست.", show_alert=True)
    await state.clear()
    await state.set_state(PselBuyFSM.custom_gb)
    await edit_or_send(
        cb, "📦 <b>خریدِ حجمِ دلخواه</b>\n\n"
            f"💵 هر گیگ: <b>{n(cfg['price'])}</b> تومان\n📏 حداقل {fa(cfg['min_gb'])} و حداکثر {fa(cfg['max_gb'])} گیگ\n"
            f"⏳ مدت: {days_text(cfg['days'])} · 👥 کاربر: {users_text(cfg['users'])}\n\n"
            "تعدادِ گیگِ موردنظرت را (فقط عدد) بفرست:", kb(back_row("psel:buy", "✖️ لغو")))
    await cb.answer()


@psel_user_router.message(PselBuyFSM.custom_gb, F.text)
async def custom_gb_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int
    row = await _row(bot)
    if not row:
        return await state.clear()
    cfg = await SV.custom_settings(row.id)
    v = fa_to_int((msg.text or "").strip())
    if cfg["price"] <= 0 or v is None or not (cfg["min_gb"] <= v <= max(cfg["min_gb"], cfg["max_gb"])):
        return await msg.answer(f"❌ عددی بین {fa(cfg['min_gb'])} تا {fa(cfg['max_gb'])} بفرست.")
    await state.clear()
    await msg.answer(f"📦 <b>{fa(v)} گیگ</b>\n💰 مبلغ: <b>{n(v * cfg['price'])}</b> تومان از کیف‌پول کسر می‌شود.\n"
                     f"⏳ مدت: {days_text(cfg['days'])}", parse_mode="HTML",
                     reply_markup=kb([btn("✅ خرید با کیف‌پول", f"psel:cgo:{v}", style=ButtonStyle.SUCCESS)],
                                     [btn("🏷 کد تخفیف دارم", f"psel:dc:g:{v}", style=ButtonStyle.PRIMARY)], back_row("psel:buy")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:cgo:\d+$"))
async def custom_buy(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_or_create_user
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    gb, uid = int(cb.data.split(":")[2]), cb.from_user.id
    await get_or_create_user(row.id, uid, cb.from_user.full_name or "", cb.from_user.username or "")
    code = await _disc_code(state, _ctx(gb=gb))                   # PATCH_PSEL_DISCOUNT_V1
    if await SV.pick_username_enabled(row.id):
        await state.clear()
        await state.set_state(PselBuyFSM.username)
        await state.update_data(pu_pid=None, pu_gb=gb, pu_code=code)
        await edit_or_send(cb, _USERNAME_PROMPT, kb(back_row("psel:buy", "✖️ لغو")))
        return await cb.answer()
    await cb.answer("در حالِ ساختِ پنل…")
    r = await _provision(cb, bot, row, uid, gb=gb, retry_cb=cb.data, discount_code=code)
    if r["status"] in ("ok", "bad_discount"):
        await _clear_disc(state)


# ═══════════════════════ کد تخفیف ═══════════════════════
# === PATCH_PSEL_DISCOUNT_V1 === همان موتورِ کد تخفیفِ رباتِ VPN (درصدی/ثابت، انقضا، سقفِ استفاده، هر نفر یک‌بار)

@psel_user_router.callback_query(F.data.regexp(r"^psel:dc:[pg]:\d+$"))
async def disc_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    from tenant import get_setting
    if (await get_setting(row.id, "discount_purchase_enabled", "1")) == "0":
        return await cb.answer("استفاده از کد تخفیف فعلاً غیرفعال است.", show_alert=True)
    _, _, kind, num = cb.data.split(":")
    back = f"psel:b:{num}" if kind == "p" else "psel:cvb"
    await state.clear()
    await state.set_state(PselBuyFSM.discount)
    await state.update_data(dc_kind=kind, dc_num=int(num))
    await edit_or_send(cb, "🏷 <b>کد تخفیف</b> را بفرست:", kb(back_row(back, "✖️ لغو")))
    await cb.answer()


@psel_user_router.message(PselBuyFSM.discount, F.text)
async def disc_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await state.clear()
    d = await state.get_data()
    kind, num = d.get("dc_kind"), int(d.get("dc_num") or 0)
    code = (msg.text or "").strip()
    q = await SV.quote_discount(row.id, msg.from_user.id, code, **({"package_id": num} if kind == "p" else {"gb": num}))
    if not q["ok"]:
        return await msg.answer(f"❌ {q['reason']}\nکدِ دیگری بفرست یا لغو کن.",
                                reply_markup=kb(back_row(f"psel:b:{num}" if kind == "p" else "psel:cvb", "✖️ لغو")))
    ctx = _ctx(pid=num) if kind == "p" else _ctx(gb=num)
    await state.set_state(None)
    await state.set_data({"dc_code": code, "dc_ctx": ctx})
    buy_cb = f"psel:bok:{num}" if kind == "p" else f"psel:cgo:{num}"
    await msg.answer(f"✅ {esc(q['label'])}\n💰 قیمت: <s>{n(q['price'])}</s> → <b>{n(q['final'])}</b> تومان\n\nپرداخت از کیف‌پول:",
                     parse_mode="HTML", reply_markup=kb([btn("✅ خرید با کیف‌پول", buy_cb, style=ButtonStyle.SUCCESS)],
                                                       back_row("psel:buy")))


ROLE_NOT_CHOSEN_MARK = "نقشِ ادمین در این پنل مشخص نیست"      # متنِ service.role_for


async def tell_owner_failure(bot: Bot, row, buyer_tg: int, what: str, error: str | None) -> None:
    """PATCH_PSEL_OWNER_FAIL_NOTICE_V1 — وقتی ساخت/تمدیدِ پنل روی خودِ پنل خطا می‌دهد، مشتری فقط پیامِ کلی می‌بیند؛ علتِ دقیق
    (مثلاً دسترسیِ sudo، آدرس/پورت، ورژنِ API) برایِ مالک و ادمین‌ها فرستاده می‌شود (حداکثر هر ۱۰ دقیقه یک‌بار برایِ هر ربات).
    PATCH_PSEL_ROLE_BUTTON_V1: اگر علت «نقشِ ادمین انتخاب نشده» (پاسارگاردِ جدید) بود، به‌جایِ فهرستِ عمومیِ «بررسی کن…»، توضیحِ دقیق + دکمهٔ
    مستقیمِ «🛡 انتخاب نقشِ ادمین» می‌آید."""
    try:
        from log_throttle import should_log
        needs_role = ROLE_NOT_CHOSEN_MARK in (error or "")
        if not should_log(f"psel_{'role' if needs_role else 'fail'}:{row.id}", 600):
            return
        from tenant import _notify_admins
        if needs_role:
            await _notify_admins(bot, row, (
                f"⚠️ <b>{esc(what)} برایِ مشتری <code>{buyer_tg}</code> انجام نشد: نقشِ ادمین انتخاب نشده</b>\n\n"
                "اتصال به پنل برقرار است؛ ولی پاسارگاردِ جدید برایِ هر ادمین یک «نقش» می‌خواهد و ربات خودش نمی‌تواند حدس بزند "
                "کدام را بدهد.\n\n"
                f"🔎 <code>{esc((error or '')[:300])}</code>\n\n"
                "👇 روی دکمهٔ زیر بزن، پنلت را انتخاب کن و نقشی را بگذار که فقط کاربرانِ خودِ مشتری را مدیریت می‌کند "
                "(معمولاً <b>Operator</b>؛ کم‌دسترسی‌تر از Administrator). بعد دوباره تست/خرید را امتحان کن."),
                kb=kb([btn("🛡 انتخاب نقشِ ادمین", "psel:role", style=ButtonStyle.SUCCESS)]))
            return
        await _notify_admins(bot, row, (
            f"⚠️ <b>{esc(what)} برایِ مشتری <code>{buyer_tg}</code> روی پنل ناموفق بود</b>\n\n"
            f"🔎 علتِ پنل:\n<code>{esc((error or 'پاسخی از پنل نیامد')[:300])}</code>\n\n"
            "بررسی کن: آدرس/پورتِ پنل، نام‌کاربری و رمزِ sudo، اینکه حسابِ اتصال «sudo» باشد، و اینکه ربات به پنل دسترسیِ شبکه دارد."))
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: اطلاعِ خطایِ پنل به مالک ناموفق بود")


# ═══════════════════════ تستِ رایگان ═══════════════════════

@psel_user_router.callback_query(F.data.regexp(r"^psel:(tst|tstok)$"))
async def free_test(cb: CallbackQuery, bot: Bot):
    from tenant import get_or_create_user
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    uid = cb.from_user.id
    cfg = await SV.test_settings(row.id)
    if not await SV.test_available(row.id, uid):
        return await cb.answer("تستِ رایگان فعال نیست یا قبلاً از آن استفاده کرده‌ای.", show_alert=True)
    if cb.data.endswith(":tst"):
        return await edit_or_send(
            cb, "🧪 <b>تستِ رایگان</b>\n\n"
                f"⏳ مدت: {fa(cfg['hours'])} ساعت · 📊 حجم: {gb_text(cfg['gb'])} · 👥 کاربر: {users_text(cfg['users'])}\n\n"
                "هر نفر یک‌بار می‌تواند تست بگیرد.",
            kb([btn("✅ ساختِ پنلِ تست", "psel:tstok", style=ButtonStyle.SUCCESS)], back_row("psel:buy")))
    await get_or_create_user(row.id, uid, cb.from_user.full_name or "", cb.from_user.username or "")
    await cb.answer("در حالِ ساختِ پنلِ تست…")
    r = await SV.buy_test(row.id, uid, bot=bot, row=row)
    st = r["status"]
    if st == "panel_failed":
        await tell_owner_failure(bot, row, uid, "ساختِ پنلِ تست", r.get("error"))
        return await edit_or_send(cb, "❌ ساختِ پنل با خطا روبه‌رو شد؛ کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", kb(back_row("psel:buy")))
    if st != "ok":
        msg = {"test_used": "قبلاً تست گرفته‌ای.", "disabled": "تستِ رایگان فعال نیست.",
               "no_panel": "فعلاً پنلی برایِ تست متصل نیست.", "unsupported": "پنلِ متصل برایِ تست پشتیبانی نمی‌شود."}.get(st, "انجام نشد.")
        return await edit_or_send(cb, "❌ " + msg, kb(back_row("psel:buy")))
    await edit_or_send(
        cb, "✅ <b>پنلِ تستِ تو آماده است!</b>\n\n"
            f"🌐 آدرس: <code>{esc(r['panel_url'])}</code>\n🆔 نام‌کاربری: <code>{esc(r['username'])}</code>\n"
            f"🔑 رمز: <code>{esc(r['password'])}</code>\n\n"
            f"⏳ {fa(r['hours'])} ساعت · 📊 {gb_text(r['gb'])} · 👥 {users_text(r['users'])}\n"
            "بعد از پایانِ تست برایِ ادامه از «🧩 خریدِ پنل» بسته بخر."
            + (f"\n\n📝 {esc(note)}" if (note := (await get_setting(row.id, "psel_note_delivery", "")).strip()) else ""),
        kb(*_login_rows(r.get("panel_url")), [btn("🗂 پنل‌هایِ من", "psel:my", style=ButtonStyle.PRIMARY)], HOME_ROW))


# ═══════════════════════ پنل‌هایِ من ═══════════════════════

async def show_my(event, bot: Bot, row, uid: int) -> None:
    async with Session() as s:
        accs = (await s.scalars(select(PanelSellerAccount).where(
            PanelSellerAccount.bot_id == row.id, PanelSellerAccount.buyer_tg == uid,
            PanelSellerAccount.status != "deleted").order_by(PanelSellerAccount.id.desc()))).all()
    if not accs:
        return await edit_or_send(event, "🗂 هنوز پنلی نخریده‌ای.", kb([btn("🧩 خریدِ پنل", "psel:buy", style=ButtonStyle.SUCCESS)], HOME_ROW))
    rows = [[btn(f"{STATUS_FA.get(a.status, '')[:1]} {a.title[:24]} · {a.admin_username}", f"psel:a:{a.id}")] for a in accs[:20]]
    rows.append(HOME_ROW)
    await edit_or_send(event, "🗂 <b>پنل‌هایِ من</b>", kb(*rows))


@psel_user_router.callback_query(F.data == "psel:my")
async def my_list(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if row:
        await show_my(cb, bot, row, cb.from_user.id)
    await cb.answer()


async def _live_usage(acc, panel):
    """(مصرف‌گیگ, تعدادِ کاربر, پنلِ جدید؟) یا None اگر پنل جواب نداد — فقط نمایشی. یک درخواست (snapshot)."""
    try:
        api = AD.PanelAdminAPI.for_panel(panel)
        try:
            snap = await asyncio.wait_for(api.snapshot(acc.admin_username), 20)
        finally:
            await api.close()
        return snap["used_bytes"] / SV.GB, snap["users"], bool(snap.get("new_style"))
    except Exception:      # noqa: BLE001
        return None


async def _acc_for(cb: CallbackQuery, bot: Bot, aid: int):
    row = await _row(bot)
    if not row:
        return None, None
    async with Session() as s:
        a = await s.get(PanelSellerAccount, aid)
    if not a or a.bot_id != row.id or a.buyer_tg != cb.from_user.id or a.status == "deleted":
        await cb.answer("پیدا نشد.", show_alert=True)
        return row, None
    return row, a


@psel_user_router.callback_query(F.data.regexp(r"^psel:a:\d+$"))
async def my_detail(cb: CallbackQuery, bot: Bot):
    row, a = await _acc_for(cb, bot, int(cb.data.split(":")[2]))
    if not a:
        return
    async with Session() as s:
        panel = await s.get(VpnPanel, a.panel_id)
    why = f" ({REASON_FA.get(a.suspend_reason, a.suspend_reason)})" if a.status == "suspended" and a.suspend_reason else ""
    text = (f"🗂 <b>{esc(a.title)}</b>\n\n🌐 <code>{esc(panel.base_url) if panel else '—'}</code>\n"
            f"🆔 <code>{esc(a.admin_username)}</code>\nوضعیت: {STATUS_FA.get(a.status, a.status)}{why}\n"
            f"⏳ پایان: {exp_text(a.expires_at)}")
    new_style = False
    if panel and a.status == "active":
        live = await _live_usage(a, panel)
        if live:
            used, users, new_style = live
            text += (f"\n📊 مصرف: {fa(f'{used:.1f}')} از {gb_text(a.data_gb)}\n👥 کاربران: {fa(users)} از {users_text(a.max_users)}")
    aid = a.id
    # === PATCH_PSEL_ACCOUNT_GRID_V1 === (Whale Support) دکمه‌ها مرتب و جفت‌جفت، مثلِ صفحهٔ سرویس در رباتِ VPN
    can_extra = bool(a.data_gb) and (a.status == "active" or (a.status == "suspended" and a.suspend_reason == "traffic")) \
        and await SV.extra_gb_price(row.id) > 0
    cells = [btn("📊 مشخصات و مصرف", f"psel:sp:{aid}", style=ButtonStyle.SUCCESS)]
    if can_extra:
        cells.append(btn("📈 خریدِ حجمِ اضافه", f"psel:xg:{aid}", style=ButtonStyle.SUCCESS))
    if await _perm(row, "psel_allow_renew"):
        cells.append(btn("🔁 تمدید", f"psel:rn:{aid}", style=ButtonStyle.SUCCESS))
    if panel and (panel.base_url or "").startswith(("http://", "https://")):
        cells.append(btn("🌐 ورود به پنل وب", url=panel.base_url, style=ButtonStyle.PRIMARY))
    cells.append(btn("✏️ تغییرِ نامِ سرویس", f"psel:rnm:{aid}", style=ButtonStyle.PRIMARY))
    if new_style and a.status == "active":       # PATCH_PSEL_CONTACT_FIELDS_V1 — فیلدهایِ رسمیِ ادمینِ پاسارگاردِ جدید
        cells.append(btn("🏷 عنوانِ پروفایل", f"psel:cf:{aid}:t", style=ButtonStyle.PRIMARY))
        cells.append(btn("🆘 آدرسِ پشتیبانی", f"psel:cf:{aid}:s", style=ButtonStyle.PRIMARY))
    if a.status == "active" and await _perm(row, "psel_allow_pw"):
        cells.append(btn("🔑 رمزِ تازه", f"psel:pw:{aid}", style=ButtonStyle.PRIMARY))
    rows = [cells[k:k + 2] for k in range(0, len(cells), 2)]
    if a.status == "active" and await _perm(row, "psel_allow_toggle"):
        rows.append([btn("⏸ خاموشِ سرویس", f"psel:tg:{aid}:0", style=ButtonStyle.DANGER),
                     btn("▶️ روشنِ سرویس", f"psel:tg:{aid}:1", style=ButtonStyle.SUCCESS)])
    if await _perm(row, "psel_allow_delete"):
        rows.append([btn("🗑 حذفِ دائمیِ سرویس", f"psel:dl:{aid}", style=ButtonStyle.DANGER)])
    rows.append(back_row("psel:my"))
    await edit_or_send(cb, text, kb(*rows))
    await cb.answer()


def _bar(pct: float, n_blocks: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * n_blocks))
    return "█" * filled + "░" * (n_blocks - filled)


@psel_user_router.callback_query(F.data.regexp(r"^psel:(sp|nt):\d+$"))
async def spec_page(cb: CallbackQuery, bot: Bot):
    """PATCH_PSEL_SPEC_PAGE_V1 — «📊 مشخصات و مصرف»: صفحهٔ کاملِ سرویس (مثلِ رباتِ VPN) + «اعلانِ حجم» روشن/خاموش."""
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if parts[1] == "nt":                                # روشن/خاموشِ هشدارِ نزدیکِ-پایانِ حجم (پرچمِ nt0 = خاموش)
        async with Session() as s:
            acc = await s.get(PanelSellerAccount, a.id, with_for_update=True)
            fl = SV._flags(acc)
            fl.symmetric_difference_update({"nt0"})
            SV._set_flags(acc, fl)
            await s.commit()
            a = acc
    async with Session() as s:
        panel = await s.get(VpnPanel, a.panel_id)
    notif_on = "nt0" not in SV._flags(a)
    lines = ["📋 <b>مشخصاتِ سرویس</b>", "", "📌 این اطلاعات برایِ ورود به پنل و پیگیریِ سرویسِ شماست.", "",
             f"• نامِ نمایشی: <b>{esc(a.title)}</b>",
             f"• نامِ کاربریِ ورود به پنل: <code>{esc(a.admin_username)}</code>"]
    _has_pw = await SV.get_secret(a.id) is not None
    _show_pw = await _perm(row, "psel_allow_showpw") and _has_pw and a.status in ("active", "suspended")
    if _show_pw:
        lines.append("• رمزِ پنل: با دکمهٔ «🔑 نمایشِ رمز» ببین")
    elif await _perm(row, "psel_allow_pw"):
        lines.append("• رمزِ پنل: برایِ این سرویس ذخیره نشده؛ از «🔑 رمزِ تازه» بگیر")
    lines.append(f"• کدِ سرویس: <code>{esc(a.tracking_code or a.id)}</code>")
    lines.append("")
    live = await _live_usage(a, panel) if (panel and a.status == "active") else None
    if a.data_gb:
        lines.append(f"• سقفِ حجم: <b>{fa(int(a.data_gb))} GB</b>")
        if live:
            used = live[0]
            left = max(0.0, a.data_gb - used)
            pct = used / a.data_gb * 100
            lines += [f"• مصرف‌شده: <b>{fa(f'{used:.2f}')} GB</b>", f"• باقی‌مانده: <b>{fa(f'{left:.2f}')} GB</b>",
                      f"• نمودارِ مصرف: <code>{_bar(pct)}</code> {fa(int(pct + 0.5))}٪"]
        else:
            lines.append("• مصرفِ زنده الان از پنل خوانده نشد؛ کمی بعد دوباره امتحان کن.")
        lines.append(f"• اعلانِ پایانِ حجم: <b>{'🟢 فعال' if notif_on else '🔴 خاموش'}</b>")
    else:
        lines.append("• حجم: <b>نامحدود</b>")
        if live:
            lines.append(f"• مصرف‌شده: <b>{fa(f'{live[0]:.2f}')} GB</b>")
    if live:
        lines.append(f"• کاربران: <b>{fa(live[1])}</b> از {users_text(a.max_users)}")
    lines += ["", f"• ⏳ پایان: {exp_text(a.expires_at)}", f"• وضعیت: {STATUS_FA.get(a.status, a.status)}"]
    rows = []
    if panel and (panel.base_url or "").startswith(("http://", "https://")):
        rows.append([btn("🌐 ورود به پنل وب", url=panel.base_url, style=ButtonStyle.PRIMARY)])
    if _show_pw:
        rows.append([btn("🔑 نمایشِ رمزِ پنل", f"psel:pws:{a.id}", style=ButtonStyle.PRIMARY)])
    if a.data_gb:
        rows.append([btn("🔕 خاموش‌کردنِ اعلانِ حجم" if notif_on else "🔔 روشن‌کردنِ اعلانِ حجم", f"psel:nt:{a.id}",
                         style=ButtonStyle.DANGER if notif_on else ButtonStyle.SUCCESS)])
    rows.append([btn("🔄 بروزرسانی", f"psel:sp:{a.id}", style=ButtonStyle.PRIMARY)])
    rows.append(back_row(f"psel:a:{a.id}", "⬅️ بازگشت به پنل"))
    await edit_or_send(cb, "\n".join(lines), kb(*rows))
    await cb.answer()


@psel_user_router.callback_query(F.data.regexp(r"^psel:pws:\d+$"))
async def show_password(cb: CallbackQuery, bot: Bot):
    """PATCH_PSEL_PASSWORD_VAULT_V1 (Whale): «رمزِ پنل را گم می‌کنم» — رمزِ ذخیره‌شدهٔ (رمزنگاری‌شده) همین مشتری، فقط به خودش."""
    row, a = await _acc_for(cb, bot, int(cb.data.split(":")[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_showpw"):
        return await cb.answer("نمایشِ رمز توسطِ مدیر بسته است.", show_alert=True)
    pw = await SV.get_secret(a.id)
    if pw is None:
        return await cb.answer("رمز برایِ این سرویس ذخیره نشده؛ از «🔑 رمزِ تازه» استفاده کن.", show_alert=True)
    await cb.message.answer(
        f"🔑 <b>اطلاعاتِ ورود</b>\n\n🆔 <code>{esc(a.admin_username)}</code>\n🔑 <code>{esc(pw)}</code>\n\n"
        "⚠️ بعد از کپی‌کردن، این پیام را پاک کن.", parse_mode="HTML")
    await cb.answer()


@psel_user_router.callback_query(F.data.regexp(r"^psel:(dl|dlok):\d+$"))
async def delete_flow(cb: CallbackQuery, bot: Bot):
    """PATCH_PSEL_ACCOUNT_BUTTONS_V1 — حذفِ دائمیِ پنل توسطِ خودِ مشتری (فقط اگر مالک اجازه داده؛ با تأییدِ دو مرحله‌ای)."""
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_delete"):
        return await cb.answer("حذفِ پنل توسطِ مدیر بسته است.", show_alert=True)
    if parts[1] == "dl":
        return await edit_or_send(
            cb, f"🗑 <b>حذفِ دائمیِ {esc(a.title)}</b>\n\nادمینِ <code>{esc(a.admin_username)}</code> و <b>همهٔ کاربرانش</b> از پنل پاک می‌شود "
                "و قابلِ برگشت نیست. مبلغی برگردانده نمی‌شود.\n\nمطمئنی؟",
            kb([btn("✅ بله، برای همیشه حذف کن", f"psel:dlok:{a.id}", style=ButtonStyle.DANGER)], back_row(f"psel:a:{a.id}")))
    await cb.answer("در حالِ حذف…")
    if not await SV.delete_account(row.id, a.id):
        return await edit_or_send(cb, "❌ حذف انجام نشد؛ کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", kb(back_row(f"psel:a:{a.id}")))
    await edit_or_send(cb, "✅ پنل برایِ همیشه حذف شد.", kb(back_row("psel:my")))


class PselAccFSM(StatesGroup):
    rename = State()
    contact = State()
    extra_gb = State()       # PATCH_PSEL_XG_CUSTOM_V1 — مقدارِ دلخواهِ حجمِ اضافه


def _support_url(raw: str) -> str | None:
    """«@id»/«id»/«t.me/id»/«https://…» ⇒ لینکِ https (مقدارِ support_url که در لینکِ اشتراکِ مشتری‌هایِ همین ادمین نشان داده می‌شود)."""
    import re
    v = (raw or "").strip()
    if not v or len(v) > 200 or re.search(r"[\s<>\"']", v):
        return None
    if v.startswith(("http://", "https://")):
        return v
    if v.startswith(("t.me/", "telegram.me/")):
        return "https://" + v
    v = v.lstrip("@")
    return f"https://t.me/{v}" if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", v) else None


@psel_user_router.callback_query(F.data.regexp(r"^psel:rnm:\d+$"))
async def rename_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row, a = await _acc_for(cb, bot, int(cb.data.split(":")[2]))
    if not a:
        return
    await state.clear()
    await state.set_state(PselAccFSM.rename)
    await state.update_data(acc_id=a.id)
    await edit_or_send(cb, "✏️ نامِ جدیدِ سرویس را بفرست (حداکثر ۶۰ نویسه؛ فقط برایِ خودت نمایش داده می‌شود):", kb(back_row(f"psel:a:{a.id}", "✖️ لغو")))
    await cb.answer()


@psel_user_router.message(PselAccFSM.rename, F.text)
async def rename_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    d = await state.get_data()
    name = (msg.text or "").strip()
    if not row or not d.get("acc_id") or not (1 <= len(name) <= 60):
        return await msg.answer("❌ نام ۱ تا ۶۰ نویسه باشد.")
    async with Session() as s:
        a = await s.get(PanelSellerAccount, int(d["acc_id"]), with_for_update=True)
        if not a or a.bot_id != row.id or a.buyer_tg != msg.from_user.id or a.status == "deleted":
            await state.clear()
            return
        a.title = name
        await s.commit()
        aid = a.id
    await state.clear()
    await msg.answer("✅ نامِ سرویس عوض شد.", reply_markup=kb(back_row(f"psel:a:{aid}", "🗂 بازگشت به پنل")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:cf:\d+:[st]$"))
async def contact_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if a.status != "active":
        return await cb.answer("فقط برایِ پنلِ فعال.", show_alert=True)
    await state.clear()
    await state.set_state(PselAccFSM.contact)
    await state.update_data(acc_id=a.id, field=parts[3])
    if parts[3] == "s":
        txt = ("🆘 <b>تنظیمِ آدرسِ پشتیبانی</b>\n\n"
               "<b>این فیلد چیست؟</b>\nآدرسِ پشتیبانی‌ای که در صفحهٔ اشتراکِ کلاینت‌هایِ (مشتری‌هایِ) تو نشان داده می‌شود.\n\n"
               "<b>چه چیزی بفرستی؟</b>\nیوزرنیم با @ یا لینکِ کاملِ تلگرام.\n"
               "مثال: <code>@MySupport</code>\nیا: <code>https://t.me/MySupport</code>\n\n"
               "⚠️ آدرسِ اشتباه = لینکِ خراب برایِ مشتری‌هایِ تو.\n\n"
               "📌 مقدار را همین الان بفرست.\n📌 برایِ پاک‌کردن: دکمهٔ «پاک کردن» یا ارسالِ <code>-</code>.")
    else:
        txt = ("🏷 <b>عنوانِ پروفایل</b>\n\n<b>این فیلد چیست؟</b>\nعنوانی که در برنامهٔ مشتری‌هایِ تو بالایِ اشتراک دیده می‌شود "
               "(مثلاً نامِ برندِ خودت).\n\n📌 عنوان را همین الان بفرست (حداکثر ۸۰ نویسه).\n📌 برایِ پاک‌کردن: دکمهٔ «پاک کردن» یا ارسالِ <code>-</code>.")
    await edit_or_send(cb, txt, kb([btn("🗑 پاک کردن", f"psel:cfc:{a.id}:{parts[3]}", style=ButtonStyle.DANGER)],
                                   back_row(f"psel:a:{a.id}", "✖️ لغو")))
    await cb.answer()


async def _apply_contact(bot: Bot, row, uid: int, aid: int, kw: dict, reply) -> bool:
    """PUT روی ادمینِ پنلِ جدید (مشترکِ «ثبت» و «پاک کردن»). reply: تابعِ ارسالِ پیام."""
    async with Session() as s:
        a = await s.get(PanelSellerAccount, aid)
        panel = await s.get(VpnPanel, a.panel_id) if a else None
    if not a or a.bot_id != row.id or a.buyer_tg != uid or a.status != "active" or not panel:
        return False
    try:
        api = AD.PanelAdminAPI.for_panel(panel)
        try:
            if not await api.is_new_style(a.admin_username):
                await reply("این پنل از این قابلیت پشتیبانی نمی‌کند.")
                return False
            await api.put_admin(a.admin_username, **kw)       # رشتهٔ خالی = پاک‌کردن (کدِ رسمی فقط None را نادیده می‌گیرد)
        finally:
            await api.close()
    except Exception as e:      # noqa: BLE001
        logger.warning("panel_seller: تنظیمِ فیلدِ تماسِ ادمین ناموفق (acc=%s): %s", aid, e)
        await tell_owner_failure(bot, row, uid, "تنظیمِ اطلاعاتِ پروفایلِ پنل", str(e))
        await reply("❌ پنل جواب نداد؛ کمی بعد دوباره امتحان کن.")
        return False
    return True


@psel_user_router.callback_query(F.data.regexp(r"^psel:cfc:\d+:[st]$"))
async def contact_clear(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    await state.clear()
    kw = {"support_url": ""} if parts[3] == "s" else {"profile_title": ""}

    async def _reply(txt):
        await cb.message.answer(txt)
    if await _apply_contact(bot, row, cb.from_user.id, a.id, kw, _reply):
        await edit_or_send(cb, "✅ پاک شد.", kb(back_row(f"psel:a:{a.id}", "🗂 بازگشت به پنل")))
    await cb.answer()


@psel_user_router.message(PselAccFSM.contact, F.text)
async def contact_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    d = await state.get_data()
    if not row or not d.get("acc_id"):
        return await state.clear()
    raw = (msg.text or "").strip()
    if raw in ("-", "—", "پاک", "پاک کردن"):                      # پاک‌کردن (مثلِ رباتِ VPN)
        kw = {"support_url": ""} if d.get("field") == "s" else {"profile_title": ""}
    elif d.get("field") == "s":
        val = _support_url(raw)
        if not val:
            return await msg.answer("❌ آیدی/لینکِ نامعتبر. مثلاً <code>@my_support</code> یا <code>https://t.me/my_support</code> بفرست.", parse_mode="HTML")
        kw = {"support_url": val}
    else:
        if not (1 <= len(raw) <= 80):
            return await msg.answer("❌ عنوان ۱ تا ۸۰ نویسه باشد.")
        kw = {"profile_title": raw}
    aid = int(d["acc_id"])
    ok = await _apply_contact(bot, row, msg.from_user.id, aid, kw, msg.answer)
    await state.clear()
    if not ok:
        return
    cleared = all(v == "" for v in kw.values())
    await msg.answer(("✅ پاک شد." if cleared else "✅ در پنل ثبت شد.")
                     + ("\nدر لینکِ اشتراکِ مشتری‌هایِ تو نشان داده می‌شود." if "support_url" in kw and not cleared else ""),
                     reply_markup=kb(back_row(f"psel:a:{aid}", "🗂 بازگشت به پنل")))


_XG_PICKS = (5, 10, 20, 50, 100)      # پیش‌فرض؛ مقدارِ واقعی از تنظیماتِ مالک (SV.extra_gb_picks)


@psel_user_router.callback_query(F.data.regexp(r"^psel:xg:\d+$"))
async def extra_gb_menu(cb: CallbackQuery, bot: Bot):
    row, a = await _acc_for(cb, bot, int(cb.data.split(":")[2]))
    if not a:
        return
    unit = await SV.extra_gb_price(row.id)
    if unit <= 0 or not a.data_gb:
        return await cb.answer("خریدِ حجمِ اضافه برایِ این پنل فعال نیست.", show_alert=True)
    picks = [btn(f"{fa(g)} گیگ · {n(g * unit)} ت", f"psel:xb:{a.id}:{g}", style=ButtonStyle.PRIMARY)
             for g in await SV.extra_gb_picks(row.id)]
    rows = [picks[i:i + 2] for i in range(0, len(picks), 2)]
    rows.append([btn("✏️ حجمِ دلخواه (هر مقدار)", f"psel:xc:{a.id}", style=ButtonStyle.SUCCESS)])     # PATCH_PSEL_XG_CUSTOM_V1
    rows.append(back_row(f"psel:a:{a.id}"))
    note = ("از کیف‌پول کسر می‌شود و همین‌جا به حجمِ این پنل اضافه می‌شود؛ این پنل مدت‌دار نیست و حجمِ اضافه دائمی است."
            if not a.expires_at else
            "از کیف‌پول کسر می‌شود و همین‌جا به حجمِ همین دوره اضافه می‌شود (با تمدید، حجم به مقدارِ بسته برمی‌گردد).")
    await edit_or_send(cb, f"📈 <b>حجمِ اضافه برایِ {esc(a.title)}</b>\n\n💵 هر گیگ: <b>{n(unit)}</b> تومان\n"
                           f"📦 حجمِ فعلی: {gb_text(a.data_gb)}\n\n{note}", kb(*rows))
    await cb.answer()


# === PATCH_PSEL_XG_CUSTOM_V1 === (Whale: «بخشِ افزایشِ حجم هم دلخواه باشد»)
@psel_user_router.callback_query(F.data.regexp(r"^psel:xc:\d+$"))
async def extra_gb_custom_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row, a = await _acc_for(cb, bot, int(cb.data.split(":")[2]))
    if not a:
        return
    unit = await SV.extra_gb_price(row.id)
    if unit <= 0 or not a.data_gb:
        return await cb.answer("خریدِ حجمِ اضافه برایِ این پنل فعال نیست.", show_alert=True)
    await state.clear()
    await state.set_state(PselAccFSM.extra_gb)
    await state.update_data(acc_id=a.id)
    lo, hi = await SV.extra_gb_range(row.id)
    await edit_or_send(cb, f"✏️ چند گیگ حجمِ اضافه می‌خواهی؟ یک عددِ {fa(lo)} تا {fa(hi)} بفرست.\n"
                           f"💵 هر گیگ: <b>{n(unit)}</b> تومان", kb(back_row(f"psel:xg:{a.id}", "✖️ لغو")))
    await cb.answer()


@psel_user_router.message(PselAccFSM.extra_gb, F.text)
async def extra_gb_custom_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    d = await state.get_data()
    raw = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).replace(",", "")
    if raw in ("/cancel", "لغو"):
        await state.clear()
        return await msg.answer("لغو شد.")
    if not row or not d.get("acc_id"):
        await state.clear()
        return
    lo, hi = await SV.extra_gb_range(row.id)
    if not raw.isdigit() or not (lo <= int(raw) <= hi):
        return await msg.answer(f"❌ یک عددِ صحیحِ {fa(lo)} تا {fa(hi)} بفرست (یا /cancel).")
    gb = int(raw)
    async with Session() as s:
        a = await s.get(PanelSellerAccount, int(d["acc_id"]))
    if not a or a.bot_id != row.id or a.buyer_tg != msg.from_user.id or a.status == "deleted" or not a.data_gb:
        await state.clear()
        return await msg.answer("این پنل الان قابلِ افزودنِ حجم نیست.")
    unit = await SV.extra_gb_price(row.id)
    await state.clear()
    await msg.answer(f"🧾 <b>تأییدِ خریدِ حجمِ اضافه</b>\n\n📈 {fa(gb)} گیگ برایِ «{esc(a.title)}»\n💰 مبلغ: <b>{n(gb * unit)}</b> تومان "
                     "(از کیف‌پول کسر می‌شود)",
                     reply_markup=kb([btn("✅ خرید", f"psel:xb:{a.id}:{gb}", style=ButtonStyle.SUCCESS)],
                                     back_row(f"psel:xg:{a.id}", "✖️ لغو")), parse_mode="HTML")


@psel_user_router.callback_query(F.data.regexp(r"^psel:xb:\d+:\d+$"))
async def extra_gb_buy(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    await cb.answer("در حالِ ثبت…")
    r = await SV.add_volume(row.id, a.id, cb.from_user.id, int(parts[3]), bot=bot, row=row)
    st = r["status"]
    if st == "insufficient":
        from tenant.shared import vs_pay
        return await vs_pay.insufficient_notice(cb.message, row.id, cb.from_user.id, int(r["need"]), cb.data)
    if st != "ok":
        msg = {"disabled": "خریدِ حجمِ اضافه فعال نیست.", "unlimited": "حجمِ این پنل نامحدود است.",
               "bad_amount": "مقدارِ نامعتبر.", "not_found": "این پنل الان قابلِ افزودنِ حجم نیست."}.get(st)
        if st == "panel_failed":
            await tell_owner_failure(bot, row, cb.from_user.id, "افزودنِ حجم", r.get("error"))
            msg = "پنل جواب نداد؛ مبلغی کم نشد. کمی بعد دوباره امتحان کن."
        return await edit_or_send(cb, "❌ " + (msg or "انجام نشد."), kb(back_row(f"psel:a:{a.id}")))
    extra = f"\n🔑 رمزِ تازه (چون پنل بسته شده بود): <code>{esc(r['new_password'])}</code>" if r.get("new_password") else ""
    await edit_or_send(cb, f"✅ حجمِ اضافه ثبت شد. حجمِ جدید: <b>{gb_text(r['new_gb'])}</b>{extra}", kb(back_row(f"psel:a:{a.id}")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:(rn|rnok):\d+$"))
async def renew_flow(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_renew"):
        return await cb.answer("تمدید توسطِ مدیر بسته است.", show_alert=True)
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    async with Session() as s:
        pk = await s.get(PanelSellerPackage, a.package_id) if a.package_id else None
    if not pk or not pk.active:
        return await cb.answer("بسته‌یِ این پنل دیگر برایِ تمدید موجود نیست؛ با مدیر هماهنگ کن.", show_alert=True)
    if parts[1] == "rn":
        return await edit_or_send(cb, f"🔁 تمدیدِ <b>{esc(a.title)}</b>\n\n{pk_line(pk)}\n\nاز کیف‌پول کسر می‌شود.",
                                  kb([btn("✅ تأیید و پرداخت", f"psel:rnok:{a.id}", style=ButtonStyle.SUCCESS)], back_row(f"psel:a:{a.id}")))
    await cb.answer("در حالِ تمدید…")
    r = await SV.renew(row.id, a.id, cb.from_user.id)
    if r["status"] == "insufficient":
        from tenant.shared import vs_pay
        return await vs_pay.insufficient_notice(cb.message, row.id, cb.from_user.id, int(r["need"]), cb.data)
    if r["status"] == "panel_failed":
        await tell_owner_failure(bot, row, cb.from_user.id, "تمدیدِ پنل", r.get("error"))
    if r["status"] != "ok":
        return await edit_or_send(cb, "❌ تمدید انجام نشد" + (f":\n<code>{esc(r.get('error'))}</code>" if r.get("error") else ".")
                                  , kb(back_row(f"psel:a:{a.id}")))
    extra = f"\n🔑 رمزِ تازه: <code>{esc(r['new_password'])}</code>" if r.get("new_password") else ""
    await edit_or_send(cb, f"✅ تمدید شد. پایان: {exp_text(r['expires_at'])}{extra}", kb(back_row(f"psel:a:{a.id}")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:(pw|pwok):\d+$"))
async def password_flow(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_pw"):
        return await cb.answer("تغییرِ رمز توسطِ مدیر بسته است.", show_alert=True)
    if parts[1] == "pw":
        return await edit_or_send(cb, "🔑 رمزِ فعلیِ پنل باطل و رمزِ تازه ساخته می‌شود. ادامه می‌دهی؟",
                                  kb([btn("✅ بله، رمزِ تازه", f"psel:pwok:{a.id}", style=ButtonStyle.DANGER)], back_row(f"psel:a:{a.id}")))
    await cb.answer("در حالِ تغییر…")
    r = await SV.change_password(row.id, a.id, cb.from_user.id)
    if r["status"] != "ok":
        return await edit_or_send(cb, "❌ تغییرِ رمز انجام نشد" + (f":\n<code>{esc(r.get('error'))}</code>" if r.get("error") else "."),
                                  kb(back_row(f"psel:a:{a.id}")))
    await edit_or_send(cb, f"✅ رمزِ تازه:\n🆔 <code>{esc(r['username'])}</code>\n🔑 <code>{esc(r['password'])}</code>",
                       kb(back_row(f"psel:a:{a.id}")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:tg:\d+:[01]$"))
async def toggle_flow(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_toggle"):
        return await cb.answer("این کار توسطِ مدیر بسته است.", show_alert=True)
    await cb.answer("در حالِ انجام…")
    r = await SV.set_users_enabled(row.id, a.id, cb.from_user.id, parts[3] == "1")
    if r["status"] == "partial":                                   # PATCH_PSEL_VERIFY_STATE_V1
        msg = f"⚠️ پنل {fa(r['remaining'])} کاربر را تغییر نداد؛ بعداً دوباره امتحان کن یا به مدیر خبر بده."
    else:
        msg = "✅ انجام شد." if r["status"] == "ok" else "❌ " + (r.get("error") or _ERR_FA.get(r["status"], "انجام نشد."))
    await cb.message.answer(msg[:300])
