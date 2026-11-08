"""
platform_chpost.py — بخشِ «ارسال پست به کانال» جدا از platform_bot.py.

استخراج‌شده از platform_bot.py (~۵۳۳ خط) به فایلِ مستقل تا platform_bot.py
سبک‌تر بشه. رفتار ۱۰۰٪ حفظ شده — همون هندلرها، همون callback_dataها.

نکته‌ی مهمِ FSM:
پنج استیتِ chpost-specific که قبلاً توی SettingsFSMِ گودالی (God FSM) بودن،
حالا توی کلاسِ اختصاصیِ خود این ماژول قرار گرفتن (ChpostFSM.channel،
ChpostFSM.content، ChpostFSM.btn_label، ChpostFSM.btn_url، ChpostFSM.btn_emoji).
این پاکیزه‌تره و از تداخل با سایرِ استفاده‌کنندگانِ SettingsFSM جلوگیری می‌کنه.
"""
from __future__ import annotations

import html as _html
import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, Message, SwitchInlineQueryChosenChat,
)

from config import PLATFORM_ADMIN_ID
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

chpost_router = Router()


class ChpostFSM(StatesGroup):
    """FSMِ اختصاصیِ همین ماژول — قبلاً بخشی از SettingsFSMِ بزرگِ پلتفرم بود."""
    channel = State()   # دریافت فوروارد/یوزرنیمِ کانالِ مقصدِ پست
    content = State()   # دریافت متن یا عکسِ پست
    btn_label = State()  # دریافت متنِ دکمه
    btn_url = State()    # دریافت لینکِ دکمه
    btn_emoji = State()  # ایموجیِ پرمیومِ اختیاریِ دکمه
    # === PATCH_CHPOST_ATTACH_EXISTING_V1 ===
    target_msg = State()   # دریافت فوروارد/لینکِ پستی که خودِ ادمین مستقیم در کانال گذاشته
    attach_idle = State()  # حالتِ «در حالِ چیدنِ دکمه برای پستِ موجود» (معادلِ content ولی بدونِ محتوای جدید)
    # === PATCH_CHPOST_DETACH_EXISTING_V1 ===
    detach_target = State()  # دریافت فوروارد/لینکِ پستی که می‌خواهیم دکمه‌های شیشه‌ایِ زیرش برداشته شود
    # === PATCH_CHPOST_USERBOT_V1 === اتصالِ اکانتِ پرمیومِ مدیر (api_id ← api_hash ← StringSession)
    ua_api_id = State()
    ua_api_hash = State()
    ua_session = State()
    ua_phone = State()      # ورود با شماره
    ua_code = State()
    ua_pw = State()


# ────────── lazy-import wrappers از platform_bot ──────────
# چون این ماژول از platform_bot ایمپورت می‌شه، اگر مستقیم helperهایی که
# توی خودِ platform_bot تعریف شدن رو ایمپورت کنیم، ایمپورت دَورِ می‌شه.
# بجاش موقعِ فراخوانی lazyـ ایمپورت می‌کنیم:

async def smart_cancel_kb():
    from platform_bot import smart_cancel_kb as _fn
    return await _fn()


async def smart_home_kb(uid: int, show_admin: bool = False):
    from platform_bot import smart_home_kb as _fn
    return await _fn(uid, show_admin=show_admin)


async def get_pset(key: str, default=""):
    from platform_bot import get_pset as _fn
    return await _fn(key, default)


async def set_pset(key: str, value):
    from platform_bot import set_pset as _fn
    return await _fn(key, value)


def is_admin(uid: int) -> bool:
    from platform_bot import is_admin as _fn
    return _fn(uid)


async def has_perm(uid: int, perm: str) -> bool:
    from platform_bot import has_perm as _fn
    return await _fn(uid, perm)


# === PATCH_CHPOST_MISSING_HELPERS_V1 ===
# ensure_home_loaded توی خودِ platform_bot.py تعریف شده ولی هنگام استخراج
# فراموش شد — با کلیک روی «انتخاب دکمهٔ خانه» خطا می‌داد.
async def ensure_home_loaded() -> None:
    """Lazy wrapper — بارگذاری تنظیماتِ صفحهٔ اصلی."""
    from platform_bot import ensure_home_loaded as _fn
    await _fn()


BTN_CANCEL = "❌ لغو"


# === PATCH_FIX_CHPOST_HOME_GLOBALS_V1 ===
# _HOME_CFG/_HOME_CUSTOM/_hcb_find همگی توی platform_bot زندگی می‌کنن و
# اینجا مستقیم (بدونِ ایمپورت) استفاده شده بودند → NameError موقعِ
# «انتخاب دکمهٔ خانه». چون این گلوبال‌ها با ensure_home_loaded تازه‌سازی
# می‌شن، باید لحظهٔ استفاده از خودِ platform_bot خونده بشن (نه یک‌بار موقعِ ایمپورت).
def _pb_home_cfg() -> dict:
    from platform_bot import _HOME_CFG as _v
    return _v


def _pb_home_custom() -> list:
    from platform_bot import _HOME_CUSTOM as _v
    return _v


def _pb_hcb_find(bid: str):
    from platform_bot import _hcb_find as _fn
    return _fn(bid)


# === PATCH_PLATFORM_CHPOST_BUTTON_COLOR_EMOJI_V1 ===
def _pb_home_style_map() -> dict:
    from platform_bot import HOME_STYLE as _v
    return _v


def _pb_hcb_color_style_map() -> dict:
    from platform_bot import _HCB_COLOR_STYLE as _v
    return _v


def _home_default_label(key: str) -> str:
    """Lazy: HOME_DEFAULT_LABEL از platform_bot"""
    from platform_bot import HOME_DEFAULT_LABEL
    return HOME_DEFAULT_LABEL.get(key, key)


# جایگزین ارجاعِ مستقیم HOME_DEFAULT_LABEL — بعداً در بدنه‌ی کد استفاده می‌شه
class _HomeDefaultLabelProxy:
    def __getitem__(self, key):
        from platform_bot import HOME_DEFAULT_LABEL
        return HOME_DEFAULT_LABEL[key]
    def get(self, key, default=None):
        from platform_bot import HOME_DEFAULT_LABEL
        return HOME_DEFAULT_LABEL.get(key, default)
    def __iter__(self):
        # === PATCH_CHPOST_HOME_LABEL_ITER_V1 ===
        # بدونِ این متد، حلقهٔ «for k in HOME_DEFAULT_LABEL» (توی
        # _chpost_home_pick_kb) با نبودِ __iter__ به پروتکلِ قدیمیِ
        # ایتریشن (تکرارِ __getitem__ با اندیس‌های صحیحِ ۰،۱،۲،...) سقوط
        # می‌کرد؛ چون HOME_DEFAULT_LABELِ واقعی یک دیکشنریِ کلید-رشته‌ایه،
        # HOME_DEFAULT_LABEL[0] بلافاصله KeyError می‌داد — نه IndexError —
        # پس پایتون این خطا رو نشونهٔ «پایانِ حلقه» نمی‌دونست و بی هیچ
        # try/exceptی مستقیم بالا می‌رفت تا هندلرِ عمومی (همون ارورهای
        # ERR-XXXXXX روی دکمهٔ «🏠 دکمه‌های صفحهٔ اصلی»).
        from platform_bot import HOME_DEFAULT_LABEL
        return iter(HOME_DEFAULT_LABEL)


HOME_DEFAULT_LABEL = _HomeDefaultLabelProxy()


# ───────────────── ارسال پست به کانال (با دکمهٔ اینلاینِ لینک) — پنل مدیر اصلی ─────────────────

def _checklist_to_text(checklist) -> str:
    """چون بات‌های معمولی (بدون اتصال Business) نمی‌توانند چک‌لیستِ تعاملی به کانال بفرستند،
    این تابع عنوان و آیتم‌های چک‌لیست را به یک متنِ ساده تبدیل می‌کند."""
    lines = [f"📋 <b>{_html.escape(checklist.title or '')}</b>", ""]
    for task in (checklist.tasks or []):
        mark = "☑️" if getattr(task, "completed_by_user", None) or getattr(task, "completed_by_chat", None) else "▫️"
        lines.append(f"{mark} {_html.escape(task.text or '')}")
    return "\n".join(lines).strip()



# === PATCH_CHPOST_EMOJI_CHANNEL_FALLBACK_V1 ===
# چرا لازم شد: تویِ چتِ خصوصی، ایموجیِ پرمیومِ دکمه (icon_custom_emoji_id)
# درست نمایش داده می‌شد ولی تویِ کانال نه. علتش محدودیتِ خودِ تلگرامه —
# نمایشِ ایموجیِ سفارشی تویِ پستِ کانال به سطحِ بوستِ کانال بستگی داره و
# چیزی نیست که از سمتِ ربات بشه فعالش کرد.
#
# راهِ دورزدن: علاوه بر فرستادنِ icon_custom_emoji_id (که هرجا پشتیبانی بشه
# نسخهٔ پرمیوم رو نشون می‌ده)، *معادلِ معمولیِ همون ایموجی* رو هم به ابتدایِ
# متنِ دکمه اضافه می‌کنیم. نتیجه: تویِ چتِ خصوصی نسخهٔ پرمیوم، و تویِ کانال
# حداقل نسخهٔ معمولی دیده می‌شه — به‌جایِ دکمهٔ بی‌شکلک.
_EMOJI_ID_TO_CHAR: dict[str, str] = {}


def _emoji_char_for_id(emoji_id) -> str:
    """از رویِ شناسهٔ ایموجیِ پرمیوم، کاراکترِ معمولیِ معادلش رو برمی‌گردونه."""
    global _EMOJI_ID_TO_CHAR
    if not emoji_id:
        return ""
    if not _EMOJI_ID_TO_CHAR:
        try:
            from premium_emoji import PREMIUM_EMOJI_IDS
            _EMOJI_ID_TO_CHAR = {v: k for k, v in PREMIUM_EMOJI_IDS.items()
                                 if v and k and k.strip()}
        except Exception:
            _EMOJI_ID_TO_CHAR = {"_": "_"}
    return _EMOJI_ID_TO_CHAR.get(str(emoji_id), "")


