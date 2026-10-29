"""
trust_report.py — «کانال اعتمادسازی» برای ربات‌های مستأجر.

هدف: ادمینِ یک ربات مستأجر می‌تونه یه کانال (که خودِ ربات توش ادمینه) وصل
کنه؛ از اون به بعد، هر سفارشِ موفق (هر پرداختی — کیف‌پول، کارت‌به‌کارت،
فروشگاهِ دیجیتال، و...) یه پیامِ عمومیِ خوش‌فرمت به اون کانال پست می‌شه —
برایِ اعتمادسازیِ مشتری‌هایِ جدید (نشون‌دادنِ اینکه سفارش‌هایِ واقعی و
موفق زیاد اتفاق می‌افته).

این ماژول عمداً UI (اتصال/تنظیمات) رو از منطقِ «کِی باید پیام رفت» جدا
نگه می‌داره؛ صدازدنِ واقعیِ send_trust_channel_post از داخلِ
tenant/__init__.py:_purchase_alert انجام می‌شه (همون نقطه‌ی مشترکی که همه‌ی
مسیرهای خرید — کیف‌پول/کارت/فروشگاه — از قبل برایِ اطلاعِ ادمین صداش
می‌زنن)، پس هر مسیرِ خریدی که اون تابع رو صدا بزنه، خودکار اینجا هم
پوشش داده می‌شه.
"""
from __future__ import annotations

import datetime

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

trust_report_router = Router()


class TrustReportFSM(StatesGroup):
    setchat = State()
    edittext = State()  # === PATCH_TRUST_CHANNEL_CUSTOM_TEXT_V1 ===
    editbtns = State()  # === PATCH_TRUST_CHANNEL_CUSTOM_BUTTONS_V1 ===
    editadmin = State()  # === PATCH_TRUST_ADMIN_USERNAME_EDIT_V1 ===


def _tehran_now_parts(dt: "datetime.datetime | None" = None) -> tuple[str, str, str]:
    """(تاریخِ شمسی, ساعت, ثانیه) به‌وقتِ تهران — dt باید UTC باشه (یا خالی=الان)."""
    dt = dt or datetime.datetime.utcnow()
    local = dt + datetime.timedelta(hours=3, minutes=30)
    try:
        import jdatetime
        jd = jdatetime.datetime.fromgregorian(datetime=local)
        return jd.strftime("%Y/%m/%d"), local.strftime("%H:%M"), local.strftime("%S")
    except Exception:
        return local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), local.strftime("%S")


def _mask_buyer_id(tg_id: int) -> str:
    """پنهان‌کردنِ بخشِ میانیِ آیدیِ عددیِ خریدار — طبقِ درخواستِ صریحِ یه
    کاربر (تیکت #۱۵۰): هم حریمِ خصوصیِ خریدار بهتر حفظ می‌شه، هم رقبا
    نمی‌تونن مشتری‌ها رو از رویِ آیدیِ کاملشون شناسایی/دنبال کنن."""
    s = str(tg_id)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


# === PATCH_TRUST_CHANNEL_CUSTOM_BUTTONS_V1 ===
# پیامِ کانالِ اعتمادسازی یک دکمهٔ ثابتِ «برای خرید اقدام کنید» داشت.
# حالا مالکِ ربات می‌تواند دکمه‌هایِ شیشه‌ایِ دلخواهِ خودش را زیرِ آن
# بگذارد (کانال، پشتیبانی، آموزش، ...). چون این پیام در «کانال» است و
# بیننده ممکن است اصلاً هنوز رباتی را استارت نکرده باشد، فقط دکمهٔ
# نوعِ URL پذیرفته می‌شود — نه callback_data.
_BTN_ROW_SEP = "||"


def _norm_url(raw: str) -> str | None:
    """لینک را نرمال می‌کند؛ اگر معتبر نبود None برمی‌گرداند."""
    u = (raw or "").strip()
    if not u:
        return None
    if u.startswith("@") and len(u) > 1:
        return f"https://t.me/{u[1:]}"
    if u.startswith(("http://", "https://", "tg://")):
        return u
    if u.startswith(("t.me/", "telegram.me/", "www.")):
        return "https://" + u
    return None


_COLOR_STYLES = {"blue": ButtonStyle.PRIMARY, "green": ButtonStyle.SUCCESS, "red": ButtonStyle.DANGER}
_COLOR_LABELS = {"blue": "🔵 آبی", "green": "🟢 سبز", "red": "🔴 قرمز"}
_COLOR_ORDER = ["blue", "green", "red"]


