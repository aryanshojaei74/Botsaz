"""
custom_domain.py — دامنهٔ اختصاصیِ هر ربات (PATCH_CUSTOM_DOMAIN_V1).

به‌جایِ اینکه لینک‌هایِ پرداخت/وب‌اپ/فایلِ یک ربات همیشه زیرِ دامنهٔ مشترکِ
پلتفرم (WEBHOOK_BASE) باشند، مالکِ ربات می‌تواند دامنهٔ خودش را وصل کند.
جریانِ کار:

  ۱) مالک دامنه‌اش را می‌فرستد (مثلاً pay.mysite.com) → یک رکوردِ TXT برایِ
     اثباتِ مالکیت تحویلش می‌دهیم: `_botsaz-verify.{domain}` = یک توکنِ
     تصادفی. همچنین باید یک رکوردِ A/CNAME هم به آیدرسِ سرور اضافه کند.
  ۲) با زدنِ «بررسیِ دوباره»، یک لوکاپِ DNS-over-HTTPS (بدونِ نیاز به کتابخانهٔ
     جداگانه — با همان aiohttpِ موجود در پروژه) روی رکوردِ TXT و CNAME/A
     انجام می‌شود. اگر هر دو درست بودند → status="verified".
  ۳) صدورِ خودِ گواهیِ SSL خودکار است: با «on-demand TLS» در Caddy — وقتی
     اولین درخواستِ HTTPS برایِ آن دامنه به Caddy می‌رسد، Caddy قبل از صدورِ
     گواهی از اندپوینتِ `/domain-ask` (در main_tenant.py) می‌پرسد «این دامنه
     مجاز است؟»؛ آن اندپوینت فقط برایِ دامنه‌هایی که در همین جدول
     status="verified" دارند 200 برمی‌گرداند. یعنی از سمتِ کدِ پایتون هیچ
     تماسی با Let's Encrypt گرفته نمی‌شود — Caddy خودش این کار را می‌کند.
     دیپلویِ Caddy، Caddyfile، و systemd unit در `deploy/` هستند — این فایل
     فقط اپلیکیشن‌سایدِ قضیه است.

نکتهٔ مهم: این dns lookup با DNS-over-HTTPSِ گوگل انجام می‌شود، نه یک
کتابخانهٔ DNS جدید — چون پروژه از قبل aiohttp دارد و نمی‌خواستیم یک
dependency تازه (dnspython) اضافه کنیم.

=== PATCH_CDOM_ORIGIN_TRACKING_V1 ===
این صفحه از دو جا صدا زده می‌شه: تنظیماتِ درگاهِ ارزدیجیتال
(crypto_admin.py) و صفحه‌ی «🔑 API ربات» (این فایل). چون دکمه‌ی «بازگشت»
باید به همون‌جایی برگرده که کاربر ازش اومده (نه یه مقصدِ ثابت)، یه
پارامترِ origin از تهِ callback_data رد می‌شه و تویِ کل مسیر (منو → تنظیم
دامنه → بررسی → حذف) دست‌نخورده باهاش حمل می‌شه.
"""
import re
import secrets

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session, WEBHOOK_BASE
from models import BotCustomDomain, Bot as BotRow

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

custom_domain_router = Router()

_DOMAIN_RE = re.compile(
    r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")

# === PATCH_CDOM_ORIGIN_TRACKING_V1 ===
_ORIGIN_TARGETS = {
    "crypto": "set:crypto",
    "apikey": "tadm:apikey",
}


def _cdom_back_target(origin: str) -> str:
    return _ORIGIN_TARGETS.get(origin, "set:crypto")


def _cdom_origin_from_cb(data: str) -> str:
    """آخرین بخشِ callback_data رو به‌عنوانِ origin برمی‌داره؛ اگه بخشِ
    آخر یکی از originهایِ شناخته‌شده نبود (یعنی این callback اصلاً origin
    نداشت)، پیش‌فرض "crypto" (رفتارِ قدیمی/بدونِ‌تغییر)."""
    tail = data.rsplit(":", 1)[-1]
    return tail if tail in _ORIGIN_TARGETS else "crypto"


class CustomDomainFSM(StatesGroup):
    waiting_domain = State()


async def _owner(cb_or_msg, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or cb_or_msg.from_user.id != row.owner_id:
        if hasattr(cb_or_msg, "answer") and hasattr(cb_or_msg, "message"):
            await cb_or_msg.answer("فقط مالکِ ربات به این بخش دسترسی دارد.", show_alert=True)
        return None
    return row


async def get_bot_webhook_base(bot_id: int) -> str:
    """آدرسِ پایه‌ای که باید برایِ ساختنِ لینک‌هایِ این ربات (پرداخت/وب‌اپ/
    فایل) استفاده شود — اگر دامنهٔ اختصاصیِ تاییدشده دارد همان، وگرنه
    دامنهٔ مشترکِ پلتفرم. همه‌جایی که قبلاً مستقیم WEBHOOK_BASE می‌خواند
    (crypto_admin.py، payment_page.py، tonpays.py، miniapp_admin.py،
    file_link.py، vpn_user.py) باید این تابع را صدا بزند، نه متغیرِ
    گلوبال را."""
    async with Session() as s:
        d = await s.scalar(select(BotCustomDomain).where(
            BotCustomDomain.bot_id == bot_id, BotCustomDomain.status == "verified"))
        if d:
            return f"https://{d.domain}"
        # === PATCH_CUSTOM_DOMAIN_V1 ===
        # همون Settingِ سطحِ پلتفرم (bot_id=0) که platform_bot.py با
        # get_pset/set_pset می‌خونه/می‌نویسه — این‌جا مستقیم از رویِ جدول
        # می‌خونیمش چون get_pset فقط سمتِ platform_*.py تعریف شده و سرویسِ
        # tenant بهش import ندارد (دو سرویسِ جدا، ولی یک دیتابیسِ مشترک).
        from models import Setting
        p = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == "webhook_base"))
    if p and p.value:
        return p.value
    return WEBHOOK_BASE or ""



