"""
tenant/gateway_redirect.py — «لینکِ ریدایرکت» برایِ درگاه‌هایِ زرین‌پال و زیبال.

=== PATCH_GATEWAY_REDIRECT_LINK_V1 ===
(جایگزینِ تک‌درگاهیِ PATCH_ZARINPAL_REFERRER_PAGE_V1؛ همان کلیدِ تنظیمِ
zarinpal_referrer_page حفظ شده تا چیزی که مالک‌ها قبلاً ثبت کرده‌اند بماند.)

چرا: شاپرک/زرین‌پال/زیبال پرداختی را که «ارجاع‌دهنده» (Referrer) اش با دامنهٔ
ثبت‌شدهٔ درگاه یکی نیست رد می‌کنند. لینکی که مستقیم از تلگرام باز شود ارجاع‌دهنده
ندارد → «دسترسی از این دامنه مجاز نمی‌باشد». پس کاربر باید اول به یک صفحه رویِ
دامنهٔ خودِ مالک برود و از آن‌جا به درگاه هدایت شود.

هر درگاه لینکِ ریدایرکتِ جدا دارد (ممکن است مرچنت‌ها رویِ دامنه‌هایِ جدا باشند).
سه حالت:
  • خالی            → مثلِ قبل مستقیم به درگاه.
  • «خودکار»        → صفحهٔ واسطِ خودِ سرورِ ما رویِ دامنهٔ callback
                       (‎/gw/<gw>/start/<ref>‎ در main_tenant.py). فقط وقتی کار
                       می‌کند که دامنهٔ callback واقعاً به همین سرور وصل باشد —
                       که برایِ تأییدِ خودکارِ پرداخت هم از قبل لازم بود.
  • آدرسِ دلخواه     → صفحه‌ای که مالک خودش رویِ سایتش گذاشته (مثلِ pay.html).
                       قراردادِ مرسوم: <پایه>/start/<ref> ؛ یا هر آدرس با {ref}.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

gateway_redirect_router = Router()

REF_RE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")

GATEWAYS: dict[str, dict] = {
    "zarinpal": {"label": "زرین‌پال", "key": "zarinpal_referrer_page",
                 "back": "set:zarinpal", "base_key": "zarinpal_webhook_base"},
    "zibal":    {"label": "زیبال", "key": "zibal_referrer_page",
                 "back": "set:zibal", "base_key": "zibal_webhook_base"},
}


class GatewayRedirectFSM(StatesGroup):
    link = State()


def direct_startpay(gw: str, ref: str) -> str:
    if gw == "zibal":
        import platform_zibal
        return platform_zibal.startpay_url(ref)
    import platform_zarinpal
    return platform_zarinpal.startpay_url(ref)


# === PATCH_GATEWAY_REDIRECT_PG_STARTPAY_V1 ===
# گزارش: سایتِ expresspanel.ir برایِ زرین‌پال به‌جایِ الگویِ معمولِ
# «/start/{ref}» از الگویِ خودِ StartPayِ زرین‌پال استفاده می‌کند:
# «/zarpay/pg/StartPay/{authority}». هر دو حالت را پشتیبانی می‌کنیم:
# مالک یا فقط پایه («.../zarpay/pg») می‌فرستد یا کلِ آدرسِ نمونه (با یک
# authorityِ واقعی در انتها) را کپی‌پیست می‌کند.
_TOKEN_TAIL_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")


def normalize_link(value: str, gw: str = "zarinpal") -> str | None:
    """ورودیِ مالک → الگویِ ذخیره‌شده (با {ref})؛ نامعتبر → None."""
    v = (value or "").strip()
    if not v or any(ch.isspace() for ch in v):
        return None
    v = v.replace("{authority}", "{ref}").replace("{trackId}", "{ref}")
    parsed = urlparse(v.replace("{ref}", "REF"))
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    if "{ref}" in v:
        return v
    base, _, tail = v.rstrip("/").rpartition("/")
    if base and tail.lower() in ("start", "startpay"):
        # آدرس با «/StartPay/» یا «/start/» (با یا بدون اسلشِ آخر) تمام
        # می‌شود؛ مسیر همین حالا کامل است، فقط ref را مستقیم اضافه می‌کنیم.
        return v.rstrip("/") + "/{ref}"
    if base and _TOKEN_TAIL_RE.match(tail):
        # کلِ لینکِ نمونه پیست شده (با یک authority/trackId واقعی در انتها)؛
        # فقط همان توکنِ آخر با {ref} عوض می‌شود، مسیر دست نمی‌خورد.
        return base + "/{ref}"
    path = parsed.path.lower().rstrip("/")
    if path.endswith("/pg"):
        return v.rstrip("/") + "/StartPay/{ref}"
    if parsed.query or path.endswith((".html", ".htm", ".php")):
        return v + ("&" if parsed.query else "?") + f"gw={gw}&a={{ref}}"
    return v.rstrip("/") + "/start/{ref}"


async def _callback_base(bot_id: int, gw: str) -> str:
    if gw == "zibal":
        from tenant.gateways.zibal_wallet import _callback_base as cb
    else:
        from tenant.gateways.zarinpal_wallet import _callback_base as cb
    return (await cb(bot_id)) or ""


async def get_link(bot_id: int, gw: str) -> str:
    from tenant import get_setting
    return (await get_setting(bot_id, GATEWAYS[gw]["key"], "")).strip()


async def startpay_for(bot_id: int, gw: str, ref: str) -> str:
    """لینکی که به کاربر داده می‌شود."""
    tpl = await get_link(bot_id, gw)
    tpl = tpl.replace("{authority}", "{ref}")
    if tpl == "auto":
        base = await _callback_base(bot_id, gw)
        if base:
            return f"{base.rstrip('/')}/gw/{gw}/start/{ref}"
        return direct_startpay(gw, ref)
    if "{ref}" in tpl:
        return tpl.replace("{ref}", ref)
    return direct_startpay(gw, ref)


async def status_line(bot_id: int, gw: str) -> str:
    tpl = await get_link(bot_id, gw)
    if not tpl:
        return "لینک ریدایرکت: ⚠️ ثبت نشده (مستقیم به درگاه)"
    if tpl == "auto":
        return "لینک ریدایرکت: ✅ خودکار (رویِ دامنهٔ callback)"
    return f"لینک ریدایرکت: ✅ {tpl.replace('{ref}', '…')}"


def menu_button(gw: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🔗 لینک ریدایرکت", callback_data=f"gwr:menu:{gw}",
                                 style=ButtonStyle.PRIMARY)]


def _menu_text(gw: str, current: str, base: str) -> str:
    label = GATEWAYS[gw]["label"]
    cur = ("خودکار (رویِ دامنهٔ callback)" if current == "auto"
           else (current.replace("{ref}", "…") if current else "ثبت نشده — مستقیم به درگاه"))
    return (
        f"🔗 <b>لینک ریدایرکتِ {label}</b>\n\n"
        f"وضعیتِ فعلی: <b>{cur}</b>\n\n"
        f"{label} پرداختی را که مستقیم از تلگرام باز شود رد می‌کند "
        "(«دسترسی از این دامنه مجاز نمی‌باشد»). کاربر باید اول از "
        "<b>دامنهٔ ثبت‌شده در پنلِ درگاه</b> رد شود و بعد به درگاه برسد.\n\n"
        "⚡️ <b>خودکار</b>: اگر دامنهٔ callbackات "
        f"({base or 'ثبت نشده'}) همان دامنهٔ ثبت‌شده در {label} است و به همین ربات "
        "وصل است، این را بزن — چیزی لازم نیست آپلود کنی.\n\n"
        "✏️ <b>لینکِ دلخواه</b>: اگر صفحهٔ ریدایرکت از قبل رویِ سایتِ خودت هست، "
        "آدرسِ پایه‌اش را بفرست. لینکِ نهایی این‌طور ساخته می‌شود:\n"
        "<code>https://example.com/payment/start/1234567890</code>\n\n"
        "🆕 <b>اگر صفحه‌ای نداری:</b>\n"
        "۱. دکمهٔ «📎 فرستادنِ فایلِ pay.html» را بزن؛ فایل همین‌جا برایت می‌آید.\n"
        "۲. همین فایل را، دقیقاً به همین اسم، رویِ هاستِ سایتِ خودت در پوشهٔ "
        f"<code>public_html</code> آپلود کن (باید با آدرسِ <code>https://دامنه‌ات/pay.html</code> "
        "باز شود — مرورگرِ خودت را امتحان کن).\n"
        "۳. برگرد این‌جا، «✏️ ثبتِ لینکِ دلخواه» را بزن و همین آدرس را بفرست: "
        "<code>https://دامنه‌ات/pay.html</code>\n"
        "۴. «🧪 تست اتصال» را بزن و صفحهٔ پرداختِ آزمایشی را باز کن — باید همین درگاه باز شود."
    )


# === PATCH_GATEWAY_REDIRECT_PAYHTML_SENDFILE_V1 ===
# روشِ دوم (بدونِ نیاز به اینکه دامنهٔ پلتفرم عمومی/در دسترس باشد): خودِ
# فایل مستقیم به‌عنوانِ سندِ تلگرامی در همین چت فرستاده می‌شود. کافی است
# فایلِ pay.html در این مسیر، رویِ سرور، کنارِ خودِ کدِ بات‌ساز باشد.
import os as _os
from pathlib import Path as _Path

PAY_HTML_PATH = _Path(_os.getenv("PAY_HTML_PATH", "")
                      or (_Path(__file__).resolve().parent.parent / "assets" / "pay.html"))


def payhtml_available() -> bool:
    try:
        return PAY_HTML_PATH.is_file() and PAY_HTML_PATH.stat().st_size > 0
    except Exception:
        return False


def _menu_kb(gw: str, has_base: bool, current: str) -> InlineKeyboardMarkup:
    rows = []
    if has_base:
        rows.append([InlineKeyboardButton(text="⚡️ خودکار (دامنهٔ callback)", callback_data=f"gwr:auto:{gw}",
                                          style=ButtonStyle.SUCCESS)])
    # === PATCH_GATEWAY_REDIRECT_PAYHTML_SENDFILE_V1 ===
    # فقط از همین چت فرستاده می‌شود؛ دیگر لینکِ دانلودِ مستقیم روی دامنهٔ
    # پلتفرم نیست — آن لینک آدرس/آی‌پیِ سرورِ اصلی را به هر مستأجری نشان
    # می‌داد. اندپوینتِ /assets/pay.html هم پایین همین فایل غیرفعال شده.
    if payhtml_available():
        rows.append([InlineKeyboardButton(text="📎 فرستادنِ فایلِ pay.html در همین چت",
                                          callback_data="gwr:sendfile")])
    rows.append([InlineKeyboardButton(text="✏️ ثبتِ لینکِ دلخواه", callback_data=f"gwr:set:{gw}",
                                      style=ButtonStyle.PRIMARY)])
    if current:
        rows.append([InlineKeyboardButton(text="🗑 حذف (مستقیم به درگاه)", callback_data=f"gwr:clear:{gw}",
                                          style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=GATEWAYS[gw]["back"],
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _owner_row(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    return await _owner_ok(cb, bot)


def _gw_from(data: str) -> str | None:
    gw = data.rsplit(":", 1)[-1]
    return gw if gw in GATEWAYS else None


async def _show_menu(target: Message, bot_id: int, gw: str, edit: bool, note: str = "") -> None:
    current = await get_link(bot_id, gw)
    base = await _callback_base(bot_id, gw)
    text = (note + "\n\n" if note else "") + _menu_text(gw, current, base)
    kb = _menu_kb(gw, bool(base), current)
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            logger.debug("gwr: ویرایشِ پیام ممکن نشد؛ پیامِ تازه")
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


@gateway_redirect_router.callback_query(F.data.startswith("gwr:menu:"))
async def gwr_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    gw = _gw_from(cb.data)
    row = await _owner_row(cb, bot)
    if not row or not gw:
        return
    await state.clear()
    await _show_menu(cb.message, row.id, gw, edit=True)
    await cb.answer()


@gateway_redirect_router.callback_query(F.data.startswith("gwr:auto:"))
async def gwr_auto(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    gw = _gw_from(cb.data)
    row = await _owner_row(cb, bot)
    if not row or not gw:
        return
    if not await _callback_base(row.id, gw):
        await cb.answer("اول دامنهٔ callback را ثبت کن.", show_alert=True)
        return
    await set_setting(row.id, GATEWAYS[gw]["key"], "auto")
    await cb.answer("✅ ذخیره شد")
    # === PATCH_GATEWAY_REDIRECT_PROBE_V1 === حالتِ خودکار هم چک می‌شود:
    # اگر دامنهٔ callback به این سرور نمی‌رسد (مثلاً زیردامنهٔ زرین‌پال یا
    # هاستِ دیگر)، هم این صفحه و هم خودِ callback کار نمی‌کنند.
    base = await _callback_base(row.id, gw)
    warn = await _probe_link(f"{base.rstrip('/')}/gw/{gw}/start/{{ref}}")
    if warn:
        warn += ("\n\n⛔ <b>دامنهٔ callback ({}) به سرورِ ربات وصل نیست</b>؛ پس "
                 "callbackِ پرداخت هم به ربات نمی‌رسد. زیردامنهٔ اختصاصیِ خودِ پنلِ "
                 "زرین‌پال را نباید به‌عنوانِ دامنهٔ callback زد.").format(base)
    await _show_menu(cb.message, row.id, gw, edit=True,
                     note="✅ لینکِ ریدایرکت رویِ حالتِ خودکار تنظیم شد." + (f"\n\n{warn}" if warn else ""))


@gateway_redirect_router.callback_query(F.data.startswith("gwr:clear:"))
async def gwr_clear(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    gw = _gw_from(cb.data)
    row = await _owner_row(cb, bot)
    if not row or not gw:
        return
    await set_setting(row.id, GATEWAYS[gw]["key"], "")
    await cb.answer("حذف شد")
    await _show_menu(cb.message, row.id, gw, edit=True, note="🗑 لینکِ ریدایرکت حذف شد.")


@gateway_redirect_router.callback_query(F.data.startswith("gwr:set:"))
async def gwr_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    gw = _gw_from(cb.data)
    row = await _owner_row(cb, bot)
    if not row or not gw:
        return
    await state.set_state(GatewayRedirectFSM.link)
    await state.update_data(gwr_gw=gw)
    await cb.message.answer(
        f"✏️ آدرسِ پایهٔ صفحهٔ ریدایرکتِ {GATEWAYS[gw]['label']} را بفرست.\n\n"
        "• مثال: <code>https://example.com/payment</code>\n"
        "• لینکِ نهایی: <code>https://example.com/payment/start/1234567890</code>\n"
        "• اگر فایلِ pay.html را آپلود کرده‌ای: <code>https://example.com/pay.html</code>\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"gwr:menu:{gw}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


_NAV = {"🏠 بازگشت به خانه", "⬅️ بازگشت", "لغو", "/cancel"}


@gateway_redirect_router.message(GatewayRedirectFSM.link, F.text)
async def gwr_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, set_setting
    row = await get_bot_row(bot)
    data = await state.get_data()
    gw = data.get("gwr_gw")
    if not row or gw not in GATEWAYS or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in _NAV:
        await state.clear()
        await _show_menu(msg, row.id, gw, edit=False, note="لغو شد.")
        return
    default_value = normalize_link(raw, gw)
    if not default_value:
        await msg.answer("❌ آدرس نامعتبر است. یک آدرسِ کاملِ https بفرست، مثلاً https://example.com/payment")
        return

    # === PATCH_GATEWAY_REDIRECT_AUTODETECT_V1 ===
    # گزارش: مالک فقط دامنه/مسیرِ پایه را می‌داند (مثلاً expresspanel.ir/zarpay)،
    # نه اینکه اسکریپتِ خودش دقیقاً «/StartPay/» می‌خواهد یا «/pg/StartPay/»
    # یا «/start/». قبلاً باید حدسِ ما (normalize_link) درست از آب درمی‌آمد
    # وگرنه کاربر یک نمونهٔ ۴۰۴ می‌گرفت (دقیقاً همان گزارشِ فدایی). حالا اگر
    # کاربر خودش صراحتاً {ref} نگذاشته باشد، چند الگویِ رایج را واقعاً روی
    # همان دامنه امتحان می‌کنیم (نه فقط حدس) و هرکدام که واقعاً جواب داد را
    # ذخیره می‌کنیم — کاربر فقط آدرسِ پایه را می‌فرستد، بقیه‌اش با خودِ ربات.
    value = default_value
    auto_note = ""
    if "{ref}" not in raw:
        base = raw.rstrip("/")
        candidates = []
        seen = set()
        for cand in (
            default_value,
            base + "/StartPay/{ref}",
            base + "/pg/StartPay/{ref}",
            base + "/start/{ref}",
            base + "/pg/start/{ref}",
        ):
            if cand and cand not in seen:
                seen.add(cand)
                candidates.append(cand)
        if len(candidates) > 1:
            await msg.answer("⏳ در حالِ بررسیِ چند مسیرِ رایج روی همین آدرس، چند ثانیه صبر کن…")
        for cand in candidates:
            if not await _probe_link(cand):  # رشتهٔ خالی یعنی موفق
                value = cand
                if cand != default_value:
                    auto_note = "🔎 آدرسِ دقیق را خودم رویِ سایتت پیدا کردم؛ لازم نبود بدانی.\n\n"
                break
        # اگر هیچ‌کدام موفق نشد، همان حدسِ پیش‌فرض ذخیره می‌شود (رفتارِ قبلی) و
        # پایین‌تر هشدارِ probe رویِ همان نشان داده خواهد شد — بدتر از قبل نمی‌شود.

    await set_setting(row.id, GATEWAYS[gw]["key"], value)
    await state.clear()
    warn = await _probe_link(value)
    await _show_menu(msg, row.id, gw, edit=False,
                     note=auto_note + "✅ لینکِ ریدایرکت ذخیره شد." + (f"\n\n{warn}" if warn else ""))


# === PATCH_GATEWAY_REDIRECT_PROBE_V1 ===
# گزارش: مالک زیردامنهٔ اختصاصیِ خودِ زرین‌پال (pay.x.ir — CNAME به زرین‌پال)
# را به‌عنوانِ لینکِ ریدایرکت زد → کاربر ۴۰۴ِ زرین‌پال می‌دید. آن زیردامنه
# مالِ سرورِ زرین‌پال است، نه صفحهٔ ریدایرکت. بعد از ذخیره یک بار لینکِ نمونه را
# باز می‌کنیم و اگر جوابِ درستی نیامد همان‌جا هشدار می‌دهیم.
_PROBE_REF = "TESTREF1234567890"


async def _probe_link(tpl: str) -> str:
    try:
        import aiohttp
    except Exception:
        return ""
    url = tpl.replace("{ref}", _PROBE_REF)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, allow_redirects=False) as r:
                status = r.status
                body = (await r.text(errors="ignore"))[:20000] if status < 300 else ""
                loc = r.headers.get("Location", "")
    except Exception as e:
        return (f"⚠️ این آدرس باز نشد ({type(e).__name__}). مطمئن شو دامنه درست است و "
                "SSL دارد، وگرنه پرداخت‌ها کار نمی‌کنند.")
    if status in (301, 302, 303, 307, 308):
        if _PROBE_REF in loc and ("zarinpal" in loc or "zibal" in loc):
            return ""  # اسکریپتِ سمتِ سرور که مستقیم به درگاه ریدایرکت می‌کند — عالی
        return ""
    if status >= 400:
        return (f"⚠️ <b>این آدرس خطای {status} داد.</b> یعنی صفحهٔ ریدایرکت آن‌جا وجود "
                "ندارد و کاربران به خطا می‌خورند.\n"
                "• «زیردامنهٔ اختصاصیِ» پنلِ زرین‌پال (مثلاً pay.mysite.ir) صفحهٔ "
                "ریدایرکت نیست — آن مالِ سرورِ خودِ زرین‌پال است.\n"
                "• لینک باید به سایت/هاستِ خودت اشاره کند؛ ساده‌ترین راه: فایلِ pay.html "
                "را در public_html آپلود کن و آدرسش را بزن.")
    if _PROBE_REF not in body and "StartPay" not in body and "zibal" not in body.lower():
        return ("⚠️ این آدرس باز شد ولی شبیهِ صفحهٔ ریدایرکت نیست. حتماً «🧪 تست اتصال» "
                "را بزن و صفحهٔ پرداختِ آزمایشی را باز کن.")
    return ""


# === PATCH_GATEWAY_REDIRECT_PAYHTML_SENDFILE_V1 ===
@gateway_redirect_router.callback_query(F.data == "gwr:sendfile")
async def gwr_sendfile(cb: CallbackQuery, bot: Bot):
    from aiogram.types import FSInputFile
    row = await _owner_row(cb, bot)
    if not row:
        return
    if not payhtml_available():
        await cb.answer("این فایل فعلاً رویِ سرور نیست؛ از پشتیبانی بپرس.", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.answer_document(
            FSInputFile(PAY_HTML_PATH, filename="pay.html"),
            caption=(
                "📎 این فایل را <b>دقیقاً به همین اسم</b> (pay.html) رویِ هاستِ سایتِ خودت، "
                "در پوشهٔ <code>public_html</code> آپلود کن.\n\n"
                "بعد آدرسِ <code>https://دامنه‌ات/pay.html</code> را در مرورگر باز کن تا مطمئن شوی "
                "بالا می‌آید، سپس همین آدرس را در «✏️ ثبتِ لینکِ دلخواه» بفرست."
            ), parse_mode="HTML")
    except Exception:
        logger.exception("gwr: فرستادنِ pay.html ناموفق بود")
        await cb.message.answer("❌ فرستادنِ فایل ناموفق بود؛ دوباره امتحان کن یا از پشتیبانی بپرس.")