def _parse_trust_buttons(raw: str, style: "ButtonStyle" = ButtonStyle.PRIMARY) -> tuple[list[list[InlineKeyboardButton]], list[str]]:
    """متنِ خامِ تنظیماتِ دکمه‌ها را به ردیف‌هایِ دکمه تبدیل می‌کند.

    هر خط = یک ردیف. هر دکمه: «متن - لینک» (جداکنندهٔ آخرین ' - ').
    برایِ دو دکمه در یک ردیف، بینشان '||' بگذار.
    خروجی: (ردیف‌ها, فهرستِ خطاها)
    """
    rows: list[list[InlineKeyboardButton]] = []
    errors: list[str] = []
    for i, line in enumerate((raw or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row: list[InlineKeyboardButton] = []
        for chunk in line.split(_BTN_ROW_SEP):
            chunk = chunk.strip()
            if not chunk:
                continue
            if " - " not in chunk:
                errors.append(f"خطِ {i}: جداکنندهٔ « - » پیدا نشد.")
                continue
            label, _, link = chunk.rpartition(" - ")
            label = label.strip()
            url = _norm_url(link)
            if not label:
                errors.append(f"خطِ {i}: متنِ دکمه خالی است.")
                continue
            if not url:
                errors.append(f"خطِ {i}: لینکِ «{label}» معتبر نیست.")
                continue
            # === PATCH_TRUST_CHANNEL_BTN_COLOR_V1 ===
            row.append(InlineKeyboardButton(text=label[:64], url=url, style=style))
        if row:
            rows.append(row[:3])
        if len(rows) >= 8:
            break
    return rows, errors


async def build_trust_keyboard(bot_id: int, bot_username: str | None) -> InlineKeyboardMarkup | None:
    """کیبوردِ زیرِ پیامِ کانال: دکمه‌هایِ دلخواهِ مالک، وگرنه دکمهٔ پیش‌فرض."""
    try:
        from tenant import get_setting
        raw = await get_setting(bot_id, "trust_channel_buttons", "")
        # === PATCH_TRUST_CHANNEL_BTN_COLOR_V1 ===
        color = await get_setting(bot_id, "trust_channel_btn_color", "blue")
    except Exception:
        raw = ""
        color = "blue"
    style = _COLOR_STYLES.get(color, ButtonStyle.PRIMARY)
    if raw:
        rows, _ = _parse_trust_buttons(
            raw.replace("{bot}", f"https://t.me/{bot_username}" if bot_username else ""),
            style=style)
        if rows:
            return InlineKeyboardMarkup(inline_keyboard=rows)
    if bot_username:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="برای خرید اقدام کنید",
                                 url=f"https://t.me/{bot_username}", style=style),
        ]])
    return None


# === PATCH_TRUST_CHANNEL_BIDI_V1 ===
# گزارشِ ادمین: «به‌هم‌ریخته». علت، مشکلِ دوجهته‌بودنِ متن (bidi) است، نه
# اشتباهِ قالب: وقتی مقداری مثلِ «Shahin» یا «20 گیگ/365 روز» داخلِ خطِ
# فارسی می‌نشیند، الگوریتمِ bidi انتهایِ خط را برعکس می‌کند و چیزهایی مثلِ
# «× 1 عدد» می‌پرند سرِ خطِ بعد یا آیدی وارونه دیده می‌شود.
#
# راهِ استانداردش «جداسازیِ جهت» است: هر مقدار را داخلِ FSI…PDI بگذاریم تا
# جهتِ خودش را داشته باشد و رویِ بقیهٔ خط اثر نگذارد. این کاراکترها نامرئی‌اند
# و در تلگرام (هم موبایل هم دسکتاپ) پشتیبانی می‌شوند.
_FSI, _PDI = "\u2068", "\u2069"


def _iso(value) -> str:
    """مقدار را از نظرِ جهتِ متن ایزوله می‌کند تا خطِ فارسی را به‌هم نریزد."""
    text = "" if value is None else str(value)
    return f"{_FSI}{text}{_PDI}" if text else text


async def send_trust_channel_post(
    bot: Bot, bot_id: int, *,
    buyer_telegram_id: int | None,
    product_name: str,
    amount,
    order_id: int | str | None = None,
    product_id: int | str | None = None,
    count: int = 1,
    tracking_code: str | None = None,
    user_order_no: int | None = None,
    bot_username: str | None = None,
    panel_name: str | None = None,
    button_name: str | None = None,  # === PATCH_ORDER_BUTTON_ID_V1 ===
) -> None:
    """اگه این ربات یه کانالِ اعتمادسازیِ متصل داشته باشه، گزارشِ این سفارشِ
    موفق رو اونجا پست می‌کنه. اگه چیزی وصل نباشه یا هرجور خطایی پیش بیاد،
    کاملاً بی‌صدا رد می‌شه — این یه قابلیتِ جانبیه، نباید هیچ‌وقت خودِ
    فرآیندِ خرید رو مختل کنه.

    === PATCH_TRUST_CHANNEL_PRIVACY_V1 (طبقِ پیشنهادِ کاربر در تیکت #۱۵۰) ===
    ۱) آیدیِ خریدار دیگه کاملاً خام نشون داده نمی‌شه، ماسک می‌شه.
    ۲) فیلدهایی که واقعاً مقدار ندارن (مثلاً برایِ خیلی از مسیرهایِ فروشِ
       VPN که رکوردِ رسمیِ Order/شناسه‌ی محصول ندارن) به‌جایِ نمایشِ «—»ِ
       غیرحرفه‌ای، از پیام کلاً حذف می‌شن."""
    try:
        from tenant import get_setting
        channel_id = await get_setting(bot_id, "trust_channel_id", "")
        if not channel_id:
            return
        show_admin = await get_setting(bot_id, "trust_show_admin", "0") == "1"
        admin_username = await get_setting(bot_id, "trust_admin_username", "")

        jdate, jtime, jsec = _tehran_now_parts()
        bot_line = f"@{bot_username}" if bot_username else "—"

        # === PATCH_TRUST_SHOW_BUYER_V2 ===
        # پیش‌فرض خاموشه = همون رفتارِ قبلی (شمارهٔ سفارش یا آیدیِ ماسک‌شده).
        # با روشن‌کردنش، یوزرنیم و آیدیِ عددیِ کاملِ خریدار نمایش داده می‌شه.
        show_buyer = await get_setting(bot_id, "trust_show_buyer", "0") == "1"
        if show_buyer and buyer_telegram_id:
            uname = None
            try:
                from sqlalchemy import select as _sel
                from config import Session as _S
                from models import BotUser as _BU
                async with _S() as _s:
                    uname = await _s.scalar(_sel(_BU.username).where(
                        _BU.bot_id == bot_id, _BU.telegram_id == buyer_telegram_id))
            except Exception:
                logger.exception("خواندنِ یوزرنیمِ خریدار برایِ کانالِ اعتمادسازی ناموفق بود")
            from tenant.shared.buyer_ident import get_mode, ident_text      # پیشنهادِ #187
            user_order_txt = ident_text(await get_mode(bot_id), uname, buyer_telegram_id)
        elif user_order_no is not None:
            user_order_txt = f"#{user_order_no}"
        elif buyer_telegram_id:
            user_order_txt = _mask_buyer_id(buyer_telegram_id)
        else:
            user_order_txt = "—"