def _emoji_for_destination(kw: dict, label: str, emoji_id, for_channel: bool) -> str:
    """=== PATCH_CHPOST_EMOJI_BY_DESTINATION_V1 ===
    بسته به مقصد، شکلکِ دکمه رو به روشِ درست اعمال می‌کنه.

    شواهدِ تست (دو اسکرین‌شاتِ کاربر) نشون داد:
      • چتِ خصوصی → فیلدِ icon_custom_emoji_id رندر می‌شه (شکلکِ پرمیوم دیده می‌شه)
      • کانال     → همون فیلد اصلاً رندر نمی‌شه و دکمه *بی‌شکلک* می‌مونه
        (حتی شکلک‌هایِ داخلِ متنِ پیام هم تویِ کانال به نسخهٔ معمولی برمی‌گردن)

    پس برایِ هر مقصد بهترین حالتِ ممکن رو می‌فرستیم:
      • کانال     → شکلکِ معمولی داخلِ *متنِ* دکمه (چون همیشه رندر می‌شه)
      • غیرِ کانال → شکلکِ پرمیوم با فیلدِ اختصاصی + حذفِ معادلِ معمولی از متن
                     (تا دو شکلکِ تکراری کنارِ هم نیفته)
    """
    ch = _emoji_char_for_id(emoji_id)
    lbl = (label or "").strip()

    # === PATCH_CHPOST_SEND_PREMIUM_ALWAYS_V1 ===
    # شناسهٔ ایموجیِ پرمیوم *همیشه* فرستاده می‌شه — چه کانال چه چتِ خصوصی.
    # دلیل: تویِ این جریان، برچسبِ دکمه و ایموجی دو ورودیِ *جدا* هستن، پس
    # برچسب معمولاً خودش شکلکی نداره و خطرِ «دو شکلکِ تکراری» وجود نداره.
    # اگه تلگرام تویِ مقصدی این فیلد رو رندر نکنه، دکمه فقط بدونِ شکلک
    # دیده می‌شه (بدتر از قبل نمی‌شه)، ولی هرجا پشتیبانی بشه شکلکِ پرمیومِ
    # متحرک نمایش داده می‌شه.
    kw["icon_custom_emoji_id"] = emoji_id

    # اگه ادمین اتفاقاً معادلِ معمولیِ همون شکلک رو هم اولِ برچسب تایپ کرده
    # باشه، حذفش می‌کنیم تا تکراری نشه.
    if ch and lbl.startswith(ch):
        lbl = lbl[len(ch):].lstrip()
    return lbl


def _label_strip_emoji(label: str, emoji_id) -> str:
    """=== PATCH_CHPOST_PREMIUM_EMOJI_RESTORE_V1 ===
    وقتی ایموجی به‌صورتِ icon_custom_emoji_id (نسخهٔ پرمیوم) فرستاده می‌شه،
    معادلِ معمولیش باید از *متنِ* دکمه برداشته بشه — وگرنه هر دو با هم
    نمایش داده می‌شن (دو شکلکِ کنارِ هم، همون چیزی که گزارش شد)."""
    ch = _emoji_char_for_id(emoji_id)
    lbl = (label or "").strip()
    if ch and lbl.startswith(ch):
        lbl = lbl[len(ch):].lstrip()
    return lbl


