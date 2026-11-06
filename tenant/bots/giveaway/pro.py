"""
tenant/bots/giveaway/pro.py — «🎰 قرعه‌کشیِ حرفه‌ای» (PATCH_GIVEAWAY_PRO_V1).

یک عملکردِ دکمه (gw_pro) که در *هر* رباتِ مستأجر روی یک دکمه گذاشته می‌شود. با زدنش منویِ شش‌دکمه‌ای می‌آید:
  🎁 ساختِ قرعه‌کشیِ جدید (مسیرِ ۹ مرحله‌ایِ گام‌به‌گام با دکمه‌هایِ آماده + «وارد کردنِ دستی»)
  ⚙️ مدیریتِ قرعه‌کشی | 🎰 قرعه‌کشی‌هایِ فعال | 👤 پروفایلِ من | 💬 پشتیبانی | 🌐 Language (فارسی ⇄ English)

از موتورِ موجودِ قرعه‌کشی (engine.py: ثبت‌نام، شانسِ دعوت، قرعهٔ وزنی، پایانِ خودکار، اعلان در گروه/کانال) استفاده می‌کند؛
فقط ستون‌هایِ pro/capacity/min_participants/access/channels/organizer به Giveaway اضافه شده‌اند.

مراحل: ۱ نام ← ۲ مدت ← ۳ ظرفیت ← ۴ تعدادِ برندگان ← ۵ حداقلِ شرکت‌کننده ← ۶ دسترسی ← ۷ کانالِ اجباری ← ۸ رفرال ← ۹ جایزه.
فقط ادمین/مالکِ ربات می‌سازد (مگر مالک «ساختِ عمومی» را روشن کرده باشد: تنظیمِ gwp_public).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from config import Session
from models import Giveaway, GiveawayEntry, GiveawayWinner

from tenant.bots.giveaway import engine as E
from tenant.bots.giveaway.common import back_row, btn, edit_or_send, esc, kb

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

giveaway_pro_router = Router(name="giveaway_pro")

MAX_CHANNELS = 4
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ═══════════════════════════ زبان / متن‌ها ═══════════════════════════

T: dict[str, dict[str, str]] = {
    "fa": {
        "hub_title": "🎰 <b>قرعه‌کشیِ حرفه‌ای</b>",
        "hub_text": "به بخشِ قرعه‌کشی خوش آمدی! 🎉\nاینجا می‌توانی قرعه‌کشی با جایزه‌هایِ مختلف بسازی، در کانال و گروه اعلام کنی و برندگان را خودکار انتخاب کنی.",
        "b_new": "🎁 ساختِ قرعه‌کشیِ جدید", "b_manage": "⚙️ مدیریتِ قرعه‌کشی", "b_active": "🎰 قرعه‌کشی‌هایِ فعال",
        "b_profile": "👤 پروفایلِ من", "b_support": "💬 پشتیبانی", "b_lang": "🌐 Language", "b_home": "🏠 منویِ اصلی",
        "cancel": "❌ لغو", "prev": "⬅️ مرحلهٔ قبل", "manual": "✍️ وارد کردنِ دستی", "unlimited": "♾ بی‌نهایت", "nolimit": "بدونِ حد",
        "back": "⬅️ بازگشت", "close": "✖️ بستن", "yes_post": "✅ بله، پست کن", "no_post": "❌ نه، ممنون",
        "step": "گام {n}/9 — {t}",
        "s1": "نامِ قرعه‌کشی", "s1p": "یک نامِ دلخواه برایِ قرعه‌کشی‌ات انتخاب کن.\nمثال: گیفت، NFT، استارزِ تلگرام، …",
        "s2": "مدتِ زمان", "s2p": "قرعه‌کشی چقدر طول بکشد؟",
        "s2m": "مدت را بفرست: عدد = دقیقه، یا مثلاً <code>2h</code> / <code>۲ ساعت</code> / <code>1d</code> / <code>۱ روز</code>",
        "s3": "ظرفیت", "s3p": "حداکثر چند نفر می‌توانند شرکت کنند؟", "s3m": "ظرفیت را (فقط عدد) بفرست:",
        "s4": "تعدادِ برنده‌ها", "s4p": "چند نفر برنده می‌شوند؟", "s4m": "تعدادِ برنده‌ها را (فقط عدد) بفرست:",
        "s5": "حداقلِ شرکت‌کننده", "s5p": "اگر به این تعداد نرسد، قرعه‌کشی لغو می‌شود.", "s5m": "حداقلِ شرکت‌کننده را (فقط عدد) بفرست:",
        "s6": "محدودیتِ دسترسی", "s6p": "چه کسانی می‌توانند شرکت کنند؟",
        "a_all": "🌍 همهٔ کاربران", "a_premium": "⭐️ فقط کاربرانِ پریمیومِ تلگرام",
        "s7": "شرطِ جوینِ کانال", "s7p": "کاربر باید عضوِ چند کانال باشد؟",
        "h_none": "✅ بدونِ نیاز به کانال", "h_n": "حداقل {n} کانال",
        "s7c": "کانالِ اجباریِ قرعه‌کشی ({i}/{n})",
        "s7cp": "یوزرنیمِ کانال را وارد کن:\nمثال: <code>@Gifty_Land</code>\n\n⚠️ ربات باید <b>ادمینِ</b> کانال باشد.",
        "skip_ch": "❌ ادامه بدونِ کانالِ اجباری",
        "s8": "رفرال‌گیری", "s8p": "می‌خواهی این قرعه‌کشی سیستمِ رفرال (دعوتِ دوستان) داشته باشد؟\n\n"
              "<b>حذفِ رفرال</b> › دعوتِ دوستان شانس اضافه نمی‌کند\n<b>افزودنِ رفرال</b> › هر دعوتِ موفق، یک شانسِ بیشتر",
        "r_off": "🚫 حذفِ رفرال", "r_on": "✅ افزودنِ رفرال",
        "s9": "جایزه", "s9p": "جایزهٔ قرعه‌کشی چیست؟",
        "p_stars": "⭐️ استارزِ تلگرام", "p_premium": "💎 پریمیومِ تلگرام", "p_gift": "🎁 گیفتِ تلگرام",
        "p_stars_t": "استارزِ تلگرام", "p_premium_t": "پریمیومِ تلگرام", "p_gift_t": "گیفتِ تلگرام",
        "s9m": "جایزه را بنویس (مثلاً «۱۰۰ استارز» یا «اشتراکِ ۱ ماههٔ پریمیوم»):",
        "m_ok": "✔", "name_is": "نامِ قرعه‌کشی: {v}",
        "created": "🎉 <b>قرعه‌کشی ساخته شد!</b>", "link_is": "🔗 لینکِ اختصاصیِ قرعه‌کشی:", "share_hint": "لینک را به اشتراک بگذار تا بیشتر شرکت کنند!",
        "ends_in": "پایان: {v} دیگر",
        "post_q": "📣 <b>ارسال در کانال / گروه</b>\n\nمی‌خواهی این قرعه‌کشی را در کانال یا گروهت تبلیغ کنی؟",
        "ask_chat": "🆔 <b>یوزرنیم یا آیدیِ کانال/گروه</b>\n\nیوزرنیم را بفرست:\nمثال: <code>@Gifty_Land</code>\n(ربات باید در آن ادمین باشد)",
        "not_admin_user": "⛔️ <b>شما ادمین یا مالکِ این کانال/گروه نیستید</b>\n\nفقط ادمین‌ها و مالکان می‌توانند بنر را ارسال کنند.",
        "bot_not_admin": "⚠️ ربات در این کانال/گروه ادمین نیست؛ اول ربات را ادمین کن (با اجازهٔ ارسالِ پیام) و دوباره بفرست.",
        "chat_bad": "❌ این کانال/گروه پیدا نشد؛ یوزرنیم را درست بفرست.",
        "posted": "✅ در {t} منتشر شد.", "post_fail": "❌ ارسال نشد: {e}",
        "not_allowed": "⛔️ ساختِ قرعه‌کشی فقط برایِ ادمین‌هایِ این ربات فعال است.",
        "cancelled": "لغو شد.", "wizard_expired": "این مرحله منقضی شده؛ دوباره «ساختِ قرعه‌کشیِ جدید» را بزن.",
        "bad_number": "❌ یک عددِ معتبر بفرست.", "bad_duration": "❌ مدتِ نامعتبر؛ مثل <code>30</code> یا <code>2h</code> بفرست.",
        "name_short": "❌ نام باید ۲ تا ۶۰ نویسه باشد.",
        "ch_bad": "❌ این کانال پیدا نشد یا کانال/سوپرگروهِ عمومی نیست؛ دوباره بفرست.",
        "ch_bot_not_admin": "⚠️ ربات در این کانال ادمین نیست. اول ربات را ادمین کن، بعد دوباره بفرست.",
        "ch_dup": "این کانال را قبلاً اضافه کردی.",
        # کارت
        "c_prize": "جایزه", "c_winners": "برنده‌ها", "c_capacity": "ظرفیت", "c_min": "حداقل", "c_access": "دسترسی",
        "c_org": "برگزارکننده", "c_channels": "کانال‌ها", "c_left": "زمانِ باقی‌مانده", "c_people": "نفر",
        "st_active": "فعال", "st_ended": "پایان‌یافته", "st_cancelled": "لغوشده", "inf": "نامحدود",
        "b_join": "🎰 شرکت در قرعه‌کشی", "b_me": "📊 وضعیتِ من", "b_refs": "👥 لیستِ رفرال", "b_results": "🏆 نتایج",
        "b_share": "📤 ارسالِ لینکِ دعوت", "b_refresh": "🔄 به‌روزرسانی",
        "me_none": "هنوز در این قرعه‌کشی ثبت‌نام نکرده‌ای.",
        "me_in": "✅ ثبت‌نام کرده‌ای\n🎯 شانس‌هایِ تو: {ch}\n👥 دعوت‌هایِ موفق: {inv}",
        "refs_title": "👥 <b>لیستِ رفرال‌هایِ تو</b>", "refs_empty": "هنوز کسی با لینکِ تو ثبت‌نام نکرده.",
        "refs_nolink": "ابتدا در قرعه‌کشی ثبت‌نام کن تا لینکِ دعوتِ اختصاصی بگیری.",
        "no_active": "⏳ <b>در حالِ حاضر قرعه‌کشیِ فعالی نیست.</b>",
        "active_title": "🎰 <b>قرعه‌کشی‌هایِ فعال</b>\n\nکدام را می‌خواهی ببینی؟",
        "g_gone": "این قرعه‌کشی پیدا نشد.",
        # مدیریت
        "mg_title": "⚙️ <b>مدیریتِ قرعه‌کشی</b>", "mg_total": "کلِ قرعه‌کشی‌ها", "mg_active": "فعال", "mg_done": "پایان‌یافته/لغوشده",
        "mg_none": "هنوز قرعه‌کشی‌ای نساخته‌ای.", "mg_manage": "مدیریت: {t}", "mg_what": "چه کاری می‌خواهی انجام بدهی؟",
        "b_endnow": "🏁 پایان و قرعه‌کشیِ همین حالا", "b_cancel": "⛔️ لغوِ قرعه‌کشی", "b_post": "📣 ارسال در کانال/گروه",
        "b_link": "🔗 لینکِ قرعه‌کشی", "b_del": "🗑 حذف", "b_settings": "⚙️ تنظیمات",
        "ended_ok": "🏁 قرعه‌کشی انجام شد.", "cancel_ok": "⛔️ قرعه‌کشی لغو شد.", "del_ok": "🗑 حذف شد.",
        "min_cancel": "قرعه‌کشی لغو شد: شرکت‌کننده ({n} نفر) کمتر از حداقل ({m}) بود.",
        "cfg_title": "⚙️ <b>تنظیماتِ قرعه‌کشی</b>", "cfg_public": "ساختِ قرعه‌کشی برایِ همهٔ کاربران: {v}", "on": "✅ روشن", "off": "⛔️ خاموش",
        # پروفایل
        "pr_title": "👤 <b>پروفایلِ شما</b>", "pr_name": "نام", "pr_id": "آیدی", "pr_status": "وضعیت", "pr_premium": "⭐️ پریمیوم",
        "pr_regular": "عادی", "pr_stats": "آمار", "pr_joins": "کلِ شرکت‌ها", "pr_wins": "برنده‌ها", "pr_rate": "نرخِ برد", "pr_refs": "رفرال‌ها",
        "pr_created": "قرعه‌کشی‌هایِ ساخته‌شده",
        "lang_done": "✅ زبان تغییر کرد.", "support_none": "پشتیبانیِ این ربات هنوز تنظیم نشده است.", "support_txt": "💬 برایِ پشتیبانی روی دکمهٔ زیر بزن:",
        "b_support_open": "💬 ارتباط با پشتیبانی",
    },
    "en": {
        "hub_title": "🎰 <b>Pro Giveaways</b>",
        "hub_text": "Welcome to the giveaway center! 🎉\nCreate giveaways with different prizes, announce them in channels and groups, and let the bot pick winners automatically.",
        "b_new": "🎁 Create new giveaway", "b_manage": "⚙️ Manage giveaways", "b_active": "🎰 Active giveaways",
        "b_profile": "👤 My profile", "b_support": "💬 Support", "b_lang": "🌐 فارسی", "b_home": "🏠 Main menu",
        "cancel": "❌ Cancel", "prev": "⬅️ Previous step", "manual": "✍️ Enter manually", "unlimited": "♾ Unlimited", "nolimit": "No limit",
        "back": "⬅️ Back", "close": "✖️ Close", "yes_post": "✅ Yes, post it", "no_post": "❌ No, thanks",
        "step": "Step {n}/9 — {t}",
        "s1": "Giveaway name", "s1p": "Pick any name for your giveaway.\nExample: Gift, NFT, Telegram Stars, …",
        "s2": "Duration", "s2p": "How long should the giveaway run?",
        "s2m": "Send the duration: a number = minutes, or e.g. <code>2h</code> / <code>1d</code>",
        "s3": "Capacity", "s3p": "Maximum number of participants?", "s3m": "Send the capacity (number only):",
        "s4": "Number of winners", "s4p": "How many winners?", "s4m": "Send the number of winners (number only):",
        "s5": "Minimum participants", "s5p": "If this number isn't reached, the giveaway is cancelled.", "s5m": "Send the minimum participants (number only):",
        "s6": "Access restriction", "s6p": "Who can participate?",
        "a_all": "🌍 All users", "a_premium": "⭐️ Telegram Premium users only",
        "s7": "Channel-join condition", "s7p": "How many channels must users join?",
        "h_none": "✅ No channel required", "h_n": "At least {n} channel(s)",
        "s7c": "Required channel ({i}/{n})",
        "s7cp": "Send the channel username:\nExample: <code>@Gifty_Land</code>\n\n⚠️ The bot must be an <b>admin</b> of the channel.",
        "skip_ch": "❌ Continue without a required channel",
        "s8": "Referrals", "s8p": "Should this giveaway have a referral system (invite friends)?\n\n"
              "<b>Remove referral</b> › invites add no extra chance\n<b>Add referral</b> › each successful invite = one more chance",
        "r_off": "🚫 Remove referral", "r_on": "✅ Add referral",
        "s9": "Prize", "s9p": "What is the prize?",
        "p_stars": "⭐️ Telegram Stars", "p_premium": "💎 Telegram Premium", "p_gift": "🎁 Telegram Gift",
        "p_stars_t": "Telegram Stars", "p_premium_t": "Telegram Premium", "p_gift_t": "Telegram Gift",
        "s9m": "Write the prize (e.g. “100 Stars” or “1-month Premium”):",
        "m_ok": "✔", "name_is": "Giveaway name: {v}",
        "created": "🎉 <b>Giveaway created!</b>", "link_is": "🔗 Private giveaway link:", "share_hint": "Share the link so more people join!",
        "ends_in": "Ends in: {v}",
        "post_q": "📣 <b>Post to channel / group</b>\n\nDo you want to promote this giveaway in your channel or group?",
        "ask_chat": "🆔 <b>Channel/group username or ID</b>\n\nSend the username:\nExample: <code>@Gifty_Land</code>\n(the bot must be an admin there)",
        "not_admin_user": "⛔️ <b>You are not an admin or owner of this channel/group</b>\n\nOnly admins and owners can post the banner.",
        "bot_not_admin": "⚠️ The bot is not an admin there; make it an admin (with permission to post) and send again.",
        "chat_bad": "❌ Channel/group not found; send the correct username.",
        "posted": "✅ Posted to {t}.", "post_fail": "❌ Could not post: {e}",
        "not_allowed": "⛔️ Creating giveaways is enabled for this bot's admins only.",
        "cancelled": "Cancelled.", "wizard_expired": "This step expired; tap “Create new giveaway” again.",
        "bad_number": "❌ Send a valid number.", "bad_duration": "❌ Invalid duration; send e.g. <code>30</code> or <code>2h</code>.",
        "name_short": "❌ The name must be 2–60 characters.",
        "ch_bad": "❌ Channel not found or not a public channel/supergroup; send again.",
        "ch_bot_not_admin": "⚠️ The bot is not an admin of this channel. Make it an admin first, then send again.",
        "ch_dup": "You already added this channel.",
        "c_prize": "Prize", "c_winners": "Winners", "c_capacity": "Capacity", "c_min": "Minimum", "c_access": "Access",
        "c_org": "Organizer", "c_channels": "Channels", "c_left": "Time left", "c_people": "people",
        "st_active": "Active", "st_ended": "Ended", "st_cancelled": "Cancelled", "inf": "Unlimited",
        "b_join": "🎰 Join giveaway", "b_me": "📊 My status", "b_refs": "👥 Referral list", "b_results": "🏆 Results",
        "b_share": "📤 Share invite link", "b_refresh": "🔄 Refresh",
        "me_none": "You haven't joined this giveaway yet.",
        "me_in": "✅ You're in\n🎯 Your chances: {ch}\n👥 Successful invites: {inv}",
        "refs_title": "👥 <b>Your referrals</b>", "refs_empty": "Nobody has joined with your link yet.",
        "refs_nolink": "Join the giveaway first to get your personal invite link.",
        "no_active": "⏳ <b>There is no active giveaway right now.</b>",
        "active_title": "🎰 <b>Active giveaways</b>\n\nWhich one do you want to see?",
        "g_gone": "Giveaway not found.",
        "mg_title": "⚙️ <b>Manage giveaways</b>", "mg_total": "Total giveaways", "mg_active": "Active", "mg_done": "Ended/cancelled",
        "mg_none": "You haven't created any giveaway yet.", "mg_manage": "Manage: {t}", "mg_what": "What would you like to do?",
        "b_endnow": "🏁 End & draw now", "b_cancel": "⛔️ Cancel giveaway", "b_post": "📣 Post to channel/group",
        "b_link": "🔗 Giveaway link", "b_del": "🗑 Delete", "b_settings": "⚙️ Settings",
        "ended_ok": "🏁 The draw is done.", "cancel_ok": "⛔️ Giveaway cancelled.", "del_ok": "🗑 Deleted.",
        "min_cancel": "Giveaway cancelled: participants ({n}) were fewer than the minimum ({m}).",
        "cfg_title": "⚙️ <b>Giveaway settings</b>", "cfg_public": "Let every user create giveaways: {v}", "on": "✅ On", "off": "⛔️ Off",
        "pr_title": "👤 <b>Your profile</b>", "pr_name": "Name", "pr_id": "ID", "pr_status": "Status", "pr_premium": "⭐️ Premium",
        "pr_regular": "Regular", "pr_stats": "Stats", "pr_joins": "Total entries", "pr_wins": "Wins", "pr_rate": "Win rate", "pr_refs": "Referrals",
        "pr_created": "Giveaways created",
        "lang_done": "✅ Language changed.", "support_none": "Support isn't set up for this bot yet.", "support_txt": "💬 Tap the button below for support:",
        "b_support_open": "💬 Contact support",
    },
}


def tr(lang: str, key: str, **kw) -> str:
    s = (T.get(lang) or T["fa"]).get(key) or T["fa"].get(key, key)
    return s.format(**kw) if kw else s


def num(lang: str, n) -> str:
    s = f"{int(n):,}"
    return s.translate(_FA) if lang == "fa" else s


async def get_lang(bot_id: int, uid: int) -> str:
    from tenant import get_setting
    v = await get_setting(bot_id, f"gwp_lang:{uid}", "fa")
    return "en" if v == "en" else "fa"


async def set_lang(bot_id: int, uid: int, lang: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, f"gwp_lang:{uid}", "en" if lang == "en" else "fa")


# ═══════════════════════════ ابزارهایِ خالص (قابلِ تست) ═══════════════════════════

def parse_duration_minutes(text: str | None) -> int | None:
    """«45» ⇒ ۴۵ دقیقه؛ «2h»/«۲ ساعت» ⇒ ۱۲۰؛ «1d»/«۱ روز» ⇒ ۱۴۴۰؛ «1d 12h»؛ بازهٔ ۱ تا ۴۳٬۲۰۰ دقیقه (۳۰ روز)؛ نامعتبر ⇒ None."""
    t = (text or "").strip().lower().translate(_EN)
    t = (t.replace("ساعت", "h").replace("روز", "d").replace("دقیقه", "m").replace("hours", "h").replace("hour", "h")
          .replace("days", "d").replace("day", "d").replace("minutes", "m").replace("minute", "m").replace("min", "m"))
    t = " ".join(t.split())
    if t.isdigit():
        total = int(t)
    else:
        found = list(re.finditer(r"(\d+)\s*([dhm])", t))
        if not found or re.sub(r"(\d+)\s*([dhm])", "", t).strip():
            return None
        mult = {"d": 1440, "h": 60, "m": 1}
        total = sum(int(m.group(1)) * mult[m.group(2)] for m in found)
    return total if 1 <= total <= 43200 else None


def parse_int(text: str | None, lo: int = 0, hi: int = 10_000_000) -> int | None:
    t = (text or "").strip().translate(_EN).replace(",", "").replace("،", "")
    return int(t) if t.isdigit() and lo <= int(t) <= hi else None


def progress_bar(done: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


def duration_label(minutes: int, lang: str = "fa") -> str:
    m = int(minutes)
    if m % 1440 == 0:
        d = m // 1440
        return f"{num(lang, d)} " + ("روز" if lang == "fa" else "day(s)")
    if m % 60 == 0:
        h = m // 60
        return f"{num(lang, h)} " + ("ساعت" if lang == "fa" else "hour(s)")
    return f"{num(lang, m)} " + ("دقیقه" if lang == "fa" else "min")


def left_label(ends_at: datetime | None, lang: str = "fa", now: datetime | None = None) -> str:
    if not ends_at:
        return "—"
    sec = int((ends_at - (now or datetime.utcnow())).total_seconds())
    if sec <= 0:
        return "0"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if lang == "fa":
        parts = [f"{num(lang, d)} روز" if d else "", f"{num(lang, h)} ساعت" if h else "", f"{num(lang, m)} دقیقه" if m else ""]
        out = " و ".join(p for p in parts if p)
        return out or f"{num(lang, s)} ثانیه"
    out = " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "") if p)
    return out or f"{s}s"


def channels_of(g: Giveaway) -> list[dict]:
    try:
        raw = json.loads(g.channels or "[]")
    except ValueError:
        return []
    return [c for c in raw if isinstance(c, dict) and c.get("id")] if isinstance(raw, list) else []


def card_text_pro(g: Giveaway, prizes, participants: int, lang: str = "fa", now: datetime | None = None) -> str:
    """کارتِ حرفه‌ایِ قرعه‌کشی (همان قالبِ اسکرین‌شات‌ها: تیتر قاب‌دار، ردیف‌هایِ مشخصات، نوارِ پیشرفت و زمانِ باقی‌مانده)."""
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    sep = "›" if lang == "fa" else ":"
    prize = esc(prizes[0].title) if prizes else "—"
    if len(prizes) > 1 and len({p.title for p in prizes}) > 1:
        prize = "، ".join(esc(p.title) for p in prizes[:3]) + ("…" if len(prizes) > 3 else "")
    st = {"active": "🟢 " + t("st_active"), "ended": "🏁 " + t("st_ended"), "cancelled": "🔴 " + t("st_cancelled")}.get(g.status, g.status)
    cap = int(g.capacity or 0)
    lines = [f"┐────── <b>{esc(g.title)}</b> ──────┌", "",
             f"🎁 <b>{t('c_prize')}</b> {sep} {prize}",
             f"🏅 <b>{t('c_winners')}</b> {sep} {num(lang, g.winners_count)}",
             f"🚀 <b>{t('c_capacity')}</b> {sep} {num(lang, cap) if cap else t('inf')}"]
    if int(g.min_participants or 0) > 0:
        lines.append(f"⚠️ {t('c_min')}: {num(lang, g.min_participants)} {t('c_people')}")
    lines.append(f"🌍 <b>{t('c_access')}</b> {sep} " + (t("a_premium").replace("⭐️ ", "") if g.access == "premium" else t("a_all").replace("🌍 ", "")))
    if g.organizer:
        lines.append(f"👤 <b>{t('c_org')}</b> {sep} {esc(g.organizer)}")
    chs = channels_of(g)
    if chs:
        lines.append(f"📢 <b>{t('c_channels')}</b> {sep} " + "، ".join("@" + esc(c["u"]) if c.get("u") else esc(str(c["id"])) for c in chs))
    bar_total = cap or int(g.min_participants or 0)
    body = [f"[{progress_bar(participants, bar_total)}] {num(lang, participants)}" + (f"/{num(lang, cap)}" if cap else "")]
    if g.status == "active":
        body.append(f"⏱ {t('c_left')}: {left_label(g.ends_at, lang, now)}")
    lines += ["", "<blockquote>" + "\n".join(body) + "</blockquote>", "", f"┘────── {st} ──────└"]
    return "\n".join(lines)


# ═══════════════════════════ دسترسی‌ها ═══════════════════════════

async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _can_create(row, uid: int) -> bool:
    from tenant import get_setting
    from tenant.bots.giveaway.common import is_admin
    if await is_admin(row, uid):
        return True
    return await get_setting(row.id, "gwp_public", "0") == "1"


async def _is_admin(row, uid: int) -> bool:
    from tenant.bots.giveaway.common import is_admin
    return await is_admin(row, uid)


# ═══════════════════════════ منویِ اصلی ═══════════════════════════

async def hub_content(bot: Bot, row, uid: int, lang: str | None = None):
    lang = lang or await get_lang(row.id, uid)
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    rows = []
    if await _can_create(row, uid):
        rows.append([btn(t("b_new"), "gwp:new", style=ButtonStyle.PRIMARY)])
        rows.append([btn(t("b_manage"), "gwp:mgr", style=ButtonStyle.PRIMARY), btn(t("b_active"), "gwp:act", style=ButtonStyle.PRIMARY)])
    else:
        rows.append([btn(t("b_active"), "gwp:act", style=ButtonStyle.PRIMARY)])
    rows.append([btn(t("b_profile"), "gwp:prof", style=ButtonStyle.PRIMARY)])
    rows.append([btn(t("b_support"), "gwp:sup", style=ButtonStyle.PRIMARY), btn(t("b_lang"), "gwp:lang", style=ButtonStyle.PRIMARY)])
    if await _is_admin(row, uid):
        rows.append([btn(t("b_settings"), "gwp:cfg")])
    return t("hub_title") + "\n\n" + t("hub_text"), kb(*rows)


async def show_hub(event, bot: Bot, row, uid: int) -> None:
    text, markup = await hub_content(bot, row, uid)
    await edit_or_send(event, text, markup)


@giveaway_pro_router.callback_query(F.data == "gwp:home")
async def gwp_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.clear()
    await show_hub(cb, bot, row, cb.from_user.id)
    await cb.answer()


@giveaway_pro_router.callback_query(F.data == "gwp:close")
async def gwp_close(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:      # noqa: BLE001
        logger.debug("gwp close: حذفِ پیام ممکن نشد")
    await cb.answer()


@giveaway_pro_router.callback_query(F.data == "gwp:lang")
async def gwp_lang(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    cur = await get_lang(row.id, cb.from_user.id)
    new = "en" if cur == "fa" else "fa"
    await set_lang(row.id, cb.from_user.id, new)
    await state.clear()
    await show_hub(cb, bot, row, cb.from_user.id)
    await cb.answer(tr(new, "lang_done"))


@giveaway_pro_router.callback_query(F.data == "gwp:sup")
async def gwp_support(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    from tenant import get_setting, normalize_link_url
    lang = await get_lang(row.id, cb.from_user.id)
    raw = (await get_setting(row.id, "support_contact", "")).strip()
    url = normalize_link_url(raw) if raw else None
    if not url:
        return await cb.answer(tr(lang, "support_none"), show_alert=True)
    await edit_or_send(cb, tr(lang, "support_txt"), kb([btn(tr(lang, "b_support_open"), url=url, style=ButtonStyle.SUCCESS)], back_row("gwp:home", tr(lang, "back"))))
    await cb.answer()


# ═══════════════════════════ ساختِ قرعه‌کشی (۹ مرحله) ═══════════════════════════

class GwpFSM(StatesGroup):
    name = State()
    dur = State()
    cap = State()
    win = State()
    minp = State()
    chan = State()
    prize = State()
    post = State()


DUR_CHIPS = [(5, "5m"), (10, "10m"), (30, "30m"), (60, "1h"), (120, "2h"), (1440, "24h")]
CAP_CHIPS = [10, 50, 100, 500, 1000]
WIN_CHIPS = [1, 2, 3, 5, 10]
MIN_CHIPS = [5, 10, 20, 50]


def _chip_label(lang: str, step: str, v: int) -> str:
    if step == "d":
        return duration_label(v, lang)
    if step == "w":
        return f"{num(lang, v)} " + ("برنده" if lang == "fa" else "winner(s)")
    return (f"{num(lang, v)} " + ("نفر" if lang == "fa" else "people")) if step == "c" else num(lang, v)


def _rows(lang: str, step: str, values: list[int], per_row: int, style=ButtonStyle.PRIMARY):
    rows, cur = [], []
    for v in values:
        cur.append(btn(_chip_label(lang, step, v), f"gwp:v:{step}:{v}", style=style))
        if len(cur) == per_row:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    return rows


def _nav(lang: str, prev: bool = True):
    r = [btn(tr(lang, "prev"), "gwp:bk", style=ButtonStyle.DANGER)] if prev else []
    r.append(btn(tr(lang, "cancel"), "gwp:cx", style=ButtonStyle.DANGER))
    return r


async def _wiz(state: FSMContext) -> dict:
    return dict((await state.get_data()).get("gwp") or {})


async def _wiz_set(state: FSMContext, **kw) -> dict:
    d = await _wiz(state)
    d.update(kw)
    await state.update_data(gwp=d)
    return d


def step_view(lang: str, n: int, d: dict) -> tuple[str, object]:
    """متن و کیبوردِ مرحلهٔ n (۱..۹)؛ دکمه‌هایِ آماده + وارد کردنِ دستی + مرحلهٔ قبل/لغو."""
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    head = f"<b>{t('step', n=num(lang, n), t=t('s%d' % n))}</b>"
    recap = ("<blockquote>" + t("name_is", v=esc(d.get("name", ""))) + " " + t("m_ok") + "</blockquote>\n\n") if n > 1 and d.get("name") else ""
    if n == 1:
        return head + "\n<blockquote>" + t("s1p") + "</blockquote>", kb([btn(t("cancel"), "gwp:cx", style=ButtonStyle.DANGER)])
    if n == 2:
        rows = _rows(lang, "d", [m for m, _ in DUR_CHIPS], 3)
        rows.append([btn(t("manual"), "gwp:man:d", style=ButtonStyle.SUCCESS)])
        rows.append(_nav(lang))
        return recap + head + "\n<blockquote>" + t("s2p") + "</blockquote>", kb(*rows)
    if n == 3:
        rows = _rows(lang, "c", CAP_CHIPS, 3)
        rows.append([btn(t("unlimited"), "gwp:v:c:0", style=ButtonStyle.SUCCESS), btn(t("manual"), "gwp:man:c", style=ButtonStyle.SUCCESS)])
        rows.append(_nav(lang))
        return recap + head + "\n<blockquote>" + t("s3p") + "</blockquote>", kb(*rows)
    if n == 4:
        rows = _rows(lang, "w", WIN_CHIPS, 4)
        rows.append([btn(t("manual"), "gwp:man:w", style=ButtonStyle.SUCCESS)])
        rows.append(_nav(lang))
        return recap + head + "\n<blockquote>" + t("s4p") + "</blockquote>", kb(*rows)
    if n == 5:
        rows = [[btn(t("nolimit"), "gwp:v:m:0", style=ButtonStyle.SUCCESS)] + [btn(num(lang, v), f"gwp:v:m:{v}") for v in MIN_CHIPS[:2]],
                [btn(num(lang, v), f"gwp:v:m:{v}") for v in MIN_CHIPS[2:]] + [btn(t("manual"), "gwp:man:m", style=ButtonStyle.SUCCESS)], _nav(lang)]
        return recap + head + "\n<blockquote>" + t("s5p") + "</blockquote>", kb(*rows)
    if n == 6:
        rows = [[btn(t("a_all"), "gwp:v:a:all")], [btn(t("a_premium"), "gwp:v:a:premium")], _nav(lang)]
        return recap + head + "\n<blockquote>" + t("s6p") + "</blockquote>", kb(*rows)
    if n == 7:
        rows = [[btn(t("h_none"), "gwp:v:h:0", style=ButtonStyle.SUCCESS)],
                [btn(t("h_n", n=num(lang, 1)), "gwp:v:h:1"), btn(t("h_n", n=num(lang, 2)), "gwp:v:h:2")],
                [btn(t("h_n", n=num(lang, 3)), "gwp:v:h:3"), btn(t("h_n", n=num(lang, 4)), "gwp:v:h:4")], _nav(lang)]
        return recap + head + "\n<blockquote>" + t("s7p") + "</blockquote>", kb(*rows)
    if n == 8:
        rows = [[btn(t("r_off"), "gwp:v:r:0", style=ButtonStyle.DANGER)], [btn(t("r_on"), "gwp:v:r:1", style=ButtonStyle.SUCCESS)], _nav(lang)]
        return recap + head + "\n<blockquote>" + t("s8p") + "</blockquote>", kb(*rows)
    rows = [[btn(t("p_stars"), "gwp:v:p:stars")], [btn(t("p_premium"), "gwp:v:p:premium")], [btn(t("p_gift"), "gwp:v:p:gift")],
            [btn(t("manual"), "gwp:man:p", style=ButtonStyle.SUCCESS)], _nav(lang)]
    return recap + head + "\n<blockquote>" + t("s9p") + "</blockquote>", kb(*rows)


async def _show_step(event, state: FSMContext, lang: str, n: int) -> None:
    d = await _wiz_set(state, step=n)
    await state.set_state(None)                       # حالتِ ورودیِ متنی فقط وقتی «دستی» خواسته شود
    text, markup = step_view(lang, n, d)
    if n == 1:
        await state.set_state(GwpFSM.name)
    await edit_or_send(event, text, markup)


@giveaway_pro_router.callback_query(F.data == "gwp:new")
async def gwp_new(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    if not await _can_create(row, cb.from_user.id):
        return await cb.answer(tr(lang, "not_allowed"), show_alert=True)
    await state.clear()
    await state.update_data(gwp={"step": 1, "chs": []})
    await _show_step(cb, state, lang, 1)
    await cb.answer()


@giveaway_pro_router.callback_query(F.data == "gwp:cx")
async def gwp_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.clear()
    lang = await get_lang(row.id, cb.from_user.id)
    await show_hub(cb, bot, row, cb.from_user.id)
    await cb.answer(tr(lang, "cancelled"))


@giveaway_pro_router.callback_query(F.data == "gwp:bk")
async def gwp_back(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    d = await _wiz(state)
    lang = await get_lang(row.id, cb.from_user.id)
    n = int(d.get("step") or 1)
    if not d:
        await cb.answer(tr(lang, "wizard_expired"), show_alert=True)
        return
    if n == 7 and d.get("chs"):
        await _wiz_set(state, chs=[], h=0)
    await _show_step(cb, state, lang, max(1, n - 1))
    await cb.answer()


@giveaway_pro_router.message(GwpFSM.name, F.text)
async def gwp_name_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    lang = await get_lang(row.id, msg.from_user.id)
    name = " ".join((msg.text or "").split())
    if not (2 <= len(name) <= 60):
        return await msg.answer(tr(lang, "name_short"))
    await _wiz_set(state, name=name)
    await _show_step(msg, state, lang, 2)


_MANUAL = {"d": (GwpFSM.dur, "s2m"), "c": (GwpFSM.cap, "s3m"), "w": (GwpFSM.win, "s4m"), "m": (GwpFSM.minp, "s5m"), "p": (GwpFSM.prize, "s9m")}


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:man:[dcwmp]$"))
async def gwp_manual(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    d = await _wiz(state)
    if not d:
        return await cb.answer(tr(lang, "wizard_expired"), show_alert=True)
    st, key = _MANUAL[cb.data.split(":")[2]]
    await state.set_state(st)
    await edit_or_send(cb, tr(lang, key), kb(_nav(lang)))
    await cb.answer()


async def _advance_after(event, bot: Bot, state: FSMContext, lang: str, step: str) -> None:
    n = {"d": 3, "c": 4, "w": 5, "m": 6, "a": 7}[step]
    await _show_step(event, state, lang, n)


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:v:[dcwmahrp]:\w+$"))
async def gwp_value(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    d = await _wiz(state)
    if not d.get("name"):
        return await cb.answer(tr(lang, "wizard_expired"), show_alert=True)
    _, _, step, val = cb.data.split(":")
    if step == "d":
        await _wiz_set(state, dur=int(val))
    elif step == "c":
        await _wiz_set(state, cap=int(val))
    elif step == "w":
        await _wiz_set(state, win=max(1, min(int(val), E.MAX_WINNERS)))
    elif step == "m":
        await _wiz_set(state, min=int(val))
    elif step == "a":
        await _wiz_set(state, access="premium" if val == "premium" else "all")
    elif step == "h":
        n = max(0, min(int(val), MAX_CHANNELS))
        await _wiz_set(state, h=n, chs=[])
        if n:
            await _ask_channel(cb, state, lang, 1, n)
            return await cb.answer()
        await _show_step(cb, state, lang, 8)
        return await cb.answer()
    elif step == "r":
        await _wiz_set(state, ref=1 if val == "1" else 0)
        await _show_step(cb, state, lang, 9)
        return await cb.answer()
    elif step == "p":
        title = tr(lang, {"stars": "p_stars_t", "premium": "p_premium_t", "gift": "p_gift_t"}.get(val, "p_stars_t"))
        await _wiz_set(state, prize=title)
        await _finish_wizard(cb, bot, row, state, lang)
        return await cb.answer()
    await _advance_after(cb, bot, state, lang, step)
    await cb.answer()


@giveaway_pro_router.message(GwpFSM.dur, F.text)
async def gwp_dur_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    lang = await get_lang(row.id, msg.from_user.id)
    m = parse_duration_minutes(msg.text)
    if m is None:
        return await msg.answer(tr(lang, "bad_duration"), parse_mode="HTML")
    await _wiz_set(state, dur=m)
    await _show_step(msg, state, lang, 3)


@giveaway_pro_router.message(GwpFSM.cap, F.text)
async def gwp_cap_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    lang = await get_lang(row.id, msg.from_user.id)
    v = parse_int(msg.text, 1, 1_000_000)
    if v is None:
        return await msg.answer(tr(lang, "bad_number"))
    await _wiz_set(state, cap=v)
    await _show_step(msg, state, lang, 4)


@giveaway_pro_router.message(GwpFSM.win, F.text)
async def gwp_win_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    lang = await get_lang(row.id, msg.from_user.id)
    v = parse_int(msg.text, 1, E.MAX_WINNERS)
    if v is None:
        return await msg.answer(tr(lang, "bad_number") + f" (1–{E.MAX_WINNERS})")
    await _wiz_set(state, win=v)
    await _show_step(msg, state, lang, 5)


@giveaway_pro_router.message(GwpFSM.minp, F.text)
async def gwp_min_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    lang = await get_lang(row.id, msg.from_user.id)
    v = parse_int(msg.text, 0, 1_000_000)
    if v is None:
        return await msg.answer(tr(lang, "bad_number"))
    await _wiz_set(state, min=v)
    await _show_step(msg, state, lang, 6)


# ── مرحلهٔ ۷: کانال‌هایِ اجباری ──

async def _ask_channel(event, state: FSMContext, lang: str, i: int, n: int) -> None:
    await _wiz_set(state, step=7, ch_i=i)
    await state.set_state(GwpFSM.chan)
    text = f"<b>{tr(lang, 'step', n=num(lang, 7), t=tr(lang, 's7c', i=num(lang, i), n=num(lang, n)))}</b>\n<blockquote>{tr(lang, 's7cp')}</blockquote>"
    await edit_or_send(event, text, kb([btn(tr(lang, "skip_ch"), "gwp:v:h:0", style=ButtonStyle.DANGER)], _nav(lang)))


async def resolve_channel(bot: Bot, raw: str):
    """«@name» / t.me/name / -100… ⇒ (chat_id, username|None, کانال/سوپرگروه؟) یا None."""
    t = (raw or "").strip()
    t = re.sub(r"^https?://t\.me/", "@", t, flags=re.I)
    t = re.sub(r"^t\.me/", "@", t, flags=re.I)
    if re.fullmatch(r"-100\d{5,}", t):
        ident = int(t)
    elif re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", t):
        ident = t
    else:
        return None
    try:
        ch = await bot.get_chat(ident)
    except Exception:      # noqa: BLE001
        return None
    if getattr(ch, "type", "") not in ("channel", "supergroup", "group"):
        return None
    return ch.id, (ch.username or None), getattr(ch, "title", "") or ""


async def bot_is_admin_in(bot: Bot, chat_id: int) -> bool:
    try:
        me = await bot.get_me()
        m = await bot.get_chat_member(chat_id, me.id)
        return getattr(m, "status", "") in ("administrator", "creator")
    except Exception:      # noqa: BLE001
        return False


@giveaway_pro_router.message(GwpFSM.chan, F.text)
async def gwp_channel_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    lang = await get_lang(row.id, msg.from_user.id)
    d = await _wiz(state)
    res = await resolve_channel(bot, msg.text or "")
    if res is None:
        return await msg.answer(tr(lang, "ch_bad"))
    cid, uname, _title = res
    if not await bot_is_admin_in(bot, cid):
        return await msg.answer(tr(lang, "ch_bot_not_admin"))
    chs = list(d.get("chs") or [])
    if any(c["id"] == cid for c in chs):
        return await msg.answer(tr(lang, "ch_dup"))
    chs.append({"id": cid, "u": uname or ""})
    await _wiz_set(state, chs=chs)
    n, i = int(d.get("h") or 1), len(chs)
    if i < n:
        return await _ask_channel(msg, state, lang, i + 1, n)
    await _show_step(msg, state, lang, 8)


@giveaway_pro_router.message(GwpFSM.prize, F.text)
async def gwp_prize_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    lang = await get_lang(row.id, msg.from_user.id)
    p = " ".join((msg.text or "").split())[:190]
    if len(p) < 2:
        return await msg.answer(tr(lang, "name_short"))
    await _wiz_set(state, prize=p)
    await _finish_wizard(msg, bot, row, state, lang)


async def create_from_wizard(bot: Bot, row, uid: int, d: dict, user=None, now: datetime | None = None) -> Giveaway:
    """ساختِ واقعیِ قرعه‌کشی از دادهٔ ویزارد (خالص از نظرِ UI؛ قابلِ تست)."""
    now = now or datetime.utcnow()
    uname = getattr(user, "username", None) if user is not None else None
    organizer = ("@" + uname) if uname else (getattr(user, "full_name", None) or str(uid))
    data = E.NewGiveaway(
        title=d["name"], winners_count=int(d.get("win") or 1), description="", require_phone=False, captcha=True,
        ref_bonus=1 if d.get("ref") else 0, ref_max=0, hide_winners=False, notify_all=False,
        ends_at=now + timedelta(minutes=int(d.get("dur") or 10)), created_by=uid,
        pro=True, capacity=int(d.get("cap") or 0), min_participants=int(d.get("min") or 0),
        access="premium" if d.get("access") == "premium" else "all",
        channels=json.dumps(d.get("chs") or [], ensure_ascii=False) if d.get("chs") else None, organizer=organizer[:80])
    return await E.create_giveaway(row.id, data, [E.NewPrize(title=str(d.get("prize") or "—")[:190])])


async def _finish_wizard(event, bot: Bot, row, state: FSMContext, lang: str) -> None:
    d = await _wiz(state)
    if not d.get("name") or not d.get("prize"):
        return await edit_or_send(event, tr(lang, "wizard_expired"), kb(back_row("gwp:home", tr(lang, "back"))))
    user = event.from_user
    g = await create_from_wizard(bot, row, user.id, d, user)
    await state.clear()
    from tenant.bots.giveaway.common import deep_link
    link = await deep_link(bot, g.id)
    text = (tr(lang, "created") + "\n\n<blockquote>" + tr(lang, "link_is") + f"\n<code>{esc(link)}</code>\n"
            + tr(lang, "ends_in", v=duration_label(int(d.get("dur") or 10), lang)) + "\n\n🎁 " + tr(lang, "share_hint") + "</blockquote>")
    await edit_or_send(event, text, kb([btn("👁 " + g.title[:30], f"gwp:view:{g.id}", style=ButtonStyle.SUCCESS)],
                                       [btn(tr(lang, "b_post"), f"gwp:post:{g.id}", style=ButtonStyle.PRIMARY)],
                                       back_row("gwp:home", tr(lang, "back"))))


# ═══════════════════════════ کارتِ قرعه‌کشی / شرکت / وضعیت ═══════════════════════════

async def show_card(event, bot: Bot, row, gid: int, uid: int, note: str = "", back: str = "gwp:act") -> None:
    lang = await get_lang(row.id, uid)
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status == "draft":
        return await edit_or_send(event, t("g_gone"), kb(back_row("gwp:home", t("back"))))
    prizes = await E.prizes_of(gid)
    n = await E.count_entries(gid)
    text = card_text_pro(g, prizes, n, lang)
    if note:
        text = note + "\n\n" + text
    rows = []
    entry = await E.entry_of(gid, uid)
    if g.status == "active":
        if entry is None:
            rows.append([btn(t("b_join"), f"gw:j:{gid}", style=ButtonStyle.PRIMARY)])
        rows.append([btn(t("b_me"), f"gwp:me:{gid}", style=ButtonStyle.SUCCESS)])
        if g.ref_bonus > 0:
            rows.append([btn(t("b_refs"), f"gwp:refs:{gid}", style=ButtonStyle.DANGER)])
        rows.append([btn(t("b_refresh"), f"gwp:view:{gid}")])
    elif g.status == "ended":
        rows.append([btn(t("b_results"), f"gw:res:{gid}", style=ButtonStyle.SUCCESS)])
    rows.append(back_row(back, t("back")))
    await edit_or_send(event, text, kb(*rows))


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:view:\d+$"))
async def gwp_view(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.clear()
    await show_card(cb, bot, row, int(cb.data.split(":")[2]), cb.from_user.id)
    await cb.answer()


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:me:\d+$"))
async def gwp_me(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    gid = int(cb.data.split(":")[2])
    entry = await E.entry_of(gid, cb.from_user.id)
    if entry is None:
        return await cb.answer(tr(lang, "me_none"), show_alert=True)
    inv = await E.referrals_count(gid, cb.from_user.id)
    await cb.answer(tr(lang, "me_in", ch=num(lang, E.chances(entry.bonus)), inv=num(lang, inv)), show_alert=True)


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:refs:\d+$"))
async def gwp_refs(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    gid = int(cb.data.split(":")[2])
    entry = await E.entry_of(gid, cb.from_user.id)
    if entry is None:
        return await cb.answer(tr(lang, "refs_nolink"), show_alert=True)
    async with Session() as s:
        invited = list((await s.scalars(select(GiveawayEntry).where(
            GiveawayEntry.giveaway_id == gid, GiveawayEntry.invited_by == cb.from_user.id).order_by(GiveawayEntry.id))).all())
    from tenant.bots.giveaway.common import deep_link
    link = await deep_link(bot, gid, cb.from_user.id)
    lines = [tr(lang, "refs_title"), ""]
    lines += [f"{i}. {esc(e.name or e.code)}" + (f" (@{esc(e.username)})" if e.username else "") for i, e in enumerate(invited[:30], 1)] or [tr(lang, "refs_empty")]
    lines += ["", "🔗 <code>" + esc(link) + "</code>"]
    await edit_or_send(cb, "\n".join(lines), kb(back_row(f"gwp:view:{gid}", tr(lang, "back"))))
    await cb.answer()


@giveaway_pro_router.callback_query(F.data == "gwp:act")
async def gwp_active(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.clear()
    lang = await get_lang(row.id, cb.from_user.id)
    gs = [g for g in await E.active_giveaways(row.id)]
    if not gs:
        await edit_or_send(cb, tr(lang, "no_active"), kb(back_row("gwp:home", tr(lang, "back"))))
        return await cb.answer()
    rows = []
    for g in gs[:20]:
        n = await E.count_entries(g.id)
        rows.append([btn(f"🎰 {g.title[:30]} · {num(lang, n)}", f"gwp:view:{g.id}", style=ButtonStyle.SUCCESS)])
    rows.append(back_row("gwp:home", tr(lang, "back")))
    await edit_or_send(cb, tr(lang, "active_title"), kb(*rows))
    await cb.answer()


# ═══════════════════════════ پروفایل ═══════════════════════════

async def profile_stats(bot_id: int, uid: int) -> dict:
    async with Session() as s:
        joins = int(await s.scalar(select(func.count(GiveawayEntry.id)).where(
            GiveawayEntry.bot_id == bot_id, GiveawayEntry.user_tg == uid)) or 0)
        wins = int(await s.scalar(select(func.count(GiveawayWinner.id)).join(
            GiveawayEntry, GiveawayEntry.id == GiveawayWinner.entry_id).where(
            GiveawayWinner.bot_id == bot_id, GiveawayEntry.user_tg == uid)) or 0)
        refs = int(await s.scalar(select(func.count(GiveawayEntry.id)).where(
            GiveawayEntry.bot_id == bot_id, GiveawayEntry.invited_by == uid)) or 0)
        created = int(await s.scalar(select(func.count(Giveaway.id)).where(
            Giveaway.bot_id == bot_id, Giveaway.created_by == uid)) or 0)
    return {"joins": joins, "wins": wins, "refs": refs, "created": created,
            "rate": (f"{wins * 100 // joins}%" if joins else "—")}


@giveaway_pro_router.callback_query(F.data == "gwp:prof")
async def gwp_profile(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    uid = cb.from_user.id
    lang = await get_lang(row.id, uid)
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    st = await profile_stats(row.id, uid)
    premium = bool(getattr(cb.from_user, "is_premium", False))
    rate = st["rate"].translate(_FA) if lang == "fa" else st["rate"]
    lines = [t("pr_title"), "",
             f"{t('pr_name')}: <b>{esc(cb.from_user.full_name or '')}</b>", f"{t('pr_id')}: <code>{uid}</code>",
             f"{t('pr_status')}: {t('pr_premium') if premium else t('pr_regular')}", "",
             "<blockquote>" + f"{t('pr_stats')}:\n📝 {t('pr_joins')}: {num(lang, st['joins'])}\n🏆 {t('pr_wins')}: {num(lang, st['wins'])}\n"
             f"📈 {t('pr_rate')}: {rate}\n👥 {t('pr_refs')}: {num(lang, st['refs'])}"
             + (f"\n🎁 {t('pr_created')}: {num(lang, st['created'])}" if st["created"] else "") + "</blockquote>"]
    await edit_or_send(cb, "\n".join(lines), kb(back_row("gwp:home", t("back"))))
    await cb.answer()


# ═══════════════════════════ مدیریت ═══════════════════════════

async def _my_giveaways(row, uid: int, admin: bool) -> list[Giveaway]:
    async with Session() as s:
        q = select(Giveaway).where(Giveaway.bot_id == row.id, Giveaway.pro == True)   # noqa: E712
        if not admin:
            q = q.where(Giveaway.created_by == uid)
        return list((await s.scalars(q.order_by(Giveaway.id.desc()).limit(30))).all())


@giveaway_pro_router.callback_query(F.data == "gwp:mgr")
async def gwp_manage(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.clear()
    uid = cb.from_user.id
    lang = await get_lang(row.id, uid)
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    if not await _can_create(row, uid):
        return await cb.answer(t("not_allowed"), show_alert=True)
    gs = await _my_giveaways(row, uid, await _is_admin(row, uid))
    if not gs:
        await edit_or_send(cb, t("mg_title") + "\n\n" + t("mg_none"), kb(back_row("gwp:home", t("back"))))
        return await cb.answer()
    active = sum(1 for g in gs if g.status == "active")
    lines = [t("mg_title"), "", "<blockquote>" + f"{t('mg_total')}: {num(lang, len(gs))}\n🟢 {t('mg_active')}: {num(lang, active)}\n"
             f"🔴 {t('mg_done')}: {num(lang, len(gs) - active)}" + "</blockquote>"]
    rows = []
    for g in gs[:10]:
        n = await E.count_entries(g.id)
        icon = {"active": "🟢", "ended": "🏁", "cancelled": "🔴"}.get(g.status, "•")
        rows.append([btn(f"{icon} {g.title[:28]} · {num(lang, n)}", f"gwp:md:{g.id}", style=ButtonStyle.PRIMARY)])
    rows.append(back_row("gwp:home", t("back")))
    await edit_or_send(cb, "\n".join(lines), kb(*rows))
    await cb.answer()


async def _owns(row, uid: int, g: Giveaway | None) -> bool:
    return bool(g is not None and g.bot_id == row.id and (g.created_by == uid or await _is_admin(row, uid)))


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:md:\d+$"))
async def gwp_manage_detail(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    uid = cb.from_user.id
    lang = await get_lang(row.id, uid)
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    gid = int(cb.data.split(":")[2])
    g = await E.get_giveaway(gid, row.id)
    if not await _owns(row, uid, g):
        return await cb.answer(t("g_gone"), show_alert=True)
    n = await E.count_entries(gid)
    text = card_text_pro(g, await E.prizes_of(gid), n, lang) + "\n\n" + t("mg_what")
    rows = []
    if g.status == "active":
        rows += [[btn(t("b_endnow"), f"gwp:end:{gid}", style=ButtonStyle.SUCCESS)], [btn(t("b_post"), f"gwp:post:{gid}", style=ButtonStyle.PRIMARY)],
                 [btn(t("b_link"), f"gwp:lk:{gid}", style=ButtonStyle.PRIMARY)], [btn(t("b_cancel"), f"gwp:cn:{gid}", style=ButtonStyle.DANGER)]]
    else:
        rows.append([btn(t("b_del"), f"gwp:del:{gid}", style=ButtonStyle.DANGER)])
    rows.append(back_row("gwp:mgr", t("back")))
    await edit_or_send(cb, text, kb(*rows))
    await cb.answer()


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:lk:\d+$"))
async def gwp_link(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    gid = int(cb.data.split(":")[2])
    g = await E.get_giveaway(gid, row.id)
    if not await _owns(row, cb.from_user.id, g):
        return await cb.answer(tr(lang, "g_gone"), show_alert=True)
    from tenant.bots.giveaway.common import deep_link
    await cb.message.answer(tr(lang, "link_is") + f"\n<code>{esc(await deep_link(bot, gid))}</code>", parse_mode="HTML")
    await cb.answer()


@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:(end|cn|del):\d+$"))
async def gwp_actions(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    uid = cb.from_user.id
    lang = await get_lang(row.id, uid)
    _, act, gid_s = cb.data.split(":")
    gid = int(gid_s)
    g = await E.get_giveaway(gid, row.id)
    if not await _owns(row, uid, g):
        return await cb.answer(tr(lang, "g_gone"), show_alert=True)
    from tenant.bots.giveaway import announce as A
    if act == "end":
        res = await A.finish_giveaway(bot, row, gid, notify_admin_chat=uid)
        if not res.ok and res.reason == "min_not_met":
            await cb.message.answer(tr(lang, "min_cancel", n=num(lang, res.participants), m=num(lang, g.min_participants)))
        await cb.answer(tr(lang, "ended_ok") if res.ok else tr(lang, "cancel_ok"), show_alert=True)
    elif act == "cn":
        ok = await E.cancel(gid, row.id)
        if ok:
            g2 = await E.get_giveaway(gid, row.id)
            try:
                await A.refresh_posts(bot, g2, card_text_pro(g2, await E.prizes_of(gid), await E.count_entries(gid), lang), None)
            except Exception:      # noqa: BLE001
                logger.debug("gwp cancel: ادیتِ اعلان‌ها ناموفق بود")
        await cb.answer(tr(lang, "cancel_ok"), show_alert=True)
    else:
        await E.delete_finished(row.id, gid)
        await cb.answer(tr(lang, "del_ok"))
        return await _back_to_manage(cb, bot, row, uid, lang)
    await gwp_manage_detail(cb, bot)


async def _back_to_manage(cb: CallbackQuery, bot: Bot, row, uid: int, lang: str) -> None:
    gs = await _my_giveaways(row, uid, await _is_admin(row, uid))
    t = lambda k, **kw: tr(lang, k, **kw)      # noqa: E731
    rows = [[btn(f"• {g.title[:30]}", f"gwp:md:{g.id}", style=ButtonStyle.PRIMARY)] for g in gs[:10]]
    rows.append(back_row("gwp:home", t("back")))
    await edit_or_send(cb, t("mg_title") + ("" if gs else "\n\n" + t("mg_none")), kb(*rows))


# ═══════════════════════════ ارسال در کانال / گروه ═══════════════════════════

@giveaway_pro_router.callback_query(F.data.regexp(r"^gwp:post:\d+$"))
async def gwp_post_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    gid = int(cb.data.split(":")[2])
    g = await E.get_giveaway(gid, row.id)
    if not await _owns(row, cb.from_user.id, g):
        return await cb.answer(tr(lang, "g_gone"), show_alert=True)
    await state.set_state(GwpFSM.post)
    await state.update_data(gwp_post=gid)
    await edit_or_send(cb, tr(lang, "ask_chat"), kb(back_row(f"gwp:md:{gid}", tr(lang, "cancel"))))
    await cb.answer()


async def user_is_chat_admin(bot: Bot, chat_id: int, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return getattr(m, "status", "") in ("administrator", "creator")
    except Exception:      # noqa: BLE001
        return False


@giveaway_pro_router.message(GwpFSM.post, F.text)
async def gwp_post_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    uid = msg.from_user.id
    lang = await get_lang(row.id, uid)
    gid = (await state.get_data()).get("gwp_post")
    g = await E.get_giveaway(gid, row.id) if gid else None
    if not await _owns(row, uid, g):
        await state.clear()
        return await msg.answer(tr(lang, "g_gone"))
    res = await resolve_channel(bot, msg.text or "")
    if res is None:
        return await msg.answer(tr(lang, "chat_bad"))
    cid, uname, title = res
    if not await user_is_chat_admin(bot, cid, uid):
        return await msg.answer(tr(lang, "not_admin_user"), parse_mode="HTML")
    if not await bot_is_admin_in(bot, cid):
        return await msg.answer(tr(lang, "bot_not_admin"))
    from tenant.bots.giveaway import announce as A
    from models import GiveawayChat
    async with Session() as s:                    # مقصد برایِ اعلانِ نتیجه هم ثبت شود
        ex = await s.scalar(select(GiveawayChat.id).where(GiveawayChat.bot_id == row.id, GiveawayChat.chat_id == cid))
        if ex is None:
            s.add(GiveawayChat(bot_id=row.id, chat_id=cid, title=(title or uname or "")[:120], kind="chat"))
            await s.commit()
    ok, errs = await A.post_announcement(bot, row, g, [cid])
    await state.clear()
    where = ("@" + uname) if uname else (title or str(cid))
    await msg.answer(tr(lang, "posted", t=esc(where)) if ok else tr(lang, "post_fail", e=esc("; ".join(errs)[:150])), parse_mode="HTML",
                     reply_markup=kb([btn("⚙️", f"gwp:md:{gid}")], back_row("gwp:home", tr(lang, "back"))))


# ═══════════════════════════ تنظیمات (مالک) ═══════════════════════════

@giveaway_pro_router.callback_query(F.data.in_({"gwp:cfg", "gwp:cfgp"}))
async def gwp_cfg(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    row = await _row(bot)
    if not row:
        return await cb.answer()
    lang = await get_lang(row.id, cb.from_user.id)
    if not await _is_admin(row, cb.from_user.id):
        return await cb.answer()
    if cb.data == "gwp:cfgp":
        cur = await get_setting(row.id, "gwp_public", "0")
        await set_setting(row.id, "gwp_public", "0" if cur == "1" else "1")
    on = await get_setting(row.id, "gwp_public", "0") == "1"
    await edit_or_send(cb, tr(lang, "cfg_title") + "\n\n" + tr(lang, "cfg_public", v=tr(lang, "on" if on else "off")),
                       kb([btn(tr(lang, "cfg_public", v=tr(lang, "on" if on else "off")), "gwp:cfgp", style=ButtonStyle.PRIMARY)],
                          back_row("gwp:home", tr(lang, "back"))))
    await cb.answer()


# ═══════════════════════════ شرطِ شرکت (ظرفیت / پریمیوم / کانال‌ها) ═══════════════════════════

async def missing_channels(bot: Bot, g: Giveaway, uid: int) -> list[dict]:
    """کانال‌هایِ اجباریِ این قرعه‌کشی که کاربر هنوز عضوشان نیست. خطایِ API ⇒ مانع نمی‌شود."""
    out = []
    for c in channels_of(g):
        try:
            m = await bot.get_chat_member(c["id"], uid)
            if getattr(m, "status", "") in ("left", "kicked"):
                out.append(c)
        except Exception:      # noqa: BLE001
            continue
    return out


async def join_block(ev, bot: Bot, row, g: Giveaway, uid: int) -> str | None:
    """None = مجاز. وگرنه متنِ کوتاهِ دلیل (برایِ say). در حالتِ کانال، خودش پیامِ دکمه‌دار نشان می‌دهد و متنِ کوتاه برمی‌گرداند."""
    lang = await get_lang(row.id, uid)
    if int(g.capacity or 0) > 0 and await E.count_entries(g.id) >= int(g.capacity):
        return {"fa": "ظرفیتِ این قرعه‌کشی تکمیل شده است.", "en": "This giveaway is full."}[lang]
    if g.access == "premium" and not bool(getattr(ev.from_user, "is_premium", False)):
        return {"fa": "این قرعه‌کشی فقط برایِ کاربرانِ پریمیومِ تلگرام است ⭐️", "en": "This giveaway is for Telegram Premium users only ⭐️"}[lang]
    miss = await missing_channels(bot, g, uid)
    if miss:
        rows = []
        for c in miss:
            if c.get("u"):
                rows.append([btn("📢 @" + c["u"], url=f"https://t.me/{c['u']}", style=ButtonStyle.PRIMARY)])
        rows.append([btn({"fa": "✅ عضو شدم، بررسی", "en": "✅ I joined, check"}[lang], f"gw:j:{g.id}", style=ButtonStyle.SUCCESS)])
        rows.append(back_row(f"gw:v:{g.id}", tr(lang, "back")))
        head = {"fa": "📢 <b>برایِ شرکت باید عضوِ کانال(ها) شوی:</b>", "en": "📢 <b>Join the channel(s) to participate:</b>"}[lang]
        try:
            await edit_or_send(ev, head, kb(*rows))
        except Exception:      # noqa: BLE001
            logger.debug("gwp join_block: نمایشِ کانال‌ها ممکن نشد")
        return {"fa": "ابتدا عضوِ کانال(ها) شو.", "en": "Join the channel(s) first."}[lang]
    return None