# === PATCH_TRUST_CHANNEL_CUSTOM_TEXT_V1 ===
        custom_template = await get_setting(bot_id, "trust_channel_template", "")
        if custom_template:
            # === PATCH_TRUST_CHANNEL_BIDI_V1 === هر مقدار ایزوله می‌شود
            text = (custom_template
                    .replace("{buyer}", _iso(user_order_txt))
                    .replace("{product}", _iso(product_name))
                    .replace("{count}", _iso(count))
                    .replace("{amount}", _iso(f"{int(float(amount)):,}"))
                    .replace("{time}", _iso(f"{jtime}:{jsec}"))
                    .replace("{date}", _iso(jdate))
                    .replace("{bot}", _iso(bot_line))
                    # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
                    .replace("{panel}", _iso(panel_name) if panel_name else "—")
                    # === PATCH_ORDER_BUTTON_ID_V1 ===
                    # {bottom} هم عمداً alias شده (تایپِ رایجِ ادمین‌ها به‌جایِ {button})
                    .replace("{button}", _iso(button_name) if button_name else "—")
                    .replace("{bottom}", _iso(button_name) if button_name else "—"))
            if show_admin and admin_username and "{admin}" not in custom_template:
                text += f"\n☛ @{admin_username.lstrip('@')}"
            elif "{admin}" in custom_template:
                admin_txt = f"@{admin_username.lstrip('@')}" if (show_admin and admin_username) else ""
                text = text.replace("{admin}", admin_txt)
        else:
            lines = [
                "🚀 <b>سفارش جدید با موفقیت ثبت شد!</b>",
                "",
                "┈┅━━⊷⊰✯⊱⊶━━┅┈",
            ]
            # === PATCH_TRUST_CHANNEL_HIDE_INTERNAL_IDS_V1 ===
            # شماره سفارش/شناسه محصول/کد پیگیری، شناسه‌های داخلیِ سیستم‌ان و
            # فقط برایِ خودِ خریدار یا ادمین معنا دارن؛ توی کانالِ عمومیِ
            # اعتمادسازی هیچ کاربردی ندارن و چون بعضی مسیرهایِ خرید (کارت‌به‌کارت،
            # فروشگاهِ دیجیتال) اونا رو پاس می‌دادن ولی بقیه (مثلاً VPN) نه،
            # نمایش‌شون بینِ پیام‌ها ناهماهنگ بود. از این پس در هیچ حالتی نمایش
            # داده نمی‌شن (پارامترها هنوز در امضایِ تابع نگه داشته شدن که کدِ
            # صداکننده نشکنه، فقط دیگه استفاده نمی‌شن).
            # === PATCH_TRUST_CHANNEL_BIDI_V1 === همان ایزوله‌سازیِ جهت
            lines.append(f"👤 مشتری: <code>{_iso(user_order_txt)}</code>")
            lines.append("┈┅━━⊷⊰✯⊱⊶━━┅┈")
            lines.append(f"🛍 محصول: {_iso(product_name)}")
            # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
            if panel_name:
                lines.append(f"🖥 پنل: {_iso(panel_name)}")
            # === PATCH_ORDER_BUTTON_ID_V1 ===
            if button_name:
                lines.append(f"🔘 دکمه: {_iso(button_name)}")
            lines.append(f"🔢 تعداد سفارش: {_iso(count)}")
            lines.append("")
            lines.append(f"💰 مبلغ پرداختی: {_iso(f'{int(float(amount)):,}')} تومان")
            lines += [
                "",
                "⏰ زمان ثبت سفارش:",
                f"🕒 {_iso(f'{jtime}:{jsec}')}",
                f"📅 {_iso(jdate)}",
                "",
                "┈┅━━⊷⊰✯⊱⊶━━┅┈",
                f"◄ ربات: {_iso(bot_line)}",
                "✪ سپاس از انتخاب و اعتماد شما",
                "- اعتماد شما اعتبار ماست",
            ]
            if show_admin and admin_username:
                lines.append(f"☛ @{admin_username.lstrip('@')}")
            text = "\n".join(lines)
        # === PATCH_TRUST_REPORT_CTA_BTN_V1 ===
        # دکمهٔ اینلاینِ URL (نه callback_data — چون این پیام توی یه کاناله و
        # بیننده ممکنه اصلاً هنوز با بات چت نکرده باشه) زیرِ گزارشِ عمومیِ
        # خرید، با رنگِ آبیِ بومیِ تلگرام (style="primary")، که مستقیم کاربر
        # رو به چتِ خودِ بات می‌بره تا بتونه اقدام به خرید کنه. اگه یوزرنیمِ
        # بات در دسترس نباشه، پیام بدونِ دکمه می‌ره (نه اینکه کرش کنه).
        # === PATCH_TRUST_CHANNEL_CUSTOM_BUTTONS_V1 ===
        kb = await build_trust_keyboard(bot_id, bot_username)
        try:
            await bot.send_message(int(channel_id), text, reply_markup=kb,
                                   parse_mode="HTML")
        except Exception as e:
            # === PATCH_TRUST_SEND_FAIL_VISIBLE_V1 ===
            # قبلاً هر شکستی بی‌صدا بلعیده می‌شد و مالکِ ربات فقط می‌دید
            # «دیگر به کانال نمی‌فرستد» بدونِ هیچ سرنخی. شایع‌ترین علت،
            # HTMLِ نامعتبر در متنِ دلخواه است (مثلاً تگِ بازِ <b>)، بعدش
            # ادمین‌نبودنِ ربات در کانال. حالا دلیل را به خودِ مالک می‌گوییم.
            # === PATCH_TRUST_FAIL_THROTTLE_V1 ===
            # لاگِ سرور: همان خطا (CHAT_RESTRICTED) با هر خرید یک ERROR و یک پیامِ تکراری به مالک —
            # تنظیمِ کانالِ مشتری است نه باگ. حالا هر نوع خطا برایِ هر ربات حداکثر هر ۶ ساعت یک‌بار
            # (یک WARNING و یک پیام به مالک)؛ تا وقتی مالک کانال را درست کند، خریدها مثلِ قبل انجام می‌شوند.
            from log_throttle import should_log
            import re as _re_t
            _kind = _re_t.sub(r"[^A-Za-z_]+", "_", str(e))[:60] or type(e).__name__
            if not should_log(f"trustfail:{bot_id}:{_kind}", 6 * 3600):
                return
            logger.warning("ارسال به کانالِ اعتمادسازی ناموفق بود (bot_id=%s): %s", bot_id, e)
            _hint = ""
            if "CHAT_RESTRICTED" in str(e) or "CHAT_WRITE_FORBIDDEN" in str(e):
                _hint = ("• <b>این خطا یعنی کانال محدود شده</b> (توسطِ تلگرام Restricted شده) یا ربات اجازهٔ "
                         "ارسالِ پیام ندارد. ربات را در کانال ادمین کن و «Post Messages» را روشن کن؛ اگر کانال "
                         "محدود شده، یک کانالِ دیگر وصل کن.\n")
            try:
                from config import Session as _S2
                from models import Bot as _BR
                from sqlalchemy import select as _sel2
                async with _S2() as _s2:
                    _owner = await _s2.scalar(_sel2(_BR.owner_id).where(_BR.id == bot_id))
                if _owner:
                    import html as _h2
                    await bot.send_message(
                        _owner,
                        "⚠️ <b>ارسال به کانالِ اعتمادسازی انجام نشد.</b>\n\n"
                        f"<code>{_h2.escape(str(e)[:200])}</code>\n\n"
                        + _hint +
                        "علت‌هایِ رایج:\n"
                        "• متنِ دلخواهت HTMLِ نامعتبر دارد (مثلاً تگِ باز یا "
                        "بسته‌نشده). با /default به متنِ پیش‌فرض برگرد و تست کن.\n"
                        "• ربات در کانال ادمین نیست یا دسترسیِ ارسال ندارد.\n\n"
                        "(این پیام حداکثر هر ۶ ساعت یک‌بار تکرار می‌شود.)")
            except Exception:
                logger.exception("اطلاعِ خطایِ کانالِ اعتمادسازی به مالک ناموفق بود")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ═══════════════════════ پنلِ مدیریت: اتصال و تنظیمات ═══════════════════════