# === PATCH_CDOM_DNS_TARGET_V1 ===
# درخواستِ Fadayi: مقدارهایِ دقیقِ DNS (آی‌پیِ سرور / هدفِ CNAME) باید خودِ ربات نشان بدهد، نه اینکه هر بار
# از پشتیبانی بپرسند. هدف از روی دامنهٔ مشترکِ پلتفرم (WEBHOOK_BASE) خوانده و آی‌پی‌اش resolve می‌شود؛
# اگر مالکِ پلتفرم بخواهد، با Setting(bot_id=0, key="custom_domain_ip") می‌تواند آی‌پی را دستی تعیین کند.
_DNS_TARGET_CACHE: dict = {"t": -1e9, "v": ("", [])}


async def get_dns_target() -> tuple[str, list[str]]:
    """(hostِ پلتفرم برایِ CNAME, [آی‌پی‌هایِ IPv4 برایِ رکوردِ A])"""
    import asyncio
    import ipaddress
    import socket
    import time
    from urllib.parse import urlparse
    if time.monotonic() - _DNS_TARGET_CACHE["t"] < 600:
        return _DNS_TARGET_CACHE["v"]
    host, ips = "", []
    try:
        base = await get_bot_webhook_base(0)
        h = urlparse(base if "://" in (base or "") else f"https://{base}").hostname or ""
        try:
            ipaddress.ip_address(h)
            ips = [h] if h else []          # خودش آی‌پی است ⇒ CNAME معنی ندارد
        except ValueError:
            host = h
        if host:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
            ips = sorted({i[4][0] for i in infos})
        from models import Setting
        async with Session() as s:
            ov = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == "custom_domain_ip"))
        if ov and (ov.value or "").strip():
            ips = [x.strip() for x in ov.value.replace("،", ",").split(",") if x.strip()]
    except Exception:
        logger.exception("خواندنِ هدفِ DNS (آی‌پیِ سرور) ناموفق بود")
    _DNS_TARGET_CACHE["t"] = time.monotonic()
    _DNS_TARGET_CACHE["v"] = (host, ips)
    return host, ips


def _dns_records_block(domain: str, host: str, ips: list[str]) -> str:
    """بلوکِ متنیِ رکوردهایِ لازم (با مقدارِ دقیق)."""
    short = domain.split(".")[0] if domain.count(".") >= 2 else "@"
    out = ["2️⃣ برایِ اینکه ترافیکِ واقعی به سرور برسد، <b>یکی از این دو</b> را در DNS دامنه بساز:\n"]
    if ips:
        out.append("الف) رکوردِ A (برایِ همهٔ دامنه‌ها، حتی دامنهٔ اصلی):\n"
                   f"   نوع: <code>A</code>\n"
                   f"   نام: <code>{domain}</code>  (در بعضی پنل‌ها فقط <code>{short}</code>)\n"
                   f"   مقدار: <code>{'</code> یا <code>'.join(ips)}</code>\n")
    if host and domain.count(".") >= 2:
        out.append(("ب" if ips else "الف") + ") رکوردِ CNAME (فقط برایِ ساب‌دامین مثل <code>api.site.com</code>):\n"
                   f"   نوع: <code>CNAME</code>\n"
                   f"   نام: <code>{domain}</code>  (یا فقط <code>{short}</code>)\n"
                   f"   مقدار: <code>{host}</code>\n")
    if not ips and not host:
        out.append("آدرسِ سرور الان خوانده نشد؛ چند دقیقهٔ دیگر «🔄 بررسیِ دوباره» را بزن یا از پشتیبانی بپرس.\n")
    out.append("⚠️ اگر دامنه روی Cloudflare است، حالتِ Proxy را <b>خاموش</b> (ابرِ خاکستری / DNS only) بگذار تا گواهیِ SSL صادر شود.")
    return "\n".join(out)


def _dns_short(host: str, ips: list[str]) -> str:
    """خلاصهٔ یک‌خطیِ مقدارهایِ DNS برایِ صفحهٔ «API ربات»."""
    parts = []
    if ips:
        parts.append("رکورد A → <code>" + "</code> یا <code>".join(ips) + "</code>")
    if host:
        parts.append(f"رکورد CNAME (ساب‌دامین) → <code>{host}</code>")
    if not parts:
        return ""
    return ("\n📌 <b>مقدارهایِ DNS برایِ وصل‌کردنِ دامنه:</b>\n" + "\n".join(parts)
            + "\n(جزئیاتِ کامل + رکوردِ تأیید: «🌐 افزودن/مدیریتِ دامنهٔ اختصاصی» در پایین)\n")