def _chpost_kb(buttons: list[list[str]], for_channel: bool = False) -> InlineKeyboardMarkup | None:
    """کیبورد اینلاینِ پست از رویِ لیستِ [متن, لینک, emoji_id‌یا‌None]؛ حداکثر دو دکمه
    در هر ردیف. PATCH_CHPOST_BLUE_EMOJI_BTN_V1 — همیشه آبی (PRIMARY) و در صورتِ
    وجودِ emoji_id، ایموجیِ پرمیوم کنارِ متن نمایش داده می‌شود."""
    if not buttons:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for item in buttons:
        label, url = item[0], item[1]
        emoji_id = item[2] if len(item) > 2 else None
        kw = {"url": url, "style": ButtonStyle.PRIMARY}
        if emoji_id:
            # === PATCH_CHPOST_EMOJI_BY_DESTINATION_V1 ===
            label = _emoji_for_destination(kw, label, emoji_id, for_channel)
        # === PATCH_CHPOST_KEEP_EMOJI_IN_TEXT_V1 ===
        # مثلِ بالا: hx_button شکلک رو از متن جدا می‌کنه و تویِ پستِ کانال
        # ناپدید می‌شه؛ این‌جا متن دست‌نخورده می‌مونه.
        row.append(InlineKeyboardButton(text=label, **kw))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _chpost_menu() -> tuple[str, InlineKeyboardMarkup]:
    # === PATCH_CHPOST_GROUP_SUPPORT_V1 ===
    # حالا هم کانال هم گروه می‌تونن هم‌زمان وصل باشن.
    chat_id = await get_pset("chpost_chat_id", "")
    title = await get_pset("chpost_chat_title", "")
    grp_id = await get_pset("chpost_group_id", "")
    grp_title = await get_pset("chpost_group_title", "")

    ch_line = (f"✅ کانالِ متصل: <b>{_html.escape(title or chat_id)}</b>"
               if chat_id else "🔴 کانالی وصل نشده")
    gr_line = (f"✅ گروهِ متصل: <b>{_html.escape(grp_title or grp_id)}</b>"
               if grp_id else "🔴 گروهی وصل نشده")

    txt = (
        "📢 <b>ارسال پیام به کانال و گروه</b>\n\n"
        f"{ch_line}\n{gr_line}\n\n"
    )
    rows = []
    # === PATCH_CHPOST_USERBOT_V1 === ارسال با اکانتِ پرمیومِ خودِ مدیر (تنها راهِ مطمئنِ حفظِ ایموجیِ پرمیوم)
    _ua_on = bool(await get_pset("chpost_ua_session", ""))
    rows.append([hx_button(("👤 اکانتِ پرمیوم: متصل ✅ (مدیریت)" if _ua_on else "👤 اتصالِ اکانتِ پرمیوم (ارسال با ایموجیِ پرمیوم)"),
                           callback_data="chpost:ua_menu", style=ButtonStyle.SUCCESS)])
    if chat_id or grp_id:
        txt += (
            "با «ساخت پست جدید» یک پیامِ متنی یا عکس‌دار — با همون قالب‌بندیِ تلگرامِ خودت "
            "(بولد، ایتالیک، لینک، اسپویلر، ایموجی پرمیوم و...) — همراه با دکمهٔ اینلاینِ لینک "
            "می‌سازی و بعد انتخاب می‌کنی به کانال، گروه، یا هر دو فرستاده بشه.\n\n"
            "💡 چون ایموجیِ پرمیوم فقط از بات‌هایی با یوزرنیمِ ویژهٔ Fragment قابلِ ارسال است، "
            "اگه می‌خوای ایموجیِ پرمیومت دقیقاً همون‌طور که خودت می‌بینی بمونه، پست "
            "رو خودت مستقیم بذار و از «📎 افزودنِ دکمه به پستِ موجود» استفاده کن."
        )
        rows.append([hx_button("📝 ساخت پست جدید", callback_data="chpost:new")])
        rows.append([hx_button("📎 افزودنِ دکمه به پستِ موجود", callback_data="chpost:attach_start")])
        # === PATCH_CHPOST_DETACH_EXISTING_V1 ===
        rows.append([hx_button("🗑 حذفِ دکمه‌های شیشه‌ای از پستِ موجود",
                                          callback_data="chpost:detach_start")])
    else:
        txt += (
            "۱) ربات را در کانال/گروهِ موردنظر <b>ادمین</b> کن (با دسترسیِ «ارسال پیام»).\n"
            "۲) دکمهٔ زیر را بزن و یک پیام از همون‌جا برایم فوروارد کن (یا یوزرنیمش را بفرست)."
        )

    rows.append([hx_button("✏️ تغییر کانال" if chat_id else "🔗 اتصال کانال",
                                      callback_data="chpost:setch")])
    if chat_id:
        rows.append([hx_button("🗑 قطع اتصال کانال", callback_data="chpost:delch",
                                          style=ButtonStyle.DANGER)])
    rows.append([hx_button("✏️ تغییر گروه" if grp_id else "🔗 اتصال گروه",
                                      callback_data="chpost:setgr")])
    if grp_id:
        rows.append([hx_button("🗑 قطع اتصال گروه", callback_data="chpost:delgr",
                                          style=ButtonStyle.DANGER)])
    rows.append([hx_button("⬅️ بازگشت", callback_data="adm:cat:support",
                                      style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


@chpost_router.callback_query(F.data == "chpost:menu")
async def chpost_menu(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    txt, kb = await _chpost_menu()
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


# === PATCH_CHPOST_GROUP_SUPPORT_V1 ===
@chpost_router.callback_query(F.data == "chpost:setgr")
async def chpost_setgr(cb: CallbackQuery, state: FSMContext):
    """اتصالِ گروه — همون جریانِ اتصالِ کانال، فقط نوعش «گروه» علامت می‌خوره."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(ChpostFSM.channel)
    await state.update_data(chpost_conn_kind="group")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "👥 ربات را در گروهِ موردنظر <b>ادمین</b> کن (با دسترسیِ «ارسال پیام»)، "
        "بعد یک پیام از همان گروه برایم فوروارد کن (یا یوزرنیمش را با @ بفرست).",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@chpost_router.callback_query(F.data == "chpost:delgr")
async def chpost_delgr(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await set_pset("chpost_group_id", "")
    await set_pset("chpost_group_title", "")
    await cb.answer("🗑 اتصالِ گروه قطع شد")
    txt, kb = await _chpost_menu()
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)


@chpost_router.callback_query(F.data == "chpost:setch")
async def chpost_setch(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(ChpostFSM.channel)
    await state.update_data(chpost_conn_kind="channel")  # PATCH_CHPOST_GROUP_SUPPORT_V1
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🔗 <b>اتصال کانال</b>\n\n"
        "یک پیام از کانالِ موردنظر برایم <b>فوروارد کن</b>، یا یوزرنیمش را با @ بفرست "
        "(مثلاً <code>@mychannel</code>).\n\n"
        "⚠️ قبلش مطمئن شو ربات توی آن کانال <b>ادمین</b> است و دسترسیِ «ارسال پیام» دارد.",
        reply_markup=await smart_cancel_kb(),
    )
    await cb.answer()


@chpost_router.callback_query(F.data == "chpost:delch")
async def chpost_delch(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await set_pset("chpost_chat_id", "")
    await set_pset("chpost_chat_title", "")
    txt, kb = await _chpost_menu()
    await cb.message.edit_text(txt, reply_markup=kb)
    await cb.answer("قطع شد")


@chpost_router.message(ChpostFSM.channel)
async def chpost_setch_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        txt, kb = await _chpost_menu()
        await msg.answer(txt, reply_markup=kb)
        return

    # === PATCH_CHPOST_FORWARD_ORIGIN_V1 === (هم‌راستا با tenant/__init__.py)
    target = None
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "type", None) == "channel":
        target = fwd_origin.chat.id
    elif msg.forward_from_chat and msg.forward_from_chat.type == "channel":
        target = msg.forward_from_chat.id
    else:
        raw = (msg.text or "").strip()
        if raw.startswith("@") and len(raw) > 1:
            target = raw
        elif raw.lstrip("-").isdigit():
            target = int(raw)

    if target is None:
        await msg.answer(
            "❌ این یک فورواردِ کانال یا یوزرنیمِ معتبر نبود. یک پیام از کانال فوروارد کن یا "
            "یوزرنیمش را با @ بفرست، یا «❌ لغو» را بزن.")
        return

    try:
        chat = await bot.get_chat(target)
        # === PATCH_CHPOST_GROUP_SUPPORT_V1 ===
        # بسته به اینکه ادمین «اتصال کانال» زده یا «اتصال گروه»، نوعِ مجاز فرق می‌کنه.
        _st = await state.get_data()
        _kind = _st.get("chpost_conn_kind", "channel")
        if _kind == "group":
            if chat.type not in ("group", "supergroup"):
                await msg.answer("❌ این یک گروه نیست. لطفاً گروه را فوروارد/معرفی کن.")
                return
        elif chat.type != "channel":
            await msg.answer("❌ این یک کانال نیست. لطفاً کانال را فوروارد/معرفی کن.")
            return
        member = await bot.get_chat_member(chat.id, bot.id)
        if member.status != "administrator":
            await msg.answer(
                "❌ ربات اینجا ادمین نیست. اول ربات را با دسترسیِ «ارسال پیام» "
                "ادمین کن، بعد دوباره امتحان کن.")
            return
    except (TelegramBadRequest, TelegramForbiddenError):
        await msg.answer(
            "❌ به این کانال دسترسی ندارم. مطمئن شو یوزرنیم/آیدی درست است و ربات آنجا ادمین است.")
        return
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await msg.answer("❌ خطایی پیش آمد، دوباره امتحان کن.")
        return

    # === PATCH_CHPOST_GROUP_SUPPORT_V1 ===
    _pref = "chpost_group" if _kind == "group" else "chpost_chat"
    await set_pset(f"{_pref}_id", str(chat.id))
    await set_pset(f"{_pref}_title", chat.title or chat.username or str(chat.id))
    _word = "گروه" if _kind == "group" else "کانال"
    await state.clear()
    await msg.answer(f"✅ {_word} «{chat.title or chat.username}» وصل شد.",
                     reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
    txt, kb = await _chpost_menu()
    await msg.answer(txt, reply_markup=kb)


async def _chpost_home_pick_kb(selected_keys: list[str]) -> InlineKeyboardMarkup:
    """PATCH_PLATFORM_CHPOST_HOME_BUTTONS_V1 — لیستِ زنده‌ی همهٔ دکمه‌های *فعالِ* صفحهٔ اصلی (سیستمی + دلخواه)؛
    از رویِ _HOME_CFG/_HOME_CUSTOM ساخته می‌شود، پس هر دکمه‌ای که بعداً از تنظیماتِ
    صفحهٔ اصلی اضافه/فعال بشه، خودکار همین‌جا هم ظاهر می‌شه — نیازی به پچِ جدید نیست."""
    await ensure_home_loaded()
    rows: list[list[InlineKeyboardButton]] = []
    for k in HOME_DEFAULT_LABEL:
        c = _pb_home_cfg().get(k) or {}
        if not c.get("enabled", True):
            continue
        label = (c.get("label") or HOME_DEFAULT_LABEL[k])
        mark = "☑️" if k in selected_keys else "⬜"
        rows.append([hx_button(f"{mark} {label}", callback_data=f"cph:pickbtn:{k}")])
    for b in _pb_home_custom():
        if not b.get("enabled", True):
            continue
        key = f"custom:{b.get('id')}"
        label = (b.get("label") or "دکمه")
        mark = "☑️" if key in selected_keys else "⬜"
        rows.append([hx_button(f"{mark} {label}", callback_data=f"cph:pickbtn:{key}")])
    n = len(selected_keys)
    rows.append([hx_button((f"✅ بازگشت به پیش‌نمایش ({n} دکمه انتخاب‌شده)" if n else "✅ بازگشت به پیش‌نمایش"),
        callback_data="cph:pickdone")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _chpost_home_link_buttons(bot: Bot, selected_keys: list[str],
                                     for_channel: bool = False) -> list[list[InlineKeyboardButton]]:
    """PATCH_PLATFORM_CHPOST_HOME_BUTTONS_V1 — دکمه‌های صفحهٔ اصلی، دکمه‌های کیبوردِ *پایین*ِ ربات‌اند (نه
    اینلاین) و پستِ کانال فقط اینلاین قبول می‌کند؛ برای همین هر کدام به یک
    دکمهٔ لینکِ اینلاین با دیپ‌لینکِ start=hcb-<key> تبدیل می‌شود — با تپ روی
    آن در کانال، کاربر وارد چتِ خصوصیِ ربات می‌شود و همان عمل بلافاصله اجرا
    می‌شود (حتی اگر قبلاً /start نزده باشد).

    === PATCH_PLATFORM_CHPOST_BUTTON_COLOR_EMOJI_V1 ===
    قبلاً این‌جا یه دکمهٔ کاملاً خام (بدونِ رنگ، بدونِ ایموجیِ پرمیوم) ساخته
    می‌شد — دقیقاً همون باگی که تویِ ربات‌هایِ مستأجر هم بود و درست شد. الان
    از همون رنگ/ایموجی‌ای که خودِ دکمه تویِ منویِ اصلیِ بات‌ساز داره استفاده
    می‌کنه (چه دکمه‌هایِ سیستمی با استایلِ ثابتشون در HOME_STYLE، چه
    دکمه‌هایِ دلخواهِ ادمین با رنگِ انتخابی‌شون در _HCB_COLOR_STYLE)."""
    if not selected_keys:
        return []
    await ensure_home_loaded()
    me = await bot.get_me()
    rows: list[list[InlineKeyboardButton]] = []
    for key in selected_keys:
        kw = {}
        if key.startswith("custom:"):
            b = _pb_hcb_find(key.split(":", 1)[1])
            if not b or not b.get("enabled", True):
                continue
            label = b.get("label") or "دکمه"
            url_key = "custom-" + key.split(":", 1)[1]
            style = _pb_hcb_color_style_map().get(b.get("color") or "")
            if style:
                kw["style"] = style
            if b.get("icon_emoji_id"):
                # === PATCH_CHPOST_EMOJI_BY_DESTINATION_V1 ===
                label = _emoji_for_destination(kw, label, b["icon_emoji_id"], for_channel)
        else:
            c = _pb_home_cfg().get(key) or {}
            if not c.get("enabled", True):
                continue
            label = c.get("label") or HOME_DEFAULT_LABEL.get(key, key)
            url_key = key
            style = _pb_home_style_map().get(key)
            if style:
                kw["style"] = style
            if c.get("icon_emoji_id"):
                # === PATCH_CHPOST_EMOJI_BY_DESTINATION_V1 ===
                label = _emoji_for_destination(kw, label, c["icon_emoji_id"], for_channel)
        # === PATCH_CHPOST_KEEP_EMOJI_IN_TEXT_V1 ===
        # عمداً از hx_button استفاده *نمی‌کنیم*: اون تابع شکلکِ ابتدایِ متن رو
        # از خودِ متن برمی‌داره و به‌صورتِ فیلدِ جداگانهٔ icon_custom_emoji_id
        # می‌فرسته. تویِ پستِ کانال اون فیلد رندر نمی‌شه، برایِ همین دکمه
        # بدونِ هیچ شکلکی («متن خالی») دیده می‌شد. با InlineKeyboardButton
        # مستقیم، شکلک داخلِ خودِ متن باقی می‌مونه و همیشه نمایش داده می‌شه.
        rows.append([InlineKeyboardButton(
            text=label, url=f"https://t.me/{me.username}?start=hcb-{url_key}", **kw)])
    return rows


# ═══════════ PATCH_CHPOST_USERBOT_V1 — ارسال با اکانتِ پرمیومِ مدیر ═══════════
async def _ua_creds():
    """(api_id, api_hash, session) رمزگشایی‌شده یا None."""
    from wallet_crypto import decrypt_secret
    api_id = (await get_pset("chpost_ua_api_id", "")).strip()
    enc_h = await get_pset("chpost_ua_api_hash", "")
    enc_s = await get_pset("chpost_ua_session", "")
    if not (api_id.isdigit() and enc_h and enc_s):
        return None
    try:
        return int(api_id), decrypt_secret(enc_h), decrypt_secret(enc_s)
    except Exception:
        logger.exception("chpost userbot: رمزگشاییِ نشست ناموفق")
        return None


def _ua_menu_kb(connected: bool) -> InlineKeyboardMarkup:
    rows = [[hx_button("📱 " + ("تعویضِ اکانت (ورود با شماره و کد)" if connected else "اتصال با شماره و کد (پیشنهادی)"),
                       callback_data="chpost:ua_login", style=ButtonStyle.SUCCESS)],
            [hx_button("🔑 اتصال با StringSession (پیشرفته)", callback_data="chpost:ua_connect",
                       style=ButtonStyle.PRIMARY)]]
    if connected:
        rows.append([hx_button("🗑 قطعِ اکانت", callback_data="chpost:ua_off", style=ButtonStyle.DANGER)])
    rows.append([hx_button("⬅️ بازگشت", callback_data="chpost:menu", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_UA_INTRO = (
    "👤 <b>ارسال با اکانتِ پرمیوم</b>\n\n"
    "تلگرام ایموجیِ پرمیوم را فقط از یک اکانتِ پرمیوم می‌پذیرد؛ رباتِ معمولی (حتی با کپی/اینلاین) آن‌ها را ساده می‌کند. "
    "با اتصالِ اکانتِ خودت، پست با <b>همان اکانت</b> در کانال گذاشته می‌شود (در کانال با نامِ کانال دیده می‌شود، نه یوزرنیمِ تو؛ "
    "مگر «Sign messages» روشن باشد) و بعد ربات دکمه‌ها را زیرش می‌چسباند.\n\n"
    "⚠️ <b>امنیت:</b> نشستِ اکانت رمزنگاری‌شده روی سرور می‌ماند و یعنی ربات به آن اکانت دسترسی دارد. "
    "پیشنهاد: یک اکانتِ پرمیومِ جدا که فقط ادمینِ کانال باشد. هر وقت خواستی «قطعِ اکانت» را بزن "
    "(برایِ امنیتِ کامل در تلگرام ← Settings ← Devices آن نشست را هم ببند).\n\n"
    "<b>شرط‌ها:</b> اکانت باید در کانال/گروه ادمین (با حقِ ارسال) باشد؛ ربات هم ادمین با دسترسیِ ویرایشِ پیام تا دکمه بچسباند."
)


@chpost_router.callback_query(F.data == "chpost:ua_menu")
async def chpost_ua_menu(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    creds = await _ua_creds()
    status = "🟢 متصل" if creds else "🔴 متصل نیست"
    txt = _UA_INTRO + f"\n\nوضعیت: <b>{status}</b>"
    try:
        await cb.message.edit_text(txt, reply_markup=_ua_menu_kb(bool(creds)), parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=_ua_menu_kb(bool(creds)), parse_mode="HTML")
    await cb.answer()


@chpost_router.callback_query(F.data == "chpost:ua_connect")
async def chpost_ua_connect(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    import chpost_userbot as _ub
    di, dh = _ub.default_api()
    await state.update_data(ua_flow="session")
    if di:
        await state.update_data(ua_api_id=di, ua_api_hash=dh)
        await state.set_state(ChpostFSM.ua_session)
        txt = ("🔗 <b>اتصالِ اکانت — مرحلهٔ آخر</b>\n\n"
               "رشتهٔ <b>StringSession</b> را بفرست. برایِ ساختنش روی کامپیوترِ خودت:\n"
               "<code>pip install telethon</code>\n<code>python3 tools/make_telethon_session.py</code>\n"
               "(کدِ ورود را در تلگرام فوروارد نکن؛ فقط در همان ترمینال بزن.) پیامت بعد از دریافت پاک می‌شود.")
    else:
        await state.set_state(ChpostFSM.ua_api_id)
        txt = ("🔗 <b>اتصالِ اکانت با StringSession</b>\n\n"
               "از my.telegram.org ← API development tools مقدارِ <b>api_id</b> (یک عدد) را بفرست.")
    await cb.message.answer(txt, reply_markup=await smart_cancel_kb(), parse_mode="HTML")
    await cb.answer()


@chpost_router.message(ChpostFSM.ua_api_id, F.text)
async def chpost_ua_api_id(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    t = (msg.text or "").strip()
    if t == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        return
    if not t.isdigit():
        await msg.answer("api_id یک عدد است. دوباره بفرست:")
        return
    await state.update_data(ua_api_id=t)
    await state.set_state(ChpostFSM.ua_api_hash)
    await msg.answer("مقدارِ <b>api_hash</b> را بفرست (رشتهٔ ۳۲ حرفی).", parse_mode="HTML")


@chpost_router.message(ChpostFSM.ua_api_hash, F.text)
async def chpost_ua_api_hash(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    t = (msg.text or "").strip()
    if t == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        return
    try:
        await msg.delete()
    except Exception:
        logger.debug("chpost ua: حذفِ پیامِ api_hash ممکن نشد", exc_info=True)
    if not (16 <= len(t) <= 64) or " " in t:
        await msg.answer("api_hash معتبر نیست. دوباره بفرست:")
        return
    await state.update_data(ua_api_hash=t)
    if (await state.get_data()).get("ua_flow") == "login":      # ورود با شماره: بعد از api_hash شماره می‌پرسیم
        await state.set_state(ChpostFSM.ua_phone)
        await msg.answer(_UA_ASK_PHONE, parse_mode="HTML")
        return
    await state.set_state(ChpostFSM.ua_session)
    await msg.answer(
        "رشتهٔ <b>StringSession</b> را بفرست.\nبرایِ ساختنش روی کامپیوترِ خودت:\n"
        "<code>pip install telethon</code>\n<code>python3 tools/make_telethon_session.py</code>\n"
        "(کدِ ورود را در تلگرام فوروارد نکن.) پیامت بعد از دریافت پاک می‌شود.", parse_mode="HTML")


@chpost_router.message(ChpostFSM.ua_session, F.text)
async def chpost_ua_session(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    t = (msg.text or "").strip()
    if t == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        return
    try:
        await msg.delete()
    except Exception:
        logger.debug("chpost ua: حذفِ پیامِ نشست ممکن نشد", exc_info=True)
    data = await state.get_data()
    import chpost_userbot as _ub
    try:
        who = await _ub.validate_session(int(data["ua_api_id"]), data["ua_api_hash"], t)
    except _ub.UserbotError as e:
        await msg.answer(f"❌ {e}\nدوباره رشتهٔ نشست را بفرست یا لغو کن.")
        return
    from wallet_crypto import encrypt_secret
    try:
        await set_pset("chpost_ua_api_id", str(data["ua_api_id"]))
        await set_pset("chpost_ua_api_hash", encrypt_secret(data["ua_api_hash"]))
        await set_pset("chpost_ua_session", encrypt_secret(t))
    except Exception:
        logger.exception("chpost ua: ذخیرهٔ نشست ناموفق")
        await msg.answer("⛔️ ذخیرهٔ رمزنگاری‌شده ممکن نشد (WALLET_ENC_KEY در .env تنظیم است؟).")
        return
    await state.clear()
    await msg.answer(f"✅ اکانت متصل شد: <b>{_html.escape(who)}</b>\nحالا هنگامِ ساختِ پست دکمهٔ «👤 ارسال با اکانتِ من» را می‌بینی.",
                     reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True), parse_mode="HTML")
    txt, kb = await _chpost_menu()
    await msg.answer(txt, reply_markup=kb)


_UA_ASK_PHONE = ("📱 <b>شمارهٔ اکانتِ پرمیوم</b> را با کدِ کشور بفرست (مثلاً <code>+989121234567</code>).\n"
                 "کدِ ورود در تلگرامِ همان اکانت می‌آید (چتِ «Telegram»).")


async def _ua_cancel(msg: Message, state: FSMContext):
    import chpost_userbot as _ub
    await _ub.drop_login(msg.from_user.id)
    await state.clear()
    await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))


@chpost_router.callback_query(F.data == "chpost:ua_login")
async def chpost_ua_login(cb: CallbackQuery, state: FSMContext):
    """اتصال با شماره ← کد ← (رمزِ دومرحله‌ای). api_id/api_hash: از env یا از دفعهٔ قبل، وگرنه یک‌بار پرسیده می‌شود."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    import chpost_userbot as _ub
    di, dh = _ub.default_api()
    if not di:
        saved = await _ua_creds_api()
        if saved:
            di, dh = saved
    await state.update_data(ua_flow="login")
    if di:
        await state.update_data(ua_api_id=di, ua_api_hash=dh)
        await state.set_state(ChpostFSM.ua_phone)
        await cb.message.answer(_UA_ASK_PHONE, reply_markup=await smart_cancel_kb(), parse_mode="HTML")
    else:
        await state.set_state(ChpostFSM.ua_api_id)
        await cb.message.answer(
            "📱 <b>اتصال با شماره و کد</b>\n\nیک‌بار از my.telegram.org ← API development tools مقدارِ <b>api_id</b> (عدد) "
            "و بعد <b>api_hash</b> را بگیر (برایِ دفعه‌هایِ بعد ذخیره می‌شود).\n\nاول <b>api_id</b> را بفرست:",
            reply_markup=await smart_cancel_kb(), parse_mode="HTML")
    await cb.answer()


async def _ua_creds_api():
    """api_id/api_hashِ ذخیره‌شده از اتصالِ قبلی (بدونِ نشست) یا None."""
    from wallet_crypto import decrypt_secret
    api_id = (await get_pset("chpost_ua_api_id", "")).strip()
    enc_h = await get_pset("chpost_ua_api_hash", "")
    if not (api_id.isdigit() and enc_h):
        return None
    try:
        return api_id, decrypt_secret(enc_h)
    except Exception:
        logger.exception("chpost ua: رمزگشاییِ api_hash ناموفق")
        return None


@chpost_router.message(ChpostFSM.ua_phone, F.text)
async def chpost_ua_phone(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await _ua_cancel(msg, state)
        return
    import chpost_userbot as _ub
    phone = _ub.parse_phone(msg.text)
    try:
        await msg.delete()
    except Exception:
        logger.debug("chpost ua: حذفِ پیامِ شماره ممکن نشد", exc_info=True)
    if len(phone) < 8:
        await msg.answer("شماره نامعتبر است. با کدِ کشور بفرست (مثلاً +989121234567):")
        return
    data = await state.get_data()
    try:
        login = _ub.make_login(int(data["ua_api_id"]), data["ua_api_hash"])
        await login.send_code(phone)
    except _ub.UserbotError as e:
        await msg.answer(f"❌ {e}")
        return
    await _ub.put_login(msg.from_user.id, login)
    await state.set_state(ChpostFSM.ua_code)
    await msg.answer(
        "✅ کد به تلگرامِ آن اکانت فرستاده شد.\n\n"
        "کد را بفرست <b>با خط یا فاصله بینِ رقم‌ها</b> (مثلاً <code>1-2-3-4-5</code>)؛ اگر کد را عیناً بفرستی تلگرام آن را "
        "«فاش‌شده» حساب می‌کند و ورود را می‌بندد. پیامت بعد از دریافت پاک می‌شود.", parse_mode="HTML")


@chpost_router.message(ChpostFSM.ua_code, F.text)
async def chpost_ua_code(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await _ua_cancel(msg, state)
        return
    import chpost_userbot as _ub
    code = _ub.parse_code(msg.text)
    try:
        await msg.delete()
    except Exception:
        logger.debug("chpost ua: حذفِ پیامِ کد ممکن نشد", exc_info=True)
    login = _ub.get_login(msg.from_user.id)
    if login is None:
        await state.clear()
        await msg.answer("⏳ مهلتِ ورود تمام شد؛ از «اتصالِ اکانت» دوباره شروع کن.")
        return
    if not code:
        await msg.answer("کد فقط رقم است. دوباره بفرست (مثلاً 1-2-3-4-5):")
        return
    try:
        session = await login.sign_in_code(code)
    except _ub.UserbotError as e:
        await msg.answer(f"❌ {e}")
        if "منقضی" in str(e):
            await _ub.drop_login(msg.from_user.id)
            await state.clear()
        return
    if session is None:                       # رمزِ دومرحله‌ای
        await state.set_state(ChpostFSM.ua_pw)
        await msg.answer("🔐 این اکانت رمزِ دومرحله‌ای (Two-Step) دارد؛ رمز را بفرست (پیامت پاک می‌شود).")
        return
    await _ua_finish(msg, state, session)


@chpost_router.message(ChpostFSM.ua_pw, F.text)
async def chpost_ua_pw(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await _ua_cancel(msg, state)
        return
    import chpost_userbot as _ub
    pw = msg.text or ""
    try:
        await msg.delete()
    except Exception:
        logger.debug("chpost ua: حذفِ پیامِ رمز ممکن نشد", exc_info=True)
    login = _ub.get_login(msg.from_user.id)
    if login is None:
        await state.clear()
        await msg.answer("⏳ مهلتِ ورود تمام شد؛ از «اتصالِ اکانت» دوباره شروع کن.")
        return
    try:
        session = await login.sign_in_password(pw)
    except _ub.UserbotError as e:
        await msg.answer(f"❌ {e}")
        return
    await _ua_finish(msg, state, session)


async def _ua_finish(msg: Message, state: FSMContext, session: str):
    """نشستِ ساخته‌شده را امتحان و رمزنگاری‌شده ذخیره می‌کند."""
    import chpost_userbot as _ub
    data = await state.get_data()
    await _ub.drop_login(msg.from_user.id)
    try:
        who = await _ub.validate_session(int(data["ua_api_id"]), data["ua_api_hash"], session)
    except _ub.UserbotError as e:
        await state.clear()
        await msg.answer(f"❌ {e}")
        return
    from wallet_crypto import encrypt_secret
    try:
        await set_pset("chpost_ua_api_id", str(data["ua_api_id"]))
        await set_pset("chpost_ua_api_hash", encrypt_secret(data["ua_api_hash"]))
        await set_pset("chpost_ua_session", encrypt_secret(session))
    except Exception:
        logger.exception("chpost ua: ذخیرهٔ نشست ناموفق")
        await state.clear()
        await msg.answer("⛔️ ذخیرهٔ رمزنگاری‌شده ممکن نشد (WALLET_ENC_KEY در .env تنظیم است؟).")
        return
    await state.clear()
    await msg.answer(f"✅ اکانت متصل شد: <b>{_html.escape(who)}</b>\nحالا هنگامِ ساختِ پست دکمهٔ «👤 ارسال با اکانتِ من» را می‌بینی.",
                     reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True), parse_mode="HTML")
    txt, kb = await _chpost_menu()
    await msg.answer(txt, reply_markup=kb)


@chpost_router.callback_query(F.data == "chpost:ua_off")
async def chpost_ua_off(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    for k in ("chpost_ua_api_id", "chpost_ua_api_hash", "chpost_ua_session"):
        await set_pset(k, "")
    await cb.answer("اکانت قطع شد؛ نشست از سرور پاک شد.", show_alert=True)
    txt, kb = await _chpost_menu()
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)


@chpost_router.callback_query(F.data == "chpost:ua_send")
async def chpost_ua_send(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پست را با اکانتِ مدیر در کانال/گروهِ متصل می‌فرستد و بعد ربات دکمه‌ها را می‌چسباند."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    creds = await _ua_creds()
    if not creds:
        await cb.answer("اول اکانت را وصل کن.", show_alert=True)
        return
    data = await state.get_data()
    plain = data.get("chpost_plain")
    if plain is None:                         # پستی که قبل از این قابلیت ساخته شده
        await cb.answer("متنِ اصلیِ پست ذخیره نشده؛ پست را دوباره بفرست.", show_alert=True)
        return
    photo = data.get("chpost_photo")
    if not photo and not plain.strip():
        await cb.answer("متن پست خالی است.", show_alert=True)
        return
    targets = [(c, w) for c, w in ((await get_pset("chpost_chat_id", ""), "کانال"),
                                   (await get_pset("chpost_group_id", ""), "گروه")) if c]
    if not targets:
        await cb.answer("مقصدی وصل نیست.", show_alert=True)
        return
    await cb.answer("⏳ در حالِ ارسال با اکانتِ تو…")
    buttons = data.get("chpost_buttons") or []
    home_keys = data.get("chpost_home_keys") or []
    kb_rows = list(_chpost_kb(buttons, for_channel=True).inline_keyboard) if buttons else []
    kb_rows += await _chpost_home_link_buttons(bot, home_keys, for_channel=True)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    import os
    import tempfile
    import chpost_userbot as _ub
    photo_path = None
    if photo:
        try:
            fd, photo_path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            await bot.download(photo, destination=photo_path)
        except Exception as e:
            logger.exception("chpost ua: دانلودِ عکس ناموفق")
            await cb.message.answer(f"❌ دریافتِ عکس از تلگرام ناموفق بود: {e}")
            return
    lines = []
    try:
        for cid, word in targets:
            try:
                mid = await _ub.send_post(*creds, cid, plain, data.get("chpost_entities") or [], photo_path)
            except _ub.UserbotError as e:
                lines.append(f"❌ {word}: {e}")
                continue
            note = ""
            if kb is not None:
                try:
                    await bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=kb)
                except Exception as e:
                    logger.warning("chpost ua: چسباندنِ دکمه ناموفق: %s", e)
                    note = " (پست ارسال شد ولی دکمه‌ها نچسبید؛ ربات باید ادمین با دسترسیِ «ویرایشِ پیام‌ها» باشد)"
            lines.append(f"✅ {word}: ارسال شد با اکانتِ تو" + note)
    finally:
        if photo_path:
            try:
                os.unlink(photo_path)
            except OSError:
                pass
    await state.clear()
    await cb.message.answer("\n".join(lines), reply_markup=await smart_home_kb(cb.from_user.id, show_admin=True))
    txt, kbm = await _chpost_menu()
    await cb.message.answer(txt, reply_markup=kbm)


# ═══════════ PATCH_CHPOST_INLINE_V1 — ارسالِ اینلاین ═══════════
# ربات ایموجیِ پرمیوم نمی‌تواند بفرستد؛ ولی پیامی که ادمینِ پرمیوم با @ربات از حالتِ اینلاین می‌فرستد از طرفِ «خودِ او»
# می‌رود و ایموجی‌هایش سالم می‌ماند. پیش‌نویس در DB (bot_id=0) ذخیره می‌شود و با @ربات در هر چت/کانال انتخاب می‌شود.
@chpost_router.callback_query(F.data == "chpost:inline_start")
async def chpost_inline_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(ChpostFSM.content)
    await state.update_data(chpost_text=None, chpost_photo=None, chpost_buttons=[],
                            chpost_home_keys=[], chpost_mode="inline",
                            chpost_target_chat_id=None, chpost_target_msg_id=None)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("chpost inline: حذفِ کیبورد ممکن نشد", exc_info=True)
    await cb.message.answer(
        "✨ <b>ارسالِ اینلاین</b>\n\nمتنِ پست را بفرست (یا یک عکس با کپشن) — با همان قالب‌بندی و ایموجی‌هایِ پرمیومِ خودت. "
        "بعد می‌توانی دکمه اضافه کنی و در آخر «✨ ارسال اینلاین» را بزنی؛ کانال/گروهِ مقصد را در تلگرام خودت انتخاب می‌کنی "
        "(نیازی به اتصالِ کانال نیست).",
        reply_markup=await smart_cancel_kb(), parse_mode="HTML")
    await cb.answer()


@chpost_router.callback_query(F.data == "chpost:inline")
async def chpost_inline(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    import chpost_inline_core as CI
    data = await state.get_data()
    if not (data.get("chpost_photo") or (data.get("chpost_text") or "").strip()):
        await cb.answer("متن پست خالی است.", show_alert=True)
        return
    try:
        me = await bot.get_me()
        supports = bool(getattr(me, "supports_inline_queries", False))
    except Exception:
        logger.exception("chpost inline: get_me ناموفق بود")
        me, supports = None, True
    if not supports:
        await cb.message.answer(CI.HINT_INLINE_OFF, parse_mode="HTML")
        await cb.answer()
        return
    did = await CI.draft_save(0, cb.from_user.id, data)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📤 انتخابِ کانال/گروه و ارسال",
        switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
            query=f"cp{did}", allow_user_chats=True, allow_bot_chats=False,
            allow_group_chats=True, allow_channel_chats=True),
        style=ButtonStyle.SUCCESS)]])
    await cb.message.answer(CI.ready_text(getattr(me, "username", "") or "", did), reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@chpost_router.inline_query(F.query.regexp(r"^\s*(cp\d+)?\s*$"))
async def chpost_inline_query(q: InlineQuery, bot: Bot):
    """فهرستِ پیش‌نویس‌هایِ مدیرِ اصلی؛ برایِ بقیه نتیجهٔ خالی. فقط «خالی» یا «cp<id>» را می‌گیرد (کوئری «پنل» برایِ سلف‌ساز می‌ماند)."""
    if q.from_user.id != PLATFORM_ADMIN_ID:
        await q.answer([], cache_time=0, is_personal=True)
        return
    import chpost_inline_core as CI
    drafts = await CI.drafts_for(0, q.from_user.id, q.query)

    async def _kb(d):
        buttons, _ids, home_keys = CI.draft_parts(d)
        rows = list(_chpost_kb(buttons, for_channel=True).inline_keyboard) if buttons else []
        rows += await _chpost_home_link_buttons(bot, home_keys, for_channel=True)
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await q.answer(await CI.build_results(drafts, _kb), cache_time=0, is_personal=True)


@chpost_router.callback_query(F.data == "chpost:new")
async def chpost_new(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if not await get_pset("chpost_chat_id", ""):
        await cb.answer("اول یک کانال وصل کن.", show_alert=True)
        return
    await state.set_state(ChpostFSM.content)
    await state.update_data(chpost_text=None, chpost_photo=None, chpost_buttons=[],
                            chpost_home_keys=[], chpost_mode="new",
                            chpost_target_chat_id=None, chpost_target_msg_id=None)  # PATCH_PLATFORM_CHPOST_HOME_BUTTONS_V1
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "📝 <b>محتوای پست</b>\n\nمتنِ پست را بفرست، یا یک عکس با کپشن.\n\n"
        "می‌تونی از قالب‌بندیِ خودِ تلگرام (بولد، ایتالیک، لینک، اسپویلر و...) و ایموجیِ "
        "پرمیوم هم استفاده کنی — همون‌طور که تایپ می‌کنی، همون‌طوری توی کانال دیده می‌شه.\n\n"
        "⚠️ نکته: تلگرام فقط اجازه می‌دهد بات‌هایی که یوزرنیمِ ویژهٔ Fragment خریده‌اند، ایموجیِ "
        "پرمیوم بفرستند؛ برای حفظِ ۱۰۰٪ ظاهرِ ایموجیِ پرمیوم از «📎 افزودنِ دکمه به پستِ موجود» استفاده کن.",
        reply_markup=await smart_cancel_kb(),
    )
    await cb.answer()


# ═══════════ PATCH_CHPOST_ATTACH_EXISTING_V1 ═══════════
# افزودنِ دکمه به پستی که مدیرِ اصلی خودش مستقیم (نه از طریقِ ربات) توی کانال
# گذاشته — چون تلگرام ارسالِ ایموجیِ پرمیوم را فقط به بات‌هایی با یوزرنیمِ
# ویژهٔ Fragment اجازه می‌دهد، اگر پست مستقیم با اکانتِ خودِ ادمین گذاشته شود
# (ایموجیِ پرمیومش سالم می‌ماند) و فقط دکمه از ربات خواسته شود، به این
# محدودیت برنمی‌خوریم — چون edit_message_reply_markup فقط کیبورد را عوض
# می‌کند، نه محتوایِ پیام را.

@chpost_router.callback_query(F.data == "chpost:attach_start")
async def chpost_attach_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if not await get_pset("chpost_chat_id", ""):
        await cb.answer("اول یک کانال وصل کن.", show_alert=True)
        return
    await state.set_state(ChpostFSM.target_msg)
    await state.update_data(chpost_mode="attach", chpost_buttons=[], chpost_home_keys=[],
                            chpost_target_chat_id=None, chpost_target_msg_id=None)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "📎 <b>افزودنِ دکمه به پستِ موجود</b>\n\n"
        "همون پستی که خودت مستقیم توی کانال گذاشتی را برایم <b>فوروارد کن</b>، "
        "یا لینکِ پیام را بفرست (مثلاً <code>https://t.me/mychannel/123</code>).\n\n"
        "⚠️ باید همون کانالِ متصل‌شده باشد؛ متنِ پست دست‌نخورده می‌ماند، فقط دکمه زیرش اضافه می‌شود.",
        reply_markup=await smart_cancel_kb(),
    )
    await cb.answer()


_TME_MSG_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(c/(?P<cid>\d+)|(?P<uname>[A-Za-z][\w]{3,31}))/(?P<mid>\d+)/?$")


# === PATCH_CHPOST_EDIT_ERROR_CLASSIFY_V1 ===
# باگِ گزارش‌شده: مدیر «چسباندنِ دکمه‌ها به همین پست» را می‌زد و پیامِ
# «احتمالاً ربات دیگر در آن کانال ادمین نیست» می‌گرفت، در حالی که ربات
# ادمینِ کانال بود. علتِ واقعی در جزئیاتِ خودِ خطا بود:
#   message is not modified: ... reply markup are exactly the same ...
# یعنی همین دکمه‌ها از قبل زیرِ همون پست چسبیده بودند و تلگرام ادیتِ
# بی‌اثر را رد می‌کند. این اصلاً خطا نیست — نتیجهٔ نهایی همونیه که مدیر
# می‌خواست. قبلاً هر TelegramBadRequest به یک پیامِ ثابت و گمراه‌کننده
# تبدیل می‌شد، پس علتِ واقعی هیچ‌وقت دیده نمی‌شد.
#
# خروجی: (is_noop, پیامِ فارسیِ مناسب). is_noop یعنی «کاری لازم نبود،
# وضعیت از قبل همونیه که خواستی» و فراخوان باید موفق حسابش کند.
def _classify_edit_error(e: Exception, *, attaching: bool) -> tuple[bool, str]:
    detail = str(getattr(e, "message", "") or e)
    low = detail.lower()
    if "message is not modified" in low:
        return True, (
            "ℹ️ دقیقاً همین دکمه‌ها از قبل زیرِ همون پست بودند، پس چیزی برایِ تغییر نبود.\n"
            "پستِ کانال همین الان همون چیزیه که می‌خواستی."
            if attaching else
            "ℹ️ زیرِ اون پست هیچ دکمهٔ شیشه‌ای‌ای نبود که برداشته شود.")
    if "message to edit not found" in low or "message_id_invalid" in low:
        return False, ("❌ اون پیام در کانال پیدا نشد — احتمالاً حذف شده. "
                       "یک‌بار دیگر پستِ فعلی را فوروارد کن یا لینکش را بفرست.")
    if "not enough rights" in low or "forbidden" in low or "chat_admin_required" in low:
        return False, ("❌ ربات اجازهٔ ویرایشِ پست‌های آن کانال را ندارد. در کانال ادمینش کن "
                       "و مطمئن شو دسترسیِ «ویرایشِ پیام‌ها» روشن است.")
    if "message can't be edited" in low:
        return False, ("❌ تلگرام اجازهٔ ویرایشِ این پیام را نمی‌دهد (پیام‌های خیلی قدیمی یا "
                       "بعضی نوع‌های پیام قابلِ ویرایش نیستند).")
    return False, (f"❌ {'چسباندنِ' if attaching else 'حذفِ'} دکمه ناموفق بود.\nجزئیات: {detail}")


# === PATCH_CHPOST_DETACH_EXISTING_V1 ===
async def _resolve_chpost_target(msg: Message, bot: Bot, saved_chat_id: str):
    """فوروارد/لینکِ پستِ کانال را به (chat_id, msg_id) تبدیل می‌کند — منطقِ مشترکِ
    بینِ ویزاردِ «افزودنِ دکمه» و ویزاردِ «حذفِ دکمه‌های شیشه‌ای»."""
    target_chat_id: int | str | None = None
    target_msg_id: int | None = None

    # ۱) فوروارد از خودِ کانال — دقیق‌ترین و مطمئن‌ترین راه
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "type", None) == "channel":
        target_chat_id = fwd_origin.chat.id
        target_msg_id = fwd_origin.message_id
    elif msg.forward_from_chat and msg.forward_from_chat.type == "channel":
        target_chat_id = msg.forward_from_chat.id
        target_msg_id = msg.forward_from_message_id
    else:
        # ۲) لینکِ مستقیمِ پیام (t.me/username/123 یا t.me/c/123456789/123)
        raw = (msg.text or "").strip()
        m = _TME_MSG_LINK_RE.search(raw)
        if m:
            target_msg_id = int(m.group("mid"))
            if m.group("cid"):
                target_chat_id = int(f"-100{m.group('cid')}")
            elif m.group("uname"):
                target_chat_id = f"@{m.group('uname')}"

    if target_chat_id is None or target_msg_id is None:
        return None, None, (
            "❌ این نه فورواردِ یک پستِ کانال بود، نه لینکِ معتبرِ پیام. یکی از پستِ کانال "
            "برایم فوروارد کن یا لینکش را بفرست (مثلاً https://t.me/mychannel/123)، "
            "یا «❌ لغو» را بزن.")

    try:
        chat = await bot.get_chat(target_chat_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None, None, "❌ به این کانال دسترسی ندارم. مطمئن شو لینک/فوروارد درست است."
    if saved_chat_id and str(chat.id) != str(saved_chat_id):
        return None, None, (
            "❌ این پیام مالِ کانالِ دیگری است، نه همون کانالِ متصل‌شده به این بخش.\n"
            "پیام را از همون کانالِ متصل فوروارد کن، یا اول از «✏️ تغییر کانال» کانال را عوض کن.")
    return chat.id, target_msg_id, None


@chpost_router.message(ChpostFSM.target_msg)
async def chpost_target_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        txt, kb = await _chpost_menu()
        await msg.answer(txt, reply_markup=kb)
        return

    saved_chat_id = await get_pset("chpost_chat_id", "")
    target_chat_id, target_msg_id, err = await _resolve_chpost_target(msg, bot, saved_chat_id)
    if err:
        await msg.answer(err)
        return

    await state.update_data(chpost_target_chat_id=target_chat_id, chpost_target_msg_id=target_msg_id)
    await state.set_state(ChpostFSM.attach_idle)
    await msg.answer("👌 پیام پیدا شد. حالا دکمه(ها) را اضافه کن:",
                     reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
    await _chpost_preview(msg, state, bot)


# === PATCH_CHPOST_DETACH_EXISTING_V1 ===
@chpost_router.callback_query(F.data == "chpost:detach_start")
async def chpost_detach_start(cb: CallbackQuery, state: FSMContext):
    """شروعِ ویزاردِ «حذفِ دکمه‌های شیشه‌ای از پستِ موجود» — عکسِ ویزاردِ attach:
    به‌جایِ چسباندن، کیبوردِ زیرِ پستِ فوروارد/لینک‌شده کاملاً برداشته می‌شود."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if not await get_pset("chpost_chat_id", ""):
        await cb.answer("اول یک کانال وصل کن.", show_alert=True)
        return
    await state.set_state(ChpostFSM.detach_target)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🗑 <b>حذفِ دکمه‌های شیشه‌ای از پستِ موجود</b>\n\n"
        "همون پستی که دکمه(ها) زیرش چسبیده را برایم <b>فوروارد کن</b>، یا لینکِ پیام را "
        "بفرست (مثلاً <code>https://t.me/mychannel/123</code>).\n\n"
        "⚠️ باید همون کانالِ متصل‌شده باشد؛ متنِ پست دست‌نخورده می‌ماند، فقط همهٔ دکمه‌های "
        "شیشه‌ایِ زیرش برداشته می‌شود.",
        reply_markup=await smart_cancel_kb(),
    )
    await cb.answer()


@chpost_router.message(ChpostFSM.detach_target)
async def chpost_detach_target_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        txt, kb = await _chpost_menu()
        await msg.answer(txt, reply_markup=kb)
        return

    saved_chat_id = await get_pset("chpost_chat_id", "")
    target_chat_id, target_msg_id, err = await _resolve_chpost_target(msg, bot, saved_chat_id)
    if err:
        await msg.answer(err)
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=target_chat_id, message_id=target_msg_id, reply_markup=None)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # === PATCH_CHPOST_EDIT_ERROR_CLASSIFY_V1 ===
        is_noop, note = _classify_edit_error(e, attaching=False)
        if not is_noop:
            await msg.answer(note)
            await state.clear()
            return
        await state.clear()
        await msg.answer(note, reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        txt, kbm = await _chpost_menu()
        await msg.answer(txt, reply_markup=kbm)
        return
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await msg.answer("❌ خطای غیرمنتظره‌ای پیش آمد.")
        await state.clear()
        return

    await state.clear()
    await msg.answer(
        "✅ همهٔ دکمه‌های شیشه‌ایِ زیرِ همون پست در کانال برداشته شد؛ متنِ پست دست‌نخورده ماند.",
        reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
    txt, kbm = await _chpost_menu()
    await msg.answer(txt, reply_markup=kbm)


@chpost_router.callback_query(F.data == "chpost:attach_apply")
async def chpost_attach_apply(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    data = await state.get_data()
    target_chat_id = data.get("chpost_target_chat_id")
    target_msg_id = data.get("chpost_target_msg_id")
    if not target_chat_id or not target_msg_id:
        await cb.answer("پیامِ هدف مشخص نیست. از اول شروع کن.", show_alert=True)
        return
    buttons = data.get("chpost_buttons") or []
    home_keys = data.get("chpost_home_keys") or []
    kb_rows = list(_chpost_kb(buttons, for_channel=True).inline_keyboard) if buttons else []
    kb_rows += await _chpost_home_link_buttons(bot, home_keys, for_channel=True)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    if kb is None:
        await cb.answer("اول حداقل یک دکمه اضافه کن.", show_alert=True)
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=target_chat_id, message_id=target_msg_id, reply_markup=kb)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # === PATCH_CHPOST_EDIT_ERROR_CLASSIFY_V1 ===
        # «message is not modified» یعنی همین دکمه‌ها از قبل زیرِ پست بودند —
        # موفقیت است، نه خطا؛ پس مثلِ مسیرِ موفق ادامه می‌دهیم.
        is_noop, note = _classify_edit_error(e, attaching=True)
        if not is_noop:
            await cb.message.answer(note)
            await cb.answer()
            return
        await state.clear()
        await cb.message.answer(note, reply_markup=await smart_home_kb(cb.from_user.id, show_admin=True))
        txt, kbm = await _chpost_menu()
        await cb.message.answer(txt, reply_markup=kbm)
        await cb.answer()
        return
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer("❌ خطای غیرمنتظره‌ای پیش آمد.")
        await cb.answer()
        return
    await state.clear()
    await cb.message.answer("✅ دکمه(ها) زیرِ همون پست در کانال چسبانده شد؛ متنِ پست دست‌نخورده ماند.",
                            reply_markup=await smart_home_kb(cb.from_user.id, show_admin=True))
    txt, kbm = await _chpost_menu()
    await cb.message.answer(txt, reply_markup=kbm)
    await cb.answer()


# === PATCH_CHPOST_ATTACH_TEST_SEND_V1 ===
@chpost_router.callback_query(F.data == "chpost:attach_testsend")
async def chpost_attach_test_send(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """کپیِ خودِ پستِ هدف را همراه با دکمه‌هایی که قرار است چسبانده شود، به چتِ خصوصیِ
    ادمین می‌فرستد — دقیقاً همون چیزی که بعد از زدنِ «چسباندن» زیرِ پستِ کانال دیده
    می‌شود، بدون اینکه هنوز چیزی توی کانال تغییر کند."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    data = await state.get_data()
    target_chat_id = data.get("chpost_target_chat_id")
    target_msg_id = data.get("chpost_target_msg_id")
    if not target_chat_id or not target_msg_id:
        await cb.answer("پیامِ هدف مشخص نیست. از اول شروع کن.", show_alert=True)
        return
    buttons = data.get("chpost_buttons") or []
    home_keys = data.get("chpost_home_keys") or []
    kb_rows = list(_chpost_kb(buttons, for_channel=True).inline_keyboard) if buttons else []
    kb_rows += await _chpost_home_link_buttons(bot, home_keys, for_channel=True)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    if kb is None:
        await cb.answer("اول حداقل یک دکمه اضافه کن.", show_alert=True)
        return
    try:
        await bot.copy_message(
            chat_id=cb.from_user.id, from_chat_id=target_chat_id,
            message_id=target_msg_id, reply_markup=kb)
    except Exception as e:
        await cb.message.answer(f"❌ ارسالِ پیش‌نمایش ناموفق بود:\n{e}")
        await cb.answer()
        return
    await cb.answer("📨 پیش‌نمایش برات فرستاده شد")
    await cb.message.answer(
        "📨 پیش‌نمایشِ بالا دقیقاً همونیه که بعد از «چسباندن» زیرِ همون پست در کانال "
        "دیده می‌شه؛ هنوز چیزی توی کانال تغییر نکرده.")


async def _chpost_preview(msg_target: Message, state: FSMContext, bot: Bot):
    """PATCH_PLATFORM_CHPOST_FORMAT_V1 / PATCH_PLATFORM_CHPOST_HOME_BUTTONS_V1 — پیش‌نمایشِ عینِ چیزی که ارسال
    می‌شود؛ متنِ پست دیگر با متنِ راهنما قاطی/بریده نمی‌شود تا وسطِ یک تگِ HTML
    قطع نشود، و دکمه‌های صفحهٔ اصلیِ انتخاب‌شده هم به‌صورتِ زنده به کیبورد اضافه می‌شوند.

    === PATCH_CHPOST_ATTACH_EXISTING_V1 ===
    اگر mode == 'attach' باشد (فقط دکمه زیرِ پستی که خودِ ادمین قبلاً مستقیم در
    کانال گذاشته می‌چسبانیم)، پیش‌نمایشِ متن/عکس نشان داده نمی‌شود و دکمهٔ نهایی
    «✅ ارسال به کانال» به «✅ چسباندنِ دکمه‌ها به همین پست» تغییر می‌کند.
    """
    data = await state.get_data()
    buttons = data.get("chpost_buttons") or []
    home_keys = data.get("chpost_home_keys") or []
    mode = data.get("chpost_mode") or "new"
    kb_rows = []
    if buttons:
        kb = _chpost_kb(buttons, for_channel=True)
        kb_rows = list(kb.inline_keyboard)
    kb_rows += await _chpost_home_link_buttons(bot, home_keys, for_channel=True)
    kb_rows.append([hx_button("➕ افزودن دکمهٔ لینک", callback_data="chpost:addbtn")])
    if buttons:
        kb_rows.append([hx_button("🗑 حذف آخرین دکمهٔ لینک", callback_data="chpost:delbtn")])
    kb_rows.append([hx_button("🏠 دکمه‌های صفحهٔ اصلی", callback_data="chpost:pickhome")])
    if mode == "attach":
        # === PATCH_CHPOST_ATTACH_TEST_SEND_V1 ===
        kb_rows.append([hx_button("📨 پیش‌نمایش/تستِ دکمه‌ها برایِ خودم",
                                             callback_data="chpost:attach_testsend")])
        kb_rows.append([hx_button("✅ چسباندنِ دکمه‌ها به همین پست", callback_data="chpost:attach_apply",
                                             style=ButtonStyle.SUCCESS)])
    else:
        # === PATCH_CHPOST_GROUP_SUPPORT_V1 ===
        _ch = await get_pset("chpost_chat_id", "")
        _gr = await get_pset("chpost_group_id", "")
        if await get_pset("chpost_ua_session", ""):          # PATCH_CHPOST_USERBOT_V1
            kb_rows.append([hx_button("👤 ارسال با اکانتِ من (ایموجیِ پرمیوم سالم)", callback_data="chpost:ua_send",
                                      style=ButtonStyle.SUCCESS)])
        if _ch:
            kb_rows.append([hx_button("✅ ارسال به کانال", callback_data="chpost:send",
                                                 style=ButtonStyle.SUCCESS)])
        if _gr:
            kb_rows.append([hx_button("👥 ارسال به گروه", callback_data="chpost:send:gr",
                                                 style=ButtonStyle.SUCCESS)])
        if _ch and _gr:
            kb_rows.append([hx_button("📢👥 ارسال هم‌زمان به کانال و گروه",
                                                 callback_data="chpost:send:both",
                                                 style=ButtonStyle.PRIMARY)])
        # === PATCH_CHPOST_TEST_SEND_V1 ===
        kb_rows.append([hx_button("📨 ارسالِ آزمایشی برایِ خودم",
                                             callback_data="chpost:testsend")])
    kb_rows.append([hx_button("❌ انصراف", callback_data="chpost:cancel",
                                         style=ButtonStyle.DANGER)])
    preview_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if mode == "attach":
        await msg_target.answer(
            "👀 <b>دکمه‌هایی که زیرِ پستِ موجود چسبانده می‌شود:</b>\n"
            "(متنِ خودِ پست دست‌نخورده می‌ماند؛ فقط همین دکمه‌ها زیرش اضافه می‌شود)",
            reply_markup=preview_kb)
        return
    photo = data.get("chpost_photo")
    text = data.get("chpost_text") or ""
    await msg_target.answer("👀 <b>پیش‌نمایش پست</b>\n\nاین همون چیزیه که توی کانال دیده می‌شه:"
                            if mode != "inline" else "👀 <b>پیش‌نمایش پست</b>\n\nاین همون چیزیه که با ارسالِ اینلاین فرستاده می‌شه:")
    import chpost_inline_core as CI
    if not await CI.copy_post(bot, msg_target.chat.id, data):        # PATCH_CHPOST_COPY_V1: پیش‌نمایشِ عینِ پستِ اصلی (ایموجیِ پرمیوم سالم)
        if photo:
            await msg_target.answer_photo(photo, caption=text or None)
        else:
            await msg_target.answer(text or "—")
    await msg_target.answer("👇 دکمه‌های پست:", reply_markup=preview_kb)


@chpost_router.message(ChpostFSM.content)
async def chpost_content_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        txt, kb = await _chpost_menu()
        await msg.answer(txt, reply_markup=kb)
        return
    if msg.checklist:
        await state.update_data(chpost_text=_checklist_to_text(msg.checklist), chpost_photo=None,
                                chpost_src_chat=None, chpost_src_msg=None,
                                chpost_plain=_checklist_to_text(msg.checklist), chpost_entities=[])
        await msg.answer(
            "ℹ️ تلگرام فعلاً اجازه نمی‌دهد بات‌های معمولی (غیرِ متصل به Business) چک‌لیستِ "
            "تعاملی به کانال بفرستند؛ این یک محدودیتِ خودِ تلگرامه، نه چیزی که از داخل ربات "
            "قابل دور زدن باشد. برای همین متنِ چک‌لیست را به یک پستِ متنیِ معمولی تبدیل کردم.")
    elif msg.photo:
        # PATCH_PLATFORM_CHPOST_FORMAT_V1 — msg.html_text خودش caption + caption_entities را با هم قالب‌بندی می‌کند.
        plain_len = len(msg.caption or "")
        if plain_len > 1024:
            await msg.answer(
                f"❌ کپشنِ عکس {plain_len} کاراکتره؛ تلگرام حداکثر ۱۰۲۴ کاراکتر برای کپشنِ عکس "
                "اجازه می‌ده. کوتاه‌ترش کن و دوباره بفرست:")
            return
        import chpost_userbot as _ub
        await state.update_data(chpost_photo=msg.photo[-1].file_id, chpost_text=msg.html_text,
                                chpost_src_chat=msg.chat.id, chpost_src_msg=msg.message_id,   # PATCH_CHPOST_COPY_V1
                                chpost_plain=msg.caption or "", chpost_entities=_ub.serialize_entities(msg.caption_entities))
    elif msg.text:
        plain_len = len(msg.text or "")
        if plain_len > 4096:
            await msg.answer(
                f"❌ متنِ پست {plain_len} کاراکتره؛ تلگرام حداکثر ۴۰۹۶ کاراکتر برای یک پیامِ متنی "
                "اجازه می‌ده. کوتاه‌ترش کن و دوباره بفرست:")
            return
        import chpost_userbot as _ub
        await state.update_data(chpost_text=msg.html_text, chpost_photo=None,
                                chpost_src_chat=msg.chat.id, chpost_src_msg=msg.message_id,   # PATCH_CHPOST_COPY_V1
                                chpost_plain=msg.text or "", chpost_entities=_ub.serialize_entities(msg.entities))
    else:
        await msg.answer(
            "فقط متن، عکس یا چک‌لیستِ تلگرام پشتیبانی می‌شود (ویدیو/فایل/صدا هنوز نه). "
            "دوباره بفرست:")
        return
    await msg.answer("👌 گرفتم.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
    await _chpost_preview(msg, state, bot)


@chpost_router.callback_query(F.data == "chpost:addbtn")
async def chpost_addbtn(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    data = await state.get_data()
    if len((data.get("chpost_buttons") or [])) >= 6:
        await cb.answer("حداکثر ۶ دکمه.", show_alert=True)
        return
    await state.set_state(ChpostFSM.btn_label)
    await cb.message.answer("📝 متنِ دکمه را بفرست:", reply_markup=await smart_cancel_kb())
    await cb.answer()


@chpost_router.message(ChpostFSM.btn_label)
async def chpost_btn_label_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    _data0 = await state.get_data()
    _idle_state = (ChpostFSM.attach_idle if _data0.get("chpost_mode") == "attach" else ChpostFSM.content)
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.set_state(_idle_state)
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        await _chpost_preview(msg, state, bot)
        return
    label = (msg.text or "").strip()[:64]
    if not label:
        await msg.answer("متنِ دکمه نمی‌تواند خالی باشد. دوباره بفرست:")
        return
    await state.update_data(chpost_btn_label=label)
    await state.set_state(ChpostFSM.btn_url)
    await msg.answer("🔗 حالا لینکِ دکمه را بفرست (باید با http:// یا https:// شروع شود):")


@chpost_router.message(ChpostFSM.btn_url)
async def chpost_btn_url_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    _data0 = await state.get_data()
    _idle_state = (ChpostFSM.attach_idle if _data0.get("chpost_mode") == "attach" else ChpostFSM.content)
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.set_state(_idle_state)
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        await _chpost_preview(msg, state, bot)
        return
    url = (msg.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await msg.answer("❌ لینک باید با http:// یا https:// شروع شود. دوباره بفرست:")
        return
    # === PATCH_CHPOST_BLUE_EMOJI_BTN_V1 ===
    await state.update_data(chpost_btn_url=url)
    await state.set_state(ChpostFSM.btn_emoji)
    await msg.answer(
        "😎 اگه می‌خوای کنارِ متنِ این دکمه یک <b>ایموجی پرمیوم</b> (Custom Emoji) هم نشون داده "
        "بشه، همین الان بفرستش؛ وگرنه بنویس «رد» تا بدونِ ایموجی ادامه بدیم.",
        reply_markup=await smart_cancel_kb())


@chpost_router.message(ChpostFSM.btn_emoji)
async def chpost_btn_emoji_recv(msg: Message, state: FSMContext, bot: Bot):
    """PATCH_CHPOST_BLUE_EMOJI_BTN_V1 — دریافتِ ایموجیِ پرمیومِ اختیاریِ دکمهٔ پست."""
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    _data0 = await state.get_data()
    _idle_state = (ChpostFSM.attach_idle if _data0.get("chpost_mode") == "attach" else ChpostFSM.content)
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.set_state(_idle_state)
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
        await _chpost_preview(msg, state, bot)
        return
    emoji_id = None
    raw = (msg.text or "").strip()
    if raw not in ("رد", "-", "skip"):
        for ent in (msg.entities or []):
            if ent.type == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
                emoji_id = ent.custom_emoji_id
                break
        if not emoji_id:
            await msg.answer(
                "❌ ایموجی پرمیوم پیدا نشد. لطفاً فقط یک ایموجی پرمیوم (Custom Emoji) بفرست، "
                "یا بنویس «رد» تا بدونِ ایموجی ادامه بدیم.")
            return
    data = await state.get_data()
    buttons = list(data.get("chpost_buttons") or [])
    buttons.append([data.get("chpost_btn_label") or "لینک", data.get("chpost_btn_url") or "", emoji_id])
    await state.update_data(chpost_buttons=buttons, chpost_btn_label=None, chpost_btn_url=None)
    await state.set_state(_idle_state)
    await msg.answer("✅ دکمه اضافه شد.", reply_markup=await smart_home_kb(msg.from_user.id, show_admin=True))
    await _chpost_preview(msg, state, bot)


@chpost_router.callback_query(F.data == "chpost:delbtn")
async def chpost_delbtn(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    data = await state.get_data()
    buttons = list(data.get("chpost_buttons") or [])
    if buttons:
        buttons.pop()
    await state.update_data(chpost_buttons=buttons)
    await cb.answer("حذف شد")
    await _chpost_preview(cb.message, state, bot)


@chpost_router.callback_query(F.data == "chpost:pickhome")
async def chpost_pickhome(cb: CallbackQuery, state: FSMContext):
    """{MARKER} — نمایشِ انتخابگرِ زندهٔ دکمه‌های صفحهٔ اصلی."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get("chpost_home_keys") or []
    kb = await _chpost_home_pick_kb(selected)
    txt = ("🏠 <b>دکمه‌های صفحهٔ اصلی</b>\n\nهرکدوم رو می‌خوای زیرِ این پست هم "
           "نمایش داده بشه، بزن تا تیک بخوره (این لیست همیشه زنده‌ست، هر دکمه‌ای "
           "بعداً اضافه/فعال کنی خودش همین‌جا هم میاد):")
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@chpost_router.callback_query(F.data.startswith("cph:pickbtn:"))
async def cph_pick_toggle(cb: CallbackQuery, state: FSMContext):
    """{MARKER} — تیک‌زدن/برداشتنِ یک دکمهٔ صفحهٔ اصلی برایِ پستِ کانال."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    key = cb.data.split(":", 2)[2]
    data = await state.get_data()
    selected = list(data.get("chpost_home_keys") or [])
    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)
    await state.update_data(chpost_home_keys=selected)
    kb = await _chpost_home_pick_kb(selected)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


@chpost_router.callback_query(F.data == "cph:pickdone")
async def cph_pick_done(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """{MARKER} — بازگشت از انتخابگرِ دکمه‌های صفحهٔ اصلی به پیش‌نمایشِ پست."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await cb.answer()
    await _chpost_preview(cb.message, state, bot)


@chpost_router.callback_query(F.data == "chpost:cancel")
async def chpost_cancel(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.clear()
    txt, kb = await _chpost_menu()
    await cb.message.answer("❌ لغو شد.", reply_markup=await smart_home_kb(cb.from_user.id, show_admin=True))
    await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


# === PATCH_CHPOST_TEST_SEND_V1 ===
@chpost_router.callback_query(F.data == "chpost:testsend")
async def chpost_test_send(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ارسالِ آزمایشیِ همین پست به چتِ خصوصیِ خودِ ادمین (به‌جای کانال).

    برایِ عیب‌یابیِ ظاهرِ پست/دکمه‌هاست: دقیقاً همون متن، همون عکس و همون
    کیبوردی که به کانال می‌ره، این‌جا هم فرستاده می‌شه — بدونِ اینکه چیزی
    تویِ کانال منتشر بشه. توجه: ربات‌ها به «پیام‌های ذخیره‌شده»ی تلگرام
    دسترسی ندارن (محدودیتِ خودِ تلگرامه)، برایِ همین به چتِ خصوصیِ خودت با
    همین ربات فرستاده می‌شه که همون کارِ تست رو می‌کنه.
    """
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("chpost_text") or ""
    photo = data.get("chpost_photo")
    buttons = data.get("chpost_buttons") or []
    home_keys = data.get("chpost_home_keys") or []
    kb_rows = list(_chpost_kb(buttons, for_channel=True).inline_keyboard) if buttons else []
    kb_rows += await _chpost_home_link_buttons(bot, home_keys, for_channel=True)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    try:
        import chpost_inline_core as CI
        if await CI.copy_post(bot, cb.from_user.id, data, kb):      # PATCH_CHPOST_COPY_V1
            pass
        elif photo:
            await bot.send_photo(cb.from_user.id, photo, caption=text or None, reply_markup=kb)
        elif text.strip():
            await bot.send_message(cb.from_user.id, text, reply_markup=kb)
        else:
            await cb.answer("متن پست خالی است.", show_alert=True)
            return
    except Exception as e:
        await cb.message.answer(f"❌ ارسالِ آزمایشی ناموفق بود:\n{e}")
        await cb.answer()
        return
    await cb.answer("📨 نسخهٔ آزمایشی برات فرستاده شد")
    await cb.message.answer(
        "📨 نسخهٔ آزمایشی بالا فرستاده شد — دقیقاً همون چیزیه که به کانال می‌ره.\n"
        "اگه این‌جا درست دیده می‌شه ولی تو کانال نه، مشکل از تنظیماتِ کاناله؛ "
        "اگه این‌جا هم مشکل داره، مشکل از خودِ پسته.")


# === PATCH_CHPOST_GROUP_SUPPORT_V1 ===
@chpost_router.callback_query(F.data.in_({"chpost:send", "chpost:send:gr", "chpost:send:both"}))
async def chpost_send(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    # مقصد(ها) بر اساسِ دکمه‌ای که زده شده
    _mode = cb.data.split(":")[2] if cb.data.count(":") > 1 else "ch"
    _ch = await get_pset("chpost_chat_id", "")
    _gr = await get_pset("chpost_group_id", "")
    targets = []
    if _mode in ("ch", "both") and _ch:
        targets.append((_ch, "کانال"))
    if _mode in ("gr", "both") and _gr:
        targets.append((_gr, "گروه"))
    if not targets:
        await cb.answer("مقصدی وصل نیست.", show_alert=True)
        await state.clear()
        return
    chat_id = targets[0][0]
    data = await state.get_data()
    text = data.get("chpost_text") or ""
    photo = data.get("chpost_photo")
    buttons = data.get("chpost_buttons") or []
    home_keys = data.get("chpost_home_keys") or []  # PATCH_PLATFORM_CHPOST_HOME_BUTTONS_V1
    kb_rows = list(_chpost_kb(buttons, for_channel=True).inline_keyboard) if buttons else []
    kb_rows += await _chpost_home_link_buttons(bot, home_keys, for_channel=True)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    if not photo and not text.strip():
        await cb.answer("متن پست خالی است.", show_alert=True)
        return

    # === PATCH_CHPOST_GROUP_SUPPORT_V1 ===
    # به همهٔ مقصدهای انتخاب‌شده می‌فرسته و گزارشِ جداگانه می‌ده — اگه یکی
    # شکست بخوره، بقیه همچنان فرستاده می‌شن.
    ok_list, fail_list = [], []
    import chpost_inline_core as CI
    for _cid, _word in targets:
        try:
            if await CI.copy_post(bot, _cid, data, kb):             # PATCH_CHPOST_COPY_V1: کپیِ پیامِ اصلی ⇒ ایموجیِ پرمیوم سالم
                pass
            elif photo:
                await bot.send_photo(_cid, photo, caption=text or None, reply_markup=kb)
            else:
                await bot.send_message(_cid, text, reply_markup=kb)
            ok_list.append(_word)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            fail_list.append(f"{_word} ({e})")
        except Exception as e:
            logger.exception("chpost send failed for %s", _word)
            fail_list.append(f"{_word} ({e})")

    await state.clear()
    lines = []
    if ok_list:
        lines.append("✅ پست با موفقیت ارسال شد به: " + "، ".join(ok_list))
    for f in fail_list:
        lines.append(f"❌ ارسال ناموفق به {f}")
    if fail_list:
        lines.append("\nمعمولاً یعنی ربات دیگه اونجا ادمین نیست.")
    await cb.message.answer("\n".join(lines),
                            reply_markup=await smart_home_kb(cb.from_user.id, show_admin=True))
    txt, kbm = await _chpost_menu()
    await cb.message.answer(txt, reply_markup=kbm)
    await cb.answer()