async def trust_report_menu_text_kb(bot_id: int, plan: str = ""):
    from tenant import get_setting
    channel_id = await get_setting(bot_id, "trust_channel_id", "")
    channel_title = await get_setting(bot_id, "trust_channel_title", "")
    show_admin = await get_setting(bot_id, "trust_show_admin", "0") == "1"
    show_buyer = await get_setting(bot_id, "trust_show_buyer", "0") == "1"  # PATCH_TRUST_SHOW_BUYER_V2
    admin_uname = await get_setting(bot_id, "trust_admin_username", "")  # === PATCH_TRUST_ADMIN_USERNAME_EDIT_V1 ===
    conn_line = (f"📢 کانال: <b>{channel_title or channel_id}</b>" if channel_id
                else "📢 کانال: ⚠️ وصل نشده")
    conn_btn_txt = "✏️ تغییرِ کانال" if channel_id else "🔗 اتصالِ کانال"
    rows = [[InlineKeyboardButton(text=conn_btn_txt, callback_data="trust:setchat", style=ButtonStyle.PRIMARY)]]
    if channel_id:
        rows.append([InlineKeyboardButton(
            text=("👤 نمایشِ آیدیِ ادمین: ✅ فعال" if show_admin else "👤 نمایشِ آیدیِ ادمین: ❌ غیرفعال"),
            callback_data="trust:toggleadmin", style=ButtonStyle.DANGER)])
        # === PATCH_TRUST_ADMIN_USERNAME_EDIT_V1 ===
        # قبلاً یوزرنیمِ ادمین فقط یک‌بار (لحظهٔ اولین روشن‌کردنِ این سوییچ)
        # از cb.from_user.username گرفته می‌شد و هیچ راهی برایِ ویرایشِ دستی‌اش
        # نبود؛ اگه ادمین بعداً یوزرنیمِ تلگرامش رو عوض می‌کرد، این‌جا برایِ
        # همیشه همون یوزرنیمِ قدیمی می‌موند.
        if show_admin:
            _uname_lbl = f"✏️ یوزرنیمِ نمایشی: @{admin_uname}" if admin_uname else "✏️ ثبتِ یوزرنیمِ نمایشی"
            rows.append([InlineKeyboardButton(
                text=_uname_lbl, callback_data="trust:editadmin", style=ButtonStyle.PRIMARY)])
        # === PATCH_TRUST_HIDE_BUYER_V1 ===
        rows.append([InlineKeyboardButton(
            text=("🛒 نمایشِ یوزرنیم/آیدیِ خریدار: ✅ فعال" if show_buyer
                  else "🛒 نمایشِ یوزرنیم/آیدیِ خریدار: ❌ غیرفعال"),
            callback_data="trust:togglebuyer", style=ButtonStyle.DANGER)])
        if show_buyer:
            from tenant.shared.buyer_ident import LABELS as _BL, get_mode as _gm
            rows.append([InlineKeyboardButton(
                text=f"🆔 نوعِ نمایشِ خریدار: {_BL[await _gm(bot_id)]}",
                callback_data="trust:identmode", style=ButtonStyle.PRIMARY)])
        # === PATCH_TRUST_CHANNEL_CUSTOM_TEXT_V1 ===
        # === PATCH_TRUST_TEXT_PRO_ONLY_V1 ===
        # فقط پلنِ حرفه‌ای؛ رایگان و رشد این دکمه را نمی‌بینند و متنِ
        # پیش‌فرض برایشان می‌ماند.
        from plans import Feature, has_feature
        if has_feature(plan, Feature.TRUST_CHANNEL_TEXT):
            rows.append([InlineKeyboardButton(text="✏️ ویرایشِ متنِ پیام", callback_data="trust:edittext",
                                              style=ButtonStyle.PRIMARY)])
            # === PATCH_TRUST_CHANNEL_CUSTOM_BUTTONS_V1 ===
            _btns_on = bool(await get_setting(bot_id, "trust_channel_buttons", ""))
            rows.append([InlineKeyboardButton(
                text=("🔘 دکمه‌هایِ زیرِ پیام: ✅ دلخواه" if _btns_on
                      else "🔘 دکمه‌هایِ زیرِ پیام: پیش‌فرض"),
                callback_data="trust:editbtns", style=ButtonStyle.PRIMARY)])
            # === PATCH_TRUST_CHANNEL_BTN_COLOR_V1 ===
            _color = await get_setting(bot_id, "trust_channel_btn_color", "blue")
            rows.append([InlineKeyboardButton(
                text=f"🎨 رنگِ دکمه‌ها: {_COLOR_LABELS.get(_color, _color)}",
                callback_data="trust:cyclecolor", style=_COLOR_STYLES.get(_color, ButtonStyle.PRIMARY))])
    # === PATCH_PARTNER_SETTINGS_BACK_FIX_V1 ===
    # برایِ رباتِ همکار این صفحه از داخلِ «تنظیمات عمومی ربات»
    # (set:hub_general) باز می‌شود، نه از زیرِ «آمار و گزارشات»
    # (tadm:sec:stats) که برایِ همکار در دسترس نیست.
    _back_cb = "set:hub_general" if plan == "partner" else "tadm:sec:stats"
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_back_cb, style=ButtonStyle.DANGER)])
    text = (
        "🎖 <b>کانال اعتمادسازی</b>\n\n"
        f"{conn_line}\n\n"
        "وقتی وصل باشه، هر بارِ که یه سفارش با موفقیت پرداخت بشه (کیف‌پول، "
        "کارت‌به‌کارت، فروشگاه، هرچی)، یه گزارشِ عمومیِ خوش‌فرمت خودکار توی "
        "این کانال پست می‌شه — برایِ اعتمادسازیِ مشتری‌هایِ جدید.\n\n"
        "🛒 «نمایشِ مشخصاتِ خریدار» پیش‌فرض خاموشه (فقط شمارهٔ سفارش/آیدیِ "
        "ماسک‌شده). با روشن‌کردنش، یوزرنیم و آیدیِ عددیِ کاملِ خریدار هم "
        "تویِ گزارش نشون داده می‌شه.\n\n"
        "⚠️ ربات باید توی اون کانال ادمین باشه (با دسترسیِ ارسالِ پیام)."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@trust_report_router.callback_query(F.data == "tadm:trustreport")
async def trust_report_menu(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@trust_report_router.callback_query(F.data == "trust:identmode")
async def trust_ident_mode(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    from tenant.shared.buyer_ident import cycle, LABELS
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    new = await cycle(row.id)
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("✅ " + LABELS[new])


# === PATCH_TRUST_HIDE_BUYER_V1 ===
@trust_report_router.callback_query(F.data == "trust:togglebuyer")
async def trust_toggle_buyer(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_setting, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    cur = await get_setting(row.id, "trust_show_buyer", "0")
    new = "0" if cur == "1" else "1"
    await set_setting(row.id, "trust_show_buyer", new)
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("✅ نمایشِ مشخصاتِ خریدار " + ("فعال شد" if new == "1" else "غیرفعال شد"))


@trust_report_router.callback_query(F.data == "trust:toggleadmin")
async def trust_toggle_admin(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_setting, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    cur = await get_setting(row.id, "trust_show_admin", "0")
    new = "0" if cur == "1" else "1"
    await set_setting(row.id, "trust_show_admin", new)
    if new == "1":
        existing = await get_setting(row.id, "trust_admin_username", "")
        if not existing and cb.from_user.username:
            await set_setting(row.id, "trust_admin_username", cb.from_user.username)
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("✅ ذخیره شد")


# === PATCH_TRUST_ADMIN_USERNAME_EDIT_V1 ===
@trust_report_router.callback_query(F.data == "trust:editadmin")
async def trust_editadmin_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(TrustReportFSM.editadmin)
    await cb.message.answer(
        "✏️ <b>یوزرنیمِ نمایشی در گزارشِ کانالِ اعتمادسازی</b>\n\n"
        "یوزرنیمِ تلگرامِ جدید رو (با یا بدون @) بفرست:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@trust_report_router.message(TrustReportFSM.editadmin, F.text)
async def trust_editadmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    uname = (msg.text or "").strip().lstrip("@")
    if not uname or not uname.replace("_", "").isalnum() or len(uname) > 32:
        await msg.answer("❌ یوزرنیمِ نامعتبر. دوباره بفرست (فقط حروف/عدد/زیرخط، بدون فاصله):")
        return
    await set_setting(row.id, "trust_admin_username", uname)
    await state.clear()
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    await msg.answer(f"✅ یوزرنیمِ نمایشی به @{uname} تغییر کرد.", reply_markup=kb)


@trust_report_router.callback_query(F.data == "trust:setchat")
async def trust_setchat_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(TrustReportFSM.setchat)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🔗 <b>اتصالِ کانالِ اعتمادسازی</b>\n\n"
        "یه پیام از همون کانال برام فوروارد کن (یا یوزرنیمش رو با @ بفرست).\n\n"
        "⚠️ قبلش مطمئن شو ربات توی اون کانال ادمینه (با دسترسیِ ارسالِ پیام).",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@trust_report_router.message(TrustReportFSM.setchat)
async def trust_setchat_recv(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, main_kb_or_remove, is_owner_or_admin

    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    if (msg.text or "").strip() in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb)
        return

    # === PATCH_TRUST_REPORT_FORWARD_ORIGIN_V1 ===
    target = None
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "type", None) == "channel":
        target = fwd_origin.chat.id
    elif msg.forward_from_chat:
        target = msg.forward_from_chat.id
    else:
        raw = (msg.text or "").strip()
        if raw.startswith("@") and len(raw) > 1:
            target = raw
        elif raw.lstrip("-").isdigit():
            target = int(raw)

    if target is None:
        await msg.answer("❌ این یک فورواردِ معتبر یا یوزرنیم نبود. یه پیام از همون کانال "
                         "فوروارد کن، یا یوزرنیمش رو با @ بفرست، یا «❌ لغو» رو بزن.")
        return

    try:
        chat = await bot.get_chat(target)
    except Exception as e:
        await msg.answer(f"❌ به این کانال دسترسی ندارم: {e}")
        return
    if chat.type != "channel":
        await msg.answer(f"❌ این یه «{chat.type}»ه، نه کانال. یه پیام از خودِ کانال فوروارد کن.")
        return
    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except Exception as e:
        await msg.answer(f"❌ نتونستم عضویتِ ربات توی این کانال رو چک کنم: {e}")
        return
    if member.status in ("left", "kicked"):
        await msg.answer("❌ ربات عضوِ این کانال نیست. اول ربات رو با دسترسیِ ادمین (ارسالِ پیام) "
                         "به کانال اضافه کن.")
        return

    from tenant import set_setting
    await set_setting(row.id, "trust_channel_id", str(chat.id))
    await set_setting(row.id, "trust_channel_title", chat.title or chat.username or str(chat.id))
    await state.clear()
    kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
    await msg.answer(f"✅ «{chat.title}» به‌عنوانِ کانالِ اعتمادسازی وصل شد.", reply_markup=kb)
    text, ikb = await trust_report_menu_text_kb(row.id, row.plan)
    await msg.answer(text, reply_markup=ikb)


# === PATCH_TRUST_CHANNEL_CUSTOM_TEXT_V1 ===
_TRUST_TEXT_HELP = (
    "✏️ <b>ویرایشِ متنِ پیامِ کانالِ اعتمادسازی</b>\n\n"
    "متنِ دلخواهت رو بفرست (قالب‌بندیِ HTML تلگرام پشتیبانی می‌شه: بولد، "
    "ایتالیک، لینک، نقل‌قول، اسپویلر و...).\n\n"
    # === PATCH_TRUST_PREMIUM_EMOJI_NOTICE_V1 ===
    # محدودیتِ خودِ تلگرام (نه باگِ ما): ربات فقط وقتی می‌تواند
    # custom_emoji بفرستد که یوزرنیمِ اضافه از Fragment خریده باشد، یا
    # پیام مستقیم به چتِ خصوصی/گروه/سوپرگروه برود و مالکِ ربات پرمیوم
    # داشته باشد. «کانال» در آن فهرست نیست — برایِ همین ایموجیِ پرمیوم
    # در کانالِ اعتمادسازی بی‌صدا به ایموجیِ معمولی تنزل می‌کند.
    "⭐ <b>دربارهٔ ایموجیِ پرمیوم:</b> می‌تونی توی متن ایموجیِ پرمیوم "
    "بذاری، ولی تلگرام اجازه نمی‌ده رباتِ معمولی توی «کانال» ایموجیِ "
    "پرمیوم پست کنه — بدونِ هیچ خطایی خودکار به ایموجیِ معمولی تبدیل "
    "می‌شه. این محدودیتِ خودِ تلگرامه، نه ایرادِ ربات.\n"
    "فقط در دو حالت کار می‌کنه:\n"
    "• رباتت یوزرنیمِ اختصاصی از Fragment خریده باشه، یا\n"
    "• به‌جایِ کانال، از یه گروه/سوپرگروه استفاده کنی و خودت اشتراکِ "
    "پرمیوم داشته باشی.\n\n"
    "🔤 <b>جاگذاری‌هایِ خودکار</b> (هرکدوم رو خواستی توی متنت بذار):\n"
    "<code>{buyer}</code> → مشخصاتِ خریدار\n"
    "<code>{product}</code> → نامِ محصول\n"
    "<code>{count}</code> → تعداد\n"
    "<code>{amount}</code> → مبلغِ پرداختی\n"
    "<code>{time}</code> → ساعت\n"
    "<code>{date}</code> → تاریخ\n"
    "<code>{bot}</code> → یوزرنیمِ ربات\n"
    "<code>{panel}</code> → نامِ نمایشیِ پنل (فقط برایِ خریدهایِ VPN؛ اگه معلوم نباشه «—»)\n"
    "<code>{button}</code> یا <code>{bottom}</code> → اسمِ دکمه‌ای که مشتری زده (اگه معلوم نباشه «—»)\n"
    "<code>{admin}</code> → آیدیِ ادمین (فقط اگه از تنظیماتِ بالا روشن باشه)\n\n"
    "برایِ بازگشت به متنِ پیش‌فرض، /default رو بفرست."
)


@trust_report_router.callback_query(F.data == "trust:edittext")
async def trust_edittext_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting, smart_cancel_kb, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    # === PATCH_TRUST_TEXT_PRO_ONLY_V1 ===
    # گیتِ سمتِ سرور: دکمه برایِ پلن‌هایِ پایین‌تر ساخته نمی‌شود، ولی
    # callbackِ قدیمی/دستی هم نباید رد شود.
    from plans import Feature, has_feature, upgrade_hint
    if not has_feature(row.plan, Feature.TRUST_CHANNEL_TEXT):
        await cb.answer(upgrade_hint(Feature.TRUST_CHANNEL_TEXT), show_alert=True)
        return
    current = await get_setting(row.id, "trust_channel_template", "")
    await state.set_state(TrustReportFSM.edittext)
    extra = ("\n\n📝 متنِ فعلی:\n" + current) if current else "\n\n📝 الان از متنِ پیش‌فرض استفاده می‌شه."
    await cb.message.answer(_TRUST_TEXT_HELP + extra, reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@trust_report_router.message(TrustReportFSM.edittext, F.text)
async def trust_edittext_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, main_kb_or_remove, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    # === PATCH_TRUST_TEXT_PRO_ONLY_V1 ===
    from plans import Feature, has_feature
    if not has_feature(row.plan, Feature.TRUST_CHANNEL_TEXT):
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb)
        return
    if raw == "/default":
        await set_setting(row.id, "trust_channel_template", "")
        await state.clear()
        await msg.answer("✅ متنِ پیامِ کانالِ اعتمادسازی به حالتِ پیش‌فرض بازگشت.")
        text, kb = await trust_report_menu_text_kb(row.id, row.plan)
        await msg.answer(text, reply_markup=kb)
        return
    template = msg.html_text or msg.text or ""
    if len(template) > 3500:
        await msg.answer("⚠️ متن بیش از حد طولانی است؛ حداکثر ۳۵۰۰ کاراکتر ارسال کنید.")
        return
    # === PATCH_TRUST_TEMPLATE_PREVIEW_V1 ===
    # قبل از ذخیره، همان متن را با مقادیرِ نمونه به خودِ مالک می‌فرستیم.
    # اگر HTMLش نامعتبر باشد، همین‌جا خطا می‌گیرد — نه چند ساعت بعد که
    # اولین خرید انجام شود و پیام بی‌صدا به کانال نرود.
    preview = (template
               .replace("{buyer}", "<code>73******50</code>")
               .replace("{product}", "۵ گیگ / ۳۰ روز")
               .replace("{count}", "1")
               .replace("{amount}", "۱۷,۵۰۰")
               .replace("{time}", "22:18:11")
               .replace("{date}", "1405/06/07")
               .replace("{bot}", f"@{(await bot.me()).username}")
               .replace("{panel}", "مولتی لوکیشن")  # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
               .replace("{button}", "🇩🇪 آلمان").replace("{bottom}", "🇩🇪 آلمان")  # === PATCH_ORDER_BUTTON_ID_V1 ===
               .replace("{admin}", "@admin"))
    try:
        await msg.answer("👀 <b>پیش‌نمایش:</b>")
        await msg.answer(preview)
    except Exception as e:
        import html as _h3
        await msg.answer(
            "❌ <b>این متن ذخیره نشد.</b>\n\n"
            f"<code>{_h3.escape(str(e)[:200])}</code>\n\n"
            "قالب‌بندیِ HTML مشکل دارد — معمولاً یک تگِ بسته‌نشده مثل "
            "<b>&lt;b&gt;</b> یا نویسه‌هایِ &lt; و &gt; که باید ساده نوشته "
            "شوند. اصلاحش کن و دوباره بفرست.")
        return

    await set_setting(row.id, "trust_channel_template", template)
    await state.clear()
    await msg.answer("✅ متنِ پیامِ کانالِ اعتمادسازی ذخیره شد.")
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    await msg.answer(text, reply_markup=kb)


# === PATCH_TRUST_CHANNEL_CUSTOM_BUTTONS_V1 ===
_TRUST_BTNS_HELP = (
    "🔘 <b>دکمه‌هایِ شیشه‌ایِ زیرِ پیامِ کانال</b>\n\n"
    "هر خط یک ردیفِ دکمه است، با این قالب:\n"
    "<code>متنِ دکمه - لینک</code>\n\n"
    "برایِ دو یا سه دکمه در <b>یک ردیف</b>، بینشان <code>||</code> بگذار.\n\n"
    "📌 <b>نمونه:</b>\n"
    "<code>🛒 ثبتِ سفارش - {bot}\n"
    "📢 کانالِ ما - @mychannel || 💬 پشتیبانی - @myadmin</code>\n\n"
    "🔤 <code>{bot}</code> خودکار به لینکِ همین ربات تبدیل می‌شود.\n"
    "لینک می‌تواند <code>https://…</code> یا یوزرنیم با <code>@</code> باشد.\n\n"
    "⚠️ چون این پیام داخلِ کاناله و بیننده ممکنه هنوز ربات رو استارت "
    "نکرده باشه، فقط دکمهٔ لینک‌دار کار می‌کنه.\n\n"
    "برایِ بازگشت به دکمهٔ پیش‌فرض، /default رو بفرست."
)


@trust_report_router.callback_query(F.data == "trust:editbtns")
async def trust_editbtns_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting, smart_cancel_kb, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    from plans import Feature, has_feature, upgrade_hint
    if not has_feature(row.plan, Feature.TRUST_CHANNEL_TEXT):
        await cb.answer(upgrade_hint(Feature.TRUST_CHANNEL_TEXT), show_alert=True)
        return
    current = await get_setting(row.id, "trust_channel_buttons", "")
    import html as _h
    extra = (f"\n\n📝 تنظیمِ فعلی:\n<code>{_h.escape(current)}</code>"
             if current else "\n\n📝 الان دکمهٔ پیش‌فرض نمایش داده می‌شه.")
    await state.set_state(TrustReportFSM.editbtns)
    await cb.message.answer(_TRUST_BTNS_HELP + extra,
                            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


# === PATCH_TRUST_CHANNEL_BTN_COLOR_V1 ===
@trust_report_router.callback_query(F.data == "trust:cyclecolor")
async def trust_cycle_color(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_setting, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    from plans import Feature, has_feature, upgrade_hint
    if not has_feature(row.plan, Feature.TRUST_CHANNEL_TEXT):
        await cb.answer(upgrade_hint(Feature.TRUST_CHANNEL_TEXT), show_alert=True)
        return
    current = await get_setting(row.id, "trust_channel_btn_color", "blue")
    idx = (_COLOR_ORDER.index(current) + 1) % len(_COLOR_ORDER) if current in _COLOR_ORDER else 0
    new_color = _COLOR_ORDER[idx]
    await set_setting(row.id, "trust_channel_btn_color", new_color)
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer(f"✅ رنگ شد: {_COLOR_LABELS.get(new_color, new_color)}")


@trust_report_router.message(TrustReportFSM.editbtns, F.text)
async def trust_editbtns_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, main_kb_or_remove, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    from plans import Feature, has_feature
    if not has_feature(row.plan, Feature.TRUST_CHANNEL_TEXT):
        await state.clear()
        return

    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb)
        return
    if raw == "/default":
        await set_setting(row.id, "trust_channel_buttons", "")
        await state.clear()
        await msg.answer("✅ دکمه‌هایِ زیرِ پیام به حالتِ پیش‌فرض بازگشت.")
        text, kb = await trust_report_menu_text_kb(row.id, row.plan)
        await msg.answer(text, reply_markup=kb)
        return
    if len(raw) > 1500:
        await msg.answer("⚠️ متن بیش از حد طولانی است؛ حداکثر ۱۵۰۰ کاراکتر.")
        return

    me_username = (await bot.me()).username
    rows, errors = _parse_trust_buttons(
        raw.replace("{bot}", f"https://t.me/{me_username}" if me_username else ""))
    if errors:
        import html as _h
        await msg.answer("❌ <b>ذخیره نشد:</b>\n" +
                         "\n".join("• " + _h.escape(e) for e in errors[:6]) +
                         "\n\nاصلاحش کن و دوباره بفرست.")
        return
    if not rows:
        await msg.answer("❌ هیچ دکمهٔ معتبری پیدا نشد. نمونه را دوباره ببین.")
        return

    # پیش‌نمایش قبل از ذخیره — همان‌طور که برایِ متنِ پیام هم انجام می‌شود.
    try:
        await msg.answer("👀 <b>پیش‌نمایشِ دکمه‌ها:</b>",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as e:
        import html as _h
        await msg.answer("❌ <b>این دکمه‌ها ذخیره نشد.</b>\n\n"
                         f"<code>{_h.escape(str(e)[:200])}</code>")
        return

    await set_setting(row.id, "trust_channel_buttons", raw)
    await state.clear()
    await msg.answer("✅ دکمه‌هایِ زیرِ پیامِ کانالِ اعتمادسازی ذخیره شد.")
    text, kb = await trust_report_menu_text_kb(row.id, row.plan)
    await msg.answer(text, reply_markup=kb)