async def _domain_content(bot_id: int, origin: str = "crypto") -> tuple[str, InlineKeyboardMarkup]:
    back_cb = _cdom_back_target(origin)
    async with Session() as s:
        d = await s.scalar(select(BotCustomDomain).where(BotCustomDomain.bot_id == bot_id))
    if not d:
        text = (
            "🌐 <b>دامنهٔ اختصاصی</b>\n\n"
            "می‌تونی دامنهٔ خودتو وصل کنی تا لینک‌هایِ پرداخت/وب‌اپ/فایل/API "
            "رباتت همیشه زیرِ دامنهٔ برندِ خودت باشن، نه یه دامنهٔ عمومی.\n\n"
            "SSL خودکار است — فقط باید DNS دامنه را درست تنظیم کنی.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودنِ دامنهٔ اختصاصی", callback_data=f"cdom:set:{origin}",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)],
        ])
        return text, kb

    if d.status == "verified":
        text = (
            f"🌐 <b>دامنهٔ اختصاصی</b>\n\n"
            f"✅ <code>{d.domain}</code> وصل و فعال است.\n"
            f"همهٔ لینک‌هایِ پرداخت/وب‌اپ/فایلِ رباتت از این به بعد زیرِ همین "
            f"دامنه ساخته می‌شوند.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 حذفِ دامنه (برگشت به پیش‌فرض)", callback_data=f"cdom:remove:{origin}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)],
        ])
        return text, kb

    # pending / failed
    note_line = f"\n\n⚠️ آخرین بررسی: {d.last_check_note}" if d.last_check_note else ""
    text = (
        f"🌐 <b>دامنهٔ اختصاصی</b>\n\n"
        f"دامنه: <code>{d.domain}</code>\n"
        f"وضعیت: {'❌ تاییدنشده' if d.status == 'failed' else '⏳ در انتظارِ تاییدِ DNS'}\n\n"
        "برایِ تاییدِ مالکیت، این دو رکورد را در تنظیماتِ DNS دامنه‌ات اضافه کن:\n\n"
        f"1️⃣ برایِ اثباتِ مالکیت (رکوردِ TXT):\n"
        f"نام: <code>_botsaz-verify.{d.domain}</code>\n"
        f"مقدار: <code>botsaz-verify={d.verify_token}</code>\n\n"
        f"{_dns_records_block(d.domain, *(await get_dns_target()))}\n\n"
        "بعدِ اضافه‌کردنِ هر دو (انتشارِ DNS معمولاً چند دقیقه تا چند ساعت طول "
        f"می‌کشد)، «🔄 بررسیِ دوباره» را بزن.{note_line}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بررسیِ دوباره", callback_data=f"cdom:check:{origin}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✏️ تغییرِ دامنه", callback_data=f"cdom:set:{origin}")],
        [InlineKeyboardButton(text="🗑 لغو", callback_data=f"cdom:remove:{origin}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)],
    ])
    return text, kb


@custom_domain_router.callback_query(F.data.startswith("cdom:menu"))
async def cdom_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    origin = _cdom_origin_from_cb(cb.data)
    text, kb = await _domain_content(row.id, origin)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@custom_domain_router.callback_query(F.data.startswith("cdom:set"))
async def cdom_set_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    origin = _cdom_origin_from_cb(cb.data)
    from tenant import smart_cancel_kb
    await state.set_state(CustomDomainFSM.waiting_domain)
    await state.update_data(cdom_origin=origin)
    await cb.message.answer(
        "🌐 دامنه‌ات را بفرست (بدونِ <code>https://</code>، مثلاً "
        "<code>pay.mysite.com</code>):",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@custom_domain_router.message(CustomDomainFSM.waiting_domain, F.text)
async def cdom_set_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    data = await state.get_data()
    origin = data.get("cdom_origin", "crypto")
    raw = (msg.text or "").strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if not row or msg.from_user.id != row.owner_id:
        await state.clear()
        return
    domain = raw.lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if not _DOMAIN_RE.match(domain):
        from aiogram.dispatcher.event.bases import SkipHandler
        await state.clear()
        raise SkipHandler
    await state.clear()
    async with Session() as s:
        clash = await s.scalar(select(BotCustomDomain).where(
            BotCustomDomain.domain == domain, BotCustomDomain.bot_id != row.id))
        if clash:
            await msg.answer("❌ این دامنه قبلاً برایِ رباتِ دیگری ثبت شده.")
            return
        d = await s.scalar(select(BotCustomDomain).where(BotCustomDomain.bot_id == row.id))
        token = secrets.token_hex(12)
        if d:
            d.domain = domain
            d.verify_token = token
            d.status = "pending"
            d.verified_at = None
            d.last_check_note = None
        else:
            d = BotCustomDomain(bot_id=row.id, domain=domain, verify_token=token, status="pending")
            s.add(d)
        await s.commit()
    text, kb = await _domain_content(row.id, origin)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_PUBLIC_API_V1 ===
async def _apikey_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """صفحه‌ی مستقلِ API Key — کاملاً جدا از وضعیتِ دامنه‌ی اختصاصی.

    === PATCH_APIKEY_DOMAIN_STATUS_V1 ===
    درخواستِ ادمین: به‌جایِ نشون‌دادنِ دامنه‌ی مشترکِ پلتفرم به‌عنوانِ
    «آدرسِ ربات»، یه بلوکِ وضعیت (دامنه/SSL/وب‌سرویس) دقیقاً مثلِ نمونه‌ای
    که خودش داده نشون بدیم — و آدرسِ نهایی فقط وقتی نشون داده بشه که خودِ
    ادمین یه دامنه‌ی اختصاصی وصل کرده باشه."""
    async with Session() as s:
        b = await s.get(BotRow, bot_id)
        d = await s.scalar(select(BotCustomDomain).where(BotCustomDomain.bot_id == bot_id))

    # === PATCH_PUBLIC_API_SMM_V2_DISPLAY_V1 ===
    # گزارشِ ادمینِ رباتِ خدماتِ مجازی: این صفحه فقط آدرسِ v1 (قراردادِ
    # اختصاصیِ خودِ بات‌ساز) را نشان می‌داد، درحالی‌که tenant/public_api.py
    # از قبل یک اندپوینتِ استانداردِ SMM هم دارد (/api/v2، پارامترهایِ
    # key/action) — دقیقاً همان چیزی که ابزارها/پنل‌هایِ اکوسیستمِ SMM
    # انتظارش را دارند و مشتری‌هایِ رباتِ خدماتِ مجازی برایِ استفاده از این
    # ربات به‌عنوانِ یک «پنلِ» بالادستی بهش نیاز دارند. v1 هنوز کار می‌کند
    # (چیزی حذف نشده)؛ فقط v2 هم کنارش نشان داده می‌شود، و فقط برایِ
    # رباتِ خدماتِ مجازی، چون بقیه‌یِ زمینه‌ها اصلاً از این قراردادِ SMM
    # استفاده نمی‌کنند.
    is_smm = bool(b and b.field == "virtual_service")
    is_vpn = bool(b and b.field == "vpn_sales")   # === PATCH_VPN_API_V1 ===
    has_ctl = is_smm or is_vpn
    if d and d.status == "verified":
        domain_txt = d.domain
        status_txt = "✅ تأیید شده"
        ssl_txt = "🟢 فعال"
        base_line = f"\n🌐 آدرسِ API شما: <code>https://{d.domain}/api/v1</code>\n"
        if is_vpn:
            base_line = (f"\n🌐 آدرسِ API فروشِ VPN: <code>https://{d.domain}/api/vpn</code>\n"
                         f"📘 مستندات: <code>https://{d.domain}/api/docs/vpn</code>\n")
        if is_smm:
            base_line += (
                f"🤝 آدرسِ استانداردِ SMM (v2): <code>https://{d.domain}/api/v2</code>\n"
                "   (برایِ ابزارها/پنل‌هایی که فرمتِ استانداردِ SMM را می‌فهمند — "
                "پارامترهایِ <code>key</code> و <code>action</code>، نه هدرِ X-API-Key)\n"
            )
    elif d:
        domain_txt = d.domain
        status_txt = "⏳ در انتظارِ تأیید" if d.status == "pending" else "❌ ناموفق"
        ssl_txt = "🔴 غیرفعال (بعدِ تأیید، خودکار فعال می‌شه)"
        base_line = ""
    else:
        domain_txt = "— (هنوز تنظیم نشده)"
        status_txt = "⚠️ تنظیم نشده"
        ssl_txt = "🔴 غیرفعال"
        base_line = ""

    key_line = f"\n🔑 <code>{b.api_key}</code>\n" if (b and b.api_key) else "\n⚠️ هنوز کلیدی نساختی.\n"
    header_line = (
        "ابتدا دامنه‌ی اختصاصیِ خودتو (مثلاً api.shop.com) وصل کن تا آدرسِ API "
        "نهاییت اینجا نشون داده بشه.\n" if not d or d.status != "verified" else ""
    )
    if not d or d.status != "verified":
        header_line += _dns_short(*(await get_dns_target()))   # PATCH_CDOM_DNS_TARGET_V1
    text = (
        "🔑 <b>API ربات</b>\n\n"
        "با این کلید می‌تونی به محصولات/سفارش‌ها/کیف‌پولِ رباتت از بیرون "
        "(سایت، اپلیکیشن، اسکریپت) دسترسیِ برنامه‌نویسی داشته باشی.\n\n"
        f"🌐 دامنه: <code>{domain_txt}</code>\n"
        f"وضعیت: {status_txt}\n"
        f"گواهیِ SSL: {ssl_txt}\n"
        f"وب‌سرویس: 🟢 روشن\n"
        f"{base_line}"
        f"{key_line}\n"
        f"{header_line}"
        + (("برایِ v1: با هدرِ زیر تویِ درخواست‌هات بفرستش:\n"
            f"<code>X-API-Key: {b.api_key}</code>\n"
            + (f"برایِ v2 (استانداردِ SMM): همین کلید رو به‌عنوانِ پارامترِ "
               f"<code>key</code> بفرست (نه هدر)، مثلاً "
               f"<code>?key={b.api_key}&amp;action=balance</code>\n" if is_smm else "")
            + (("برایِ فروشِ VPN (آدرسِ /api/vpn): پارامترهایِ <code>key</code> و <code>action</code> "
                "(plans، balance، buy، services، status) — با کلیدِ ربات باید <code>telegram_id</code> هم بدهی، "
                "مثلاً <code>?key=" + b.api_key + "&amp;action=plans</code>\n") if is_vpn else "")
            + "⚠️ این کلید رو با کسی به اشتراک نذار.")
           if (b and b.api_key) else ""))
    kb_rows = []
    if b and b.api_key:
        kb_rows.append([InlineKeyboardButton(text="🔄 ساختِ کلیدِ جدید (کلیدِ قبلی باطل می‌شه)",
                                             callback_data="tadm:apikey:regen", style=ButtonStyle.DANGER)])
        # === PATCH_APIKEY_DELETE_V1 ===
        kb_rows.append([InlineKeyboardButton(text="🗑 حذفِ کلید (بدونِ ساختِ کلیدِ جدید)",
                                             callback_data="tadm:apikey:delete", style=ButtonStyle.DANGER)])
    else:
        kb_rows.append([InlineKeyboardButton(text="🔑 ساختِ API Key", callback_data="tadm:apikey:regen",
                                             style=ButtonStyle.SUCCESS)])
    # === PATCH_CDOM_ORIGIN_TRACKING_V1 ===
    # origin="apikey" یعنی دکمه‌ی بازگشتِ اون صفحه باید برگرده اینجا،
    # نه بره تنظیماتِ درگاهِ ارزدیجیتال (که مقصدِ پیش‌فرض/قدیمیه).
    kb_rows.append([InlineKeyboardButton(
        text=("🌐 مدیریتِ دامنهٔ اختصاصی" if d else "🌐 افزودنِ دامنهٔ اختصاصی"), callback_data="cdom:menu:apikey")])
    # === PATCH_API_OPEN_TO_ALL_V1 ===
    # سوئیچِ «API برایِ همه یا فقط بعضی‌ها» — رباتِ خدماتِ مجازی و فروشِ VPN.
    if has_ctl:
        from tenant import get_setting as _gs
        _all = (await _gs(bot_id, "api_open_to_all", "")) == "1"
        kb_rows.append([InlineKeyboardButton(
            text=("👥 API برایِ همهٔ کاربران: ✅ فعال (زدن = فقط کاربرانِ انتخاب‌شده)" if _all
                  else "👥 API برایِ همهٔ کاربران: ⬜️ غیرفعال (زدن = باز برایِ همه)"),
            callback_data="tadm:apikey:allusers",
            style=(ButtonStyle.SUCCESS if _all else ButtonStyle.PRIMARY))])
    # === PATCH_API_PAID_ACCESS_V1 ===
    # قیمت و مدتِ دسترسیِ API برایِ کاربران (خرید از موجودیِ کیف پول).
    if has_ctl:
        from tenant.shared.public_api import api_price as _apr, api_period_days as _apd
        _price = await _apr(bot_id)
        _days = await _apd(bot_id)
        kb_rows.append([InlineKeyboardButton(
            text=(f"💰 قیمتِ دسترسیِ API: {_price:,} تومان" if _price else "💰 قیمتِ دسترسیِ API: رایگان (تنظیم)"),
            callback_data="tadm:apikey:price")])
        kb_rows.append([InlineKeyboardButton(
            text=("⏳ مدت: ♾ دائمی (زدن = ماهانه)" if not _days else "⏳ مدت: 📅 ماهانه ۳۰ روزه (زدن = دائمی)"),
            callback_data="tadm:apikey:period")])
    # === PATCH_PUBLIC_API_PER_USER_SEARCH_V1 ===
    # درخواستِ ادمین: بشه مستقیم از همین صفحه یه کاربرِ خاص رو پیدا کرد و
    # دسترسیِ API/درصدِ تخفیفش رو تنظیم کرد — بدونِ رفتن به یه بخشِ جدا.
    kb_rows.append([InlineKeyboardButton(
        text="🔍 جست‌وجویِ کاربر (تنظیمِ دسترسی/تخفیفِ API)", callback_data="apiuser:search")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


class ApiUserSearchFSM(StatesGroup):
    waiting_query = State()


# === PATCH_API_PAID_ACCESS_V1 ===
class ApiPriceFSM(StatesGroup):
    waiting_price = State()


@custom_domain_router.callback_query(F.data == "tadm:apikey:price")
async def tadm_apikey_price_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    if row.field not in ("virtual_service", "vpn_sales"):   # PATCH_VPN_API_V1
        await cb.answer("این قابلیت فقط برایِ رباتِ خدماتِ مجازی و فروشِ VPN است.", show_alert=True)
        return
    from tenant import smart_cancel_kb
    await state.set_state(ApiPriceFSM.waiting_price)
    await cb.message.answer(
        "💰 قیمتِ دسترسیِ API رو به <b>تومان</b> بفرست (فقط عدد).\n"
        "مثلاً <code>50000</code>\n\n"
        "• <code>0</code> = رایگان (دسترسی مثلِ قبل: دستیِ ادمین یا «باز برایِ همه»)\n"
        "• عددِ بیشتر از صفر = کاربر با موجودیِ کیف پولش می‌خره؛ مدتش از دکمهٔ «⏳ مدت» تعیین می‌شه.",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@custom_domain_router.message(ApiPriceFSM.waiting_price, F.text)
async def tadm_apikey_price_set(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    raw = raw.replace(",", "").replace("٬", "").replace(" ", "")
    if not raw.isdigit() or int(raw) > 100_000_000:
        await msg.answer("❌ فقط یک عددِ درست (تومان) بفرست؛ مثلاً 50000. یا «❌ لغو».")
        return
    await state.clear()
    from tenant import set_setting as _ss
    await _ss(row.id, "api_price", str(int(raw)))
    await msg.answer(f"✅ قیمتِ دسترسیِ API روی {int(raw):,} تومان تنظیم شد." if int(raw)
                     else "✅ دسترسیِ API رایگان شد.")
    text, kb = await _apikey_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@custom_domain_router.callback_query(F.data == "tadm:apikey:period")
async def tadm_apikey_period_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    if row.field not in ("virtual_service", "vpn_sales"):   # PATCH_VPN_API_V1
        await cb.answer("این قابلیت فقط برایِ رباتِ خدماتِ مجازی و فروشِ VPN است.", show_alert=True)
        return
    from tenant import set_setting as _ss
    from tenant.shared.public_api import api_period_days as _apd
    cur = await _apd(row.id)
    new_days = 30 if cur == 0 else 0
    await _ss(row.id, "api_period_days", str(new_days))
    await cb.answer("📅 از این به بعد خریدها ماهانه (۳۰ روزه) هستند." if new_days
                    else "♾ از این به بعد خریدها دائمی هستند.", show_alert=True)
    await tadm_apikey_show(cb, bot)


@custom_domain_router.callback_query(F.data == "apiuser:search")
async def apiuser_search_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(ApiUserSearchFSM.waiting_query)
    await cb.message.answer(
        "👤 آیدیِ عددی یا یوزرنیمِ کاربری که می‌خوای دسترسیِ API/درصدِ تخفیفش رو "
        "تنظیم کنی رو بفرست:\n(مثلاً <code>123456789</code> یا <code>@username</code>)",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@custom_domain_router.message(ApiUserSearchFSM.waiting_query, F.text)
async def apiuser_search_resolve(msg: Message, bot: Bot, state: FSMContext):
    """=== PATCH_PUBLIC_API_PER_USER_SEARCH_V1 ===
    عمداً یه کپیِ کوچیک از منطقِ جست‌وجویِ tadm:users (که تویِ
    tenant/__init__.py هست) نساختیم — همون تابعِ _render_user_detail رو
    مستقیم صدا می‌زنیم، چون از قبل (بعدِ این پچ) دکمه‌هایِ دسترسیِ API/
    تخفیف رو تویِ کارتِ کاربر داره؛ یعنی جست‌وجو از اینجا دقیقاً همون
    کارتِ آشنایِ «مدیریتِ کاربران» رو نشون می‌ده، فقط مسیرِ ورودش کوتاه‌تره."""
    await state.clear()
    row = await _owner(msg, bot)
    if not row:
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await msg.answer("لغو شد.")
        return
    from tenant import fa_to_int, get_bot_row, _render_user_detail
    from models import BotUser as _BotUser
    from sqlalchemy import func as _func
    uid = fa_to_int(raw)
    if not uid:
        uname = raw.lstrip("@").strip()
        if not uname:
            await msg.answer("شناسه نامعتبر. یک آیدی عددی یا یوزرنیم بفرست.")
            return
        async with Session() as s:
            u = await s.scalar(select(_BotUser).where(
                _BotUser.bot_id == row.id, _func.lower(_BotUser.username) == uname.lower()))
        if not u:
            await msg.answer(f"کاربری با یوزرنیمِ @{uname} تویِ این ربات پیدا نشد.")
            return
        uid = u.telegram_id
    await _render_user_detail(msg, bot, uid)


# === PATCH_APIUSER_ISSUE_KEY_V1 ===
# صدورِ کلیدِ APIِ یک مشتری توسطِ خودِ ادمین (فقط ربات‌هایِ «خدمات مجازی»).
# قبلاً ادمین فقط می‌توانست دسترسی را روشن کند و کاربر باید خودش از
# «پروفایلِ من» کلید را می‌گرفت؛ حالا ادمین کلید را می‌سازد، خودش می‌بیند و
# همان‌جا برای کاربر ارسال می‌شود.
# پیشوندِ callback عمداً `apiuser:` است نه `usr:` — چون هندلرِ عمومیِ
# `usr:` در tenant/__init__.py زودتر ثبت شده و این را می‌بلعید.
async def _issue_user_api_key(cb: CallbackQuery, bot: Bot, uid: int) -> None:
    row = await _owner(cb, bot)
    if not row:
        return
    if row.field not in ("virtual_service", "vpn_sales"):   # PATCH_VPN_API_V1
        await cb.answer("این قابلیت فقط برایِ رباتِ خدماتِ مجازی و فروشِ VPN است.", show_alert=True)
        return
    import secrets as _secrets
    from models import BotUser as _BotUser
    new_key = _secrets.token_hex(24)
    async with Session() as s:
        u = await s.scalar(select(_BotUser).where(
            _BotUser.bot_id == row.id, _BotUser.telegram_id == uid).with_for_update())
        if not u:
            await cb.answer("کاربر یافت نشد", show_alert=True)
            return
        u.api_access = True
        u.api_expires_at = None   # === PATCH_API_PAID_ACCESS_V1 ===
        u.api_key = new_key
        disc = float(u.api_discount_pct or 0)
        await s.commit()

    base = await get_bot_webhook_base(row.id)
    disc_line = f"\n🎁 تخفیفِ اختصاصیِ شما رویِ خریدهایِ API: {disc:g}٪" if disc else ""
    # === PATCH_PUBLIC_API_SMM_V2_DISPLAY_V1 ===
    # این تابع فقط برایِ رباتِ خدماتِ مجازی صدا زده می‌شود (چک بالای همین
    # تابع: row.field != "virtual_service" → return)، پس v2 همیشه مرتبط
    # است — نیازی به شرطِ اضافه نیست.
    if row.field == "vpn_sales":   # === PATCH_VPN_API_V1 ===
        v2_line = (
            f"🛡 آدرسِ API فروشِ VPN: <code>{base}/api/vpn</code>\n"
            f"   پارامترها: <code>key={new_key}</code> و <code>action</code> (plans / balance / buy / services / status)\n"
            f"📘 مستندات: {base}/api/docs/vpn\n\n"
        )
    else:
        v2_line = (
            f"🤝 آدرسِ استانداردِ SMM (v2): <code>{base}/api/v2</code>\n"
            f"   با پارامترِ <code>key={new_key}</code> (نه هدر) — برایِ ابزارها/پنل‌هایی "
            "که فرمتِ استانداردِ SMM را می‌فهمند\n\n"
        )
    sent_ok = True
    try:
        await bot.send_message(
            uid,
            "🔑 <b>API Key شخصیِ شما</b>\n\n"
            f"<code>{new_key}</code>\n\n"
            f"آدرسِ پایه (v1): <code>{base}/api/v1</code>{disc_line}\n\n"
            f"{v2_line}"
            "با این کلید می‌تونی محصولاتِ ربات رو بگیری و سفارش ثبت کنی. "
            "برایِ v1 تویِ هدرِ درخواست‌هات بفرستش:\n"
            f"<code>X-API-Key: {new_key}</code>\n\n"
            "⚠️ این کلید رو با کسی به اشتراک نذار.",
            parse_mode="HTML")
    except Exception:
        sent_ok = False
        logger.exception("ارسالِ کلیدِ API به کاربر %s ناموفق بود", uid)

    note = "✅ کلید ساخته و برایِ کاربر ارسال شد." if sent_ok else (
        "⚠️ کلید ساخته شد ولی ارسالِ پیام به کاربر ناموفق بود (احتمالاً ربات را بلاک کرده). "
        "کلید را دستی برایش بفرست.")
    await cb.message.answer(
        f"📤 <b>کلیدِ API کاربر <code>{uid}</code></b>\n\n"
        f"<code>{new_key}</code>\n\n{note}",
        parse_mode="HTML")
    await cb.answer("✅ انجام شد" if sent_ok else "⚠️ ارسال ناموفق", show_alert=not sent_ok)
    from tenant import _render_user_detail
    await _render_user_detail(cb.message, bot, uid)


@custom_domain_router.callback_query(F.data.startswith("apiuser:issue:"))
async def apiuser_issue_start(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    try:
        uid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    from models import BotUser as _BotUser
    async with Session() as s:
        u = await s.scalar(select(_BotUser).where(
            _BotUser.bot_id == row.id, _BotUser.telegram_id == uid))
        has_key = bool(u and u.api_key)
    if not has_key:
        await _issue_user_api_key(cb, bot, uid)
        return
    await cb.message.answer(
        f"⚠️ این کاربر از قبل کلید دارد. با ساختِ کلیدِ جدید، کلیدِ قبلی‌اش <b>باطل</b> می‌شود "
        "و هر جایی که ازش استفاده کرده از کار می‌افتد.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، کلیدِ جدید بساز و بفرست",
                                  callback_data=f"apiuser:issuec:{uid}", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data=f"apiuser:cancel:{uid}",
                                  style=ButtonStyle.DANGER)],
        ]))
    await cb.answer()


@custom_domain_router.callback_query(F.data.startswith("apiuser:issuec:"))
async def apiuser_issue_confirm(cb: CallbackQuery, bot: Bot):
    try:
        uid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    await _issue_user_api_key(cb, bot, uid)


@custom_domain_router.callback_query(F.data.startswith("apiuser:cancel:"))
async def apiuser_issue_cancel(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    try:
        uid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("حذفِ پیامِ تأییدِ صدورِ کلید ناموفق بود")
    await cb.answer("لغو شد")
    from tenant import _render_user_detail
    await _render_user_detail(cb.message, bot, uid)


# === PATCH_APIKEY_KEYBOARD_MODE_FALLBACK_V1 ===
# در حالتِ منویِ کیبوردی، دکمهٔ «🔑 API ربات» یک پیامِ متنی می‌فرستد (نه
# callback). دیسپچرِ متنِ تنظیمات در tenant/__init__.py این برچسب را دارد،
# ولی آن هندلر با StateFilter(None) ثبت شده — یعنی اگر مالکِ ربات یک FSM
# استیتِ باز داشته باشد (مثلاً قبلاً وارد یکی از صفحه‌هایی شده که منتظرِ
# متن است و بعد بی‌خیالش شده)، هیچ هندلری با پیام مچ نمی‌شود و زدنِ دکمه
# «هیچ واکنشی» ندارد — دقیقاً گزارشِ ادمین.
#
# این هندلر همان صفحه را بدونِ وابستگی به استیت باز می‌کند و استیتِ
# جامانده را هم پاک می‌کند. چون روی یک روترِ فرزند است، فقط وقتی اجرا
# می‌شود که هندلرِ اصلیِ tenant_router مچ نکرده باشد؛ پس مسیرِ عادی
# دست‌نخورده می‌ماند.
_APIKEY_BTN_LABEL = "🔑 API ربات"


@custom_domain_router.message(F.text == _APIKEY_BTN_LABEL)
async def tadm_apikey_keyboard_entry(msg: Message, bot: Bot, state: FSMContext):
    from aiogram.dispatcher.event.bases import SkipHandler
    row = await _owner(msg, bot)
    if not row:
        await msg.answer("🔒 مدیریت API ربات فقط برای مالک ربات در دسترس است.")
        return
    try:
        await state.clear()
    except Exception:
        logger.exception("پاک‌کردنِ استیتِ جامانده پیشِ نمایشِ صفحهٔ API ناموفق بود")
    text, kb = await _apikey_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@custom_domain_router.callback_query(F.data == "tadm:apikey")
async def tadm_apikey_show(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await _apikey_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@custom_domain_router.callback_query(F.data == "tadm:apikey:regen")
async def tadm_apikey_regen(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    import secrets as _secrets
    async with Session() as s:
        b = await s.get(BotRow, row.id, with_for_update=True)
        if not b:
            await cb.answer()
            return
        b.api_key = _secrets.token_hex(24)
        await s.commit()
    await cb.answer("✅ کلید ساخته شد")
    await tadm_apikey_show(cb, bot)


# === PATCH_API_OPEN_TO_ALL_V1 ===
@custom_domain_router.callback_query(F.data == "tadm:apikey:allusers")
async def tadm_apikey_allusers(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    if row.field not in ("virtual_service", "vpn_sales"):   # PATCH_VPN_API_V1
        await cb.answer("این قابلیت فقط برایِ رباتِ خدماتِ مجازی و فروشِ VPN است.", show_alert=True)
        return
    from tenant import get_setting as _gs, set_setting as _ss
    new_on = (await _gs(row.id, "api_open_to_all", "")) != "1"
    await _ss(row.id, "api_open_to_all", "1" if new_on else "0")
    await cb.answer(
        "✅ API برایِ همهٔ کاربران باز شد؛ هر کاربر از «پروفایلِ من» کلیدش رو می‌گیره."
        if new_on else "⬜️ API فقط برایِ کاربرانِ انتخاب‌شده فعاله.",
        show_alert=True)
    await tadm_apikey_show(cb, bot)


# === PATCH_APIKEY_DELETE_V1 ===
@custom_domain_router.callback_query(F.data == "tadm:apikey:delete")
async def tadm_apikey_delete(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        b = await s.get(BotRow, row.id, with_for_update=True)
        if not b:
            await cb.answer()
            return
        b.api_key = None
        await s.commit()
    await cb.answer("✅ کلید حذف شد")
    await tadm_apikey_show(cb, bot)


@custom_domain_router.callback_query(F.data.startswith("cdom:remove"))
async def cdom_remove(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    origin = _cdom_origin_from_cb(cb.data)
    async with Session() as s:
        d = await s.scalar(select(BotCustomDomain).where(BotCustomDomain.bot_id == row.id))
        if d:
            await s.delete(d)
            await s.commit()
    await cb.answer("✅ حذف شد")
    text, kb = await _domain_content(row.id, origin)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _doh_txt_lookup(name: str) -> list[str]:
    """رکوردهای TXT را با DNS-over-HTTPSِ گوگل می‌خواند — بدونِ نیاز به
    کتابخانهٔ DNS جدا. اگر چیزی پیدا نشد یا خطا داد، لیستِ خالی برمی‌گرداند."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get("https://dns.google/resolve",
                             params={"name": name, "type": "TXT"}) as r:
                if r.status != 200:
                    return []
                data = await r.json()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (لوکاپِ DNS-over-HTTPS برایِ TXT)")
        return []
    out = []
    for ans in data.get("Answer", []) or []:
        val = (ans.get("data") or "").strip('"')
        out.append(val)
    return out


async def _doh_resolves(name: str) -> bool:
    """آیا دامنه اصلاً به یک آدرسی resolve می‌شود (A/CNAME)؟ فقط اثباتِ
    اینکه DNSِ دامنه به‌سمتِ جایی اشاره می‌کند — بررسیِ دقیقِ اینکه دقیقاً
    آی‌پیِ سرورِ ماست یا نه، در سمتِ Caddy (وقتی واقعاً درخواستِ HTTPS بیاید)
    انجام می‌شود؛ اینجا فقط یک پیش‌شرطِ اولیه است تا کاربر متوجهِ فراموش‌کردنِ
    رکوردِ A/CNAME بشود."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get("https://dns.google/resolve", params={"name": name, "type": "A"}) as r:
                data = await r.json() if r.status == 200 else {}
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (لوکاپِ DNS-over-HTTPS برایِ A/CNAME)")
        return False
    return bool(data.get("Answer"))


@custom_domain_router.callback_query(F.data.startswith("cdom:check"))
async def cdom_check(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    origin = _cdom_origin_from_cb(cb.data)
    async with Session() as s:
        d = await s.scalar(select(BotCustomDomain).where(BotCustomDomain.bot_id == row.id))
    if not d:
        await cb.answer("دامنه‌ای ثبت نشده.", show_alert=True)
        return
    await cb.answer("در حالِ بررسی…")
    from datetime import datetime
    txt_records = await _doh_txt_lookup(f"_botsaz-verify.{d.domain}")
    expected = f"botsaz-verify={d.verify_token}"
    txt_ok = expected in txt_records
    a_ok = await _doh_resolves(d.domain)
    async with Session() as s:
        d2 = await s.scalar(select(BotCustomDomain).where(BotCustomDomain.bot_id == row.id))
        if not d2:
            return
        d2.last_check_at = datetime.utcnow()
        if txt_ok and a_ok:
            d2.status = "verified"
            d2.verified_at = datetime.utcnow()
            d2.last_check_note = None
        else:
            d2.status = "failed"
            missing = []
            if not txt_ok:
                missing.append("رکوردِ TXT هنوز دیده نشد یا مقدارش درست نیست")
            if not a_ok:
                missing.append("دامنه هنوز به هیچ آدرسی resolve نمی‌شود (رکوردِ A/CNAME)")
            d2.last_check_note = " و ".join(missing)
        await s.commit()
        bot_id = d2.bot_id
    text, kb = await _domain_content(bot_id, origin)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
