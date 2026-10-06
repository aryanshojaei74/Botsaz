"""
referral_gate.py — شرطی‌سازی دکمه‌ها با تعداد زیرمجموعه.

سه حالت روی هر دکمهٔ صفحهٔ اصلی:

  ۱) «جایزهٔ پلکانی» (mode="reward"): کاربر با رسیدن به هر تعداد زیرمجموعه یک
     جایزه (کانفیگ از انبار یا متن ثابت) می‌گیرد. چند سطح قابل تعریف است؛ هر
     سطح یک‌بار و مستقل از بقیه. ادمین می‌تواند یک «لینک دلخواه» هم بگذارد.

  ۲) «گیتِ دسترسی» (mode="access"): عملکرد فعلیِ یک دکمهٔ موجود فقط برای کسانی
     که حداقل N زیرمجموعه دارند باز می‌شود؛ در غیر این صورت پیام نیاز به
     زیرمجموعه + لینک دعوت نشان داده می‌شود. عملکرد اصلی در keep_action حفظ
     می‌شود.

تنظیمات در ``MainButton.config`` (JSON):
  reward: {"mode":"reward","src":"join|real","link":"...","tiers":[
             {"req":3,"rew":"stock","vol":2,"days":30}, {"req":5,"rew":"text"}]}
  access: {"mode":"access","src":"join","req":3,"keep_action":"buy_config","link":"..."}

سازگاری با نسخهٔ قدیمی: configِ بدون mode ولی دارای req/rew/vol/days به‌صورت
جایزهٔ تک‌سطحی تفسیر می‌شود.
"""
import json
import secrets
from urllib.parse import quote

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from config import Session
from models import (
    Bot as BotRow, BotUser, Config, GatedClaim, MainButton,
    ReferralJoin, ReferralLink,
    GateReferralLink, GateReferralJoin,  # === PATCH_GATE_DEDICATED_LINK_V1 ===
    Product,  # === PATCH_GATE_STOCK_CHECK_V1 ===
)

from tenant.bots.vpn.vpn_user import fmt_vol as _fmt_vol  # PATCH_UNLIMITED_LABEL_V1

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

gate_router = Router()


# ═══════════════════════════════════════════
#  پارسر/نرمالایزر تنظیمات
# ═══════════════════════════════════════════

def _parse_config(btn) -> dict | None:
    if not btn or btn.action != "referral_gate" or not btn.config:
        return None
    try:
        data = json.loads(btn.config) or {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_gate(btn) -> dict | None:
    """تنظیمات شرط را نرمال‌شده برمی‌گرداند؛ None اگر ناقص/غیرشرطی باشد."""
    data = _parse_config(btn)
    if data is None:
        return None

    mode = data.get("mode")
    src = data.get("src", "join")

    if mode == "access":
        req = int(data.get("req", 0) or 0)
        if req <= 0:
            return None
        return {"mode": "access", "src": src, "req": req,
                "keep_action": data.get("keep_action"), "link": data.get("link")}

    tiers = data.get("tiers")
    if isinstance(tiers, list) and tiers:
        norm = []
        for t in tiers:
            try:
                req = int(t.get("req", 0) or 0)
            except Exception:
                req = 0
            if req <= 0:
                continue
            norm.append({"req": req, "rew": t.get("rew", "text"),
                         "vol": int(t.get("vol", 0) or 0),
                         "days": int(t.get("days", 0) or 0)})
        if not norm:
            return None
        norm.sort(key=lambda x: x["req"])
        return {"mode": "reward", "src": src, "tiers": norm, "link": data.get("link")}

    # سازگاری قدیمی
    if data.get("req"):
        return {"mode": "reward", "src": src,
                "tiers": [{"req": int(data["req"]), "rew": data.get("rew", "text"),
                           "vol": int(data.get("vol", 0) or 0),
                           "days": int(data.get("days", 0) or 0)}],
                "link": data.get("link")}
    return None


async def count_referrals(bot_id: int, user_id: int, telegram_id: int, src: str) -> int:
    # === PATCH_REFERRAL_POINTS_V1 ===
    if src == "points":
        from tenant.shared.ref_points import get_points
        return await get_points(bot_id, user_id)
    async with Session() as s:
        if src == "real":
            n = await s.scalar(
                select(func.count()).select_from(ReferralJoin).where(
                    ReferralJoin.bot_id == bot_id,
                    ReferralJoin.inviter_user_id == user_id,
                )
            )
            return int(n or 0)
        jc = await s.scalar(
            select(ReferralLink.join_count).where(
                ReferralLink.bot_id == bot_id,
                ReferralLink.user_id == user_id,
            )
        )
        return int(jc or 0)


def _src_label(src: str) -> str:
    if src == "real":
        return "زیرمجموعهٔ واقعی"
    # === PATCH_REFERRAL_POINTS_V1 ===
    if src == "points":
        return "امتیازِ رفرال"
    if src == "gatelink":
        return "دعوت‌شدگانِ لینکِ اختصاصیِ همین دکمه"
    return "دعوت‌شدگان لینک"


def gate_summary(gate: dict) -> str:
    if gate.get("mode") == "access":
        src = gate.get("src", "join")
        lines = ["🔒 نوع: <b>گیتِ دسترسی روی این دکمه</b>",
                 f"🎯 شرط: حداقل <b>{gate['req']}</b> {_src_label(src)}"]
        ka = gate.get("keep_action")
        lines.append(f"⚙️ عملکرد پس از باز شدن: <code>{ka or 'متن/پیام دکمه'}</code>")
        # === PATCH_ACCESS_GATE_CONSUME_V1 / PATCH_GATE_DEDICATED_LINK_V1 ===
        if src == "points":
            # === PATCH_REFERRAL_POINTS_V1 ===
            lines.append(f"🏅 هر استفاده {gate['req']} امتیاز کم می‌کند؛ امتیازِ باقی‌مانده "
                         f"می‌ماند (مثلاً با آستانهٔ {gate['req']}: دو برابرش = دو بار استفاده).")
        elif src == "gatelink":
            lines.append("🔁 هر استفاده صفرش می‌کند؛ برایِ استفادهٔ بعدی باید همون تعداد "
                         "رفیقِ تازه از همین لینکِ اختصاصی بیاورد (سوزاننده، نه پلکانی).")
        else:
            lines.append(f"🔁 هر استفاده رفرال‌هاش مصرف می‌شود؛ برایِ استفادهٔ بعدی باید "
                         f"{gate['req']} نفرِ <b>تازه</b> بیاورد (پلکانیِ فزاینده، نامحدود).")
        return "\n".join(lines)

    lines = ["🎁 نوع: <b>جایزهٔ پلکانیِ زیرمجموعه</b>",
             f"📊 ملاک شمارش: {_src_label(gate.get('src', 'join'))}"]
    for i, t in enumerate(gate.get("tiers", []), 1):
        rw = (f"کانفیگ {t.get('vol', 0)}گیگ/{t.get('days', 0)}روز"
              if t.get("rew") == "stock" else "متن/کانفیگ ثابت")
        lines.append(f"  • سطح {i}: <b>{t['req']}</b> نفر → {rw}")
    return "\n".join(lines)


# ═══════════════════════════════════════════
#  لینک دعوت اختصاصی کاربر + کیبورد
# ═══════════════════════════════════════════

async def _build_user_invite_link(bot: Bot, bot_id: int, u) -> str | None:
    try:
        from tenant.shared.extras import get_or_create_referral, _get_referral_reward
        me = await bot.get_me()
        reward_raw = await _get_referral_reward(bot_id)
        ref = await get_or_create_referral(bot_id, u, me.username, reward_raw)
        if ref:
            return f"https://t.me/{me.username}?start=ref_{ref.code}"
    except Exception:
        return None
    return None


def _invite_keyboard(link: str | None, admin_link: str | None,
                     back_cb: str = "mbtn:home") -> InlineKeyboardMarkup:
    rows = []
    if link:
        invite_text = "سلام! با این لینک وارد شو 👇🎁"
        share_url = (f"https://t.me/share/url?url={quote(link)}"
                     f"&text={quote(invite_text)}")
        rows.append([InlineKeyboardButton(text="📤 ارسال لینک به دوستان", url=share_url)])
    if admin_link:
        rows.append([InlineKeyboardButton(text="🔗 لینک دعوت", url=admin_link)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_need_more(msg: Message, bot: Bot, bot_id: int, u,
                          have: int, req: int, src: str,
                          admin_link: str | None, cb=None,
                          already_used_note: str | None = None) -> None:
    alert_text = (
        f"⚠️ برای استفاده از این بخش باید حداقل {req} {_src_label(src)} داشته باشی.\n"
        f"👤 تعداد فعلی تو: {have} از {req} نفر است."
    )
    if already_used_note:
        alert_text += f"\n{already_used_note}"
    if cb is not None:
        try:
            await cb.answer(alert_text, show_alert=True)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    link = await _build_user_invite_link(bot, bot_id, u)
    body = alert_text
    if link:
        body += ("\n\n🔗 لینک دعوت اختصاصی تو (برای کپی، روی آن لمس کن):\n"
                 f"<code>{link}</code>\n\n"
                 "این لینک را برای دوستانت بفرست؛ هر کسی از طریق آن وارد شود "
                 "به تعداد زیرمجموعه‌هایت اضافه می‌شود.")
    await msg.answer(body, reply_markup=_invite_keyboard(link, admin_link))


# ═══════════════════════════════════════════
#  تحویل (کلیک کاربر)
# ═══════════════════════════════════════════

async def deliver_gate(btn, msg: Message, bot: Bot, row, u, bot_id: int,
                       state=None, cb=None) -> None:
    from tenant import inline_back_kb
    gate = load_gate(btn)
    if not gate:
        if cb is not None:
            await cb.answer("این دکمه هنوز کامل تنظیم نشده است.", show_alert=True)
        else:
            await msg.answer("این دکمه هنوز کامل تنظیم نشده است.",
                             reply_markup=await inline_back_kb(bot_id))
        return
    if gate["mode"] == "access":
        await _deliver_access(gate, btn, msg, bot, row, u, bot_id, state, cb)
    else:
        await _deliver_reward(gate, btn, msg, bot, row, u, bot_id, cb)


async def _access_used_count(button_id: int, user_id: int) -> int:
    """چند بار این کاربر تا الان از این دکمهٔ گیت‌شده با موفقیت استفاده کرده.

    === PATCH_GATE_ACCESS_TIER_COLLISION_V1 ===
    باگِ جدی: قبلاً اینجا هر ردیفِ GatedClaim با همین (button_id, user_id)
    شمرده می‌شد — بدونِ توجه به این‌که اون ردیف مالِ «استفادهٔ گیتِ دسترسی»
    بوده یا مالِ «کلایمِ یه سطح از جایزهٔ پلکانی» (که هر دو از همون جدول و
    همون UniqueConstraint رویِ (button_id, user_id, tier) استفاده می‌کنن).
    اگه یه دکمه یه‌بار حالتِ «جایزهٔ پلکانی» بوده (کاربر سطحِ ۱ رو گرفته،
    یعنی tier=1 ثبت شده) و بعداً ادمین همون دکمه رو به «گیتِ دسترسی» عوض
    کرده باشه، اولین تلاشِ کاربر برایِ استفاده از گیت (که هم tier=1 امتحان
    می‌کرد) با همون ردیفِ قدیمیِ جایزه تداخل می‌کرد — یعنی حتی با رفرالِ
    کافی، مصرف هیچ‌وقت با موفقیت ثبت نمی‌شد. الان استفاده‌هایِ گیتِ دسترسی
    با tierِ **منفی** ذخیره می‌شن (سطوحِ جایزهٔ پلکانی همیشه ≥۱ هستن، پس
    برخوردی ممکن نیست) — یعنی این دو مکانیزم دیگه هیچ‌وقت با هم تداخل
    نمی‌کنن، حتی اگه دکمه قبلاً حالتش عوض شده باشه."""
    async with Session() as s:
        n = await s.scalar(
            select(func.count()).select_from(GatedClaim).where(
                GatedClaim.button_id == button_id, GatedClaim.user_id == user_id,
                GatedClaim.tier < 0))
    return int(n or 0)


async def _record_access_use(bot_id: int, button_id: int, user_id: int, use_no: int) -> bool:
    """ثبتِ اتمیکِ «استفادهٔ شمارهٔ use_no» — با تکیه بر همان قیدِ یکتای
    (button_id, user_id, tier) که برایِ جایزهٔ پلکانی هم استفاده می‌شود؛ اگر
    هم‌زمان (مثلاً دو تپِ سریع) کسی همین شماره را قبلاً ثبت کرده باشد، False
    برمی‌گردد تا از مصرفِ دوبارهٔ همان رفرال‌ها جلوگیری شود.

    === PATCH_GATE_ACCESS_TIER_COLLISION_V1 ===
    tier با علامتِ منفی ذخیره می‌شود (نگاه کن به توضیحِ بالا در
    _access_used_count) تا هیچ‌وقت با tierهایِ مثبتِ جایزهٔ پلکانی تداخل نکند."""
    async with Session() as s:
        s.add(GatedClaim(bot_id=bot_id, button_id=button_id, user_id=user_id,
                         tier=-use_no, delivered=None))
        try:
            await s.flush()
            await s.commit()
            return True
        except IntegrityError:
            await s.rollback()
            return False


async def _deliver_access(gate, btn, msg, bot, row, u, bot_id, state, cb) -> None:
    from tenant import _run_button_action
    req = int(gate["req"])
    src = gate["src"]

    # === PATCH_GATE_DEDICATED_LINK_V1 ===
    # src == "gatelink" یعنی این دکمه به‌جایِ رفرالِ عمومی/زیرمجموعهٔ واقعی،
    # از یک لینکِ دعوتِ کاملاً اختصاصیِ همین دکمه استفاده می‌کنه که مسیرش
    # جداست (پایین‌تر، _deliver_access_gatelink).
    if src == "gatelink":
        await _deliver_access_gatelink(gate, btn, msg, bot, row, u, bot_id, state, cb, req)
        return

    # === PATCH_REFERRAL_POINTS_V1 ===
    if src == "points":
        await _deliver_access_points(gate, btn, msg, bot, row, u, bot_id, state, cb, req)
        return

    have = await count_referrals(bot_id, u.id, msg.from_user.id, src)

    # === PATCH_ACCESS_GATE_CONSUME_V1 ===
    # قبلاً همین‌که کاربر یک‌بار به آستانهٔ req می‌رسید، برایِ همیشه (نامحدود)
    # می‌توانست از عملکردِ زیرِ گیت (مثلاً «تست رایگان») استفاده کند — چون
    # هیچ‌جا مصرفِ رفرال‌ها ثبت نمی‌شد. حالا هر «استفاده» شمرده می‌شود و
    # استفادهٔ بعدی به یک دستهٔ کاملاً تازهٔ req نفره نیاز دارد (پلکانیِ
    # فزاینده: استفادهٔ ۱اُم ≥ req، ۲اُم ≥ ۲×req، ۳اُم ≥ ۳×req و...) — دقیقاً
    # همان چیزی که قبلاً با همین رفرال‌های قبلی می‌شد نامحدود استفاده کرد.
    used_count = await _access_used_count(btn.id, u.id)
    next_use_no = used_count + 1
    need = req * next_use_no
    if have < need:
        note = None
        if used_count > 0:
            note = (f"🔁 قبلاً {used_count} بار از این بخش استفاده کرده‌ای؛ برایِ استفادهٔ بعدی "
                    f"باید {req} نفرِ <b>تازه</b> (غیر از قبلی‌ها) بیاوری — یعنی مجموعاً {need} نفر.")
        await _send_need_more(msg, bot, bot_id, u, have, need, src,
                              None, cb=cb, already_used_note=note)
        return
    if not await _record_access_use(bot_id, btn.id, u.id, next_use_no):
        # هم‌زمانی: یکی دیگه (یا همین کاربر با دو تپِ سریع) همین شماره‌استفاده
        # رو زودتر ثبت کرد — به‌جایِ اجرایِ دوبارهٔ عملکرد، دوباره همین تابع
        # رو صدا می‌زنیم تا با شمارشِ به‌روزشده، وضعیتِ درست (اجازه/نیاز به بیشتر) تعیین شود.
        await _deliver_access(gate, btn, msg, bot, row, u, bot_id, state, cb)
        return
    if cb is not None:
        try:
            await cb.answer()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _run_button_action(gate.get("keep_action"), btn, msg, bot, state,
                             row, u, bot_id, cb=cb)


# === PATCH_GATE_DEDICATED_LINK_V1 ===

async def _get_or_create_gate_link(bot_id: int, button_id: int, u: BotUser) -> GateReferralLink:
    """لینکِ دعوتِ اختصاصیِ (button, user) رو برمی‌گردونه؛ اگه نبود می‌سازه."""
    async with Session() as s:
        link = await s.scalar(select(GateReferralLink).where(
            GateReferralLink.button_id == button_id, GateReferralLink.user_id == u.id))
        if link:
            return link
        code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        link = GateReferralLink(bot_id=bot_id, button_id=button_id, user_id=u.id,
                                code=code, join_count=0)
        s.add(link)
        try:
            await s.flush()
            await s.commit()
            return link
        except IntegrityError:
            await s.rollback()
    # یکی دیگه هم‌زمان ساختش (ریس)؛ دوباره بخون
    async with Session() as s:
        link = await s.scalar(select(GateReferralLink).where(
            GateReferralLink.button_id == button_id, GateReferralLink.user_id == u.id))
        return link


async def handle_gate_referral_join(gate_code: str, new_telegram_id: int) -> None:
    """وقتی یه کاربرِ کاملاً تازه از طریقِ لینکِ اختصاصیِ یه دکمهٔ گیت‌شده وارد
    ربات می‌شه، این تابع صدا زده می‌شه (از tenant/__init__.py، دقیقاً مثلِ
    handle_referral_join برایِ رفرالِ عمومی، ولی فقط برایِ کاربرِ واقعاً
    تازه‌ساخته‌شده — was_created=True). هر فرد فقط یک‌بار برایِ همون لینک
    شمارش می‌شه."""
    async with Session() as s:
        link = await s.scalar(select(GateReferralLink).where(GateReferralLink.code == gate_code))
        if not link:
            return
        s.add(GateReferralJoin(link_id=link.id, referred_telegram_id=new_telegram_id))
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return  # قبلاً شمارش شده (محکم‌کاری؛ عملاً نباید پیش بیاد)
        link.join_count = (link.join_count or 0) + 1
        await s.commit()


async def _deliver_access_gatelink(gate, btn, msg, bot, row, u, bot_id, state, cb, req: int) -> None:
    from tenant import _run_button_action
    link = await _get_or_create_gate_link(bot_id, btn.id, u)
    have = link.join_count or 0
    if have < req:
        me = await bot.get_me()
        invite_url = f"https://t.me/{me.username}?start=gate_{link.code}"
        alert_text = (
            f"⚠️ برای استفاده از این بخش باید {req} نفر رو با لینکِ اختصاصیِ زیر دعوت کنی.\n"
            f"👤 تعداد فعلی: {have} از {req} نفر."
        )
        if cb is not None:
            try:
                await cb.answer(alert_text, show_alert=True)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        body = (
            alert_text
            + "\n\n🔗 لینکِ اختصاصیِ همین دکمه (برایِ کپی، روش لمس کن):\n"
            f"<code>{invite_url}</code>\n\n"
            "⚠️ این لینک فقط برایِ همین دکمه شمارش می‌شه؛ لینکِ دعوتِ عمومیِ ربات یا "
            "رفقایی که قبلاً از این لینک آورده بودی و مصرف شدن، دیگه شمارش نمی‌شن."
        )
        kb = _invite_keyboard(invite_url, None)
        # === PATCH_GATELINK_POINTS_ALT_V1 ===
        # گزارشِ ایلیا: «امتیازم الان ۹۰ است ولی اینجا ۰ زده». این دکمه فقط دعوتِ لینکِ اختصاصی را می‌شمارد
        # (طراحیِ عمدی) و امتیازِ رفرالِ کاربر دیده نمی‌شد. حالا اگر «امتیازگیری با رفرال» روشن است و کاربر
        # امتیازِ کافی دارد، یک راهِ دوم نشان داده می‌شود: باز کردنِ همین بخش با کسرِ همان تعداد امتیاز
        # (با تأییدِ خودِ کاربر). اگر امتیاز دارد ولی کم است، فقط وضعیتش را می‌بیند.
        try:
            from tenant.shared.ref_points import get_points, points_enabled
            if await points_enabled(bot_id):
                pts = await get_points(bot_id, u.id)
                if pts > 0:
                    body += f"\n\n🏅 امتیازِ رفرالِ تو: <b>{pts}</b>"
                    if pts >= req:
                        body += (f"\nمی‌تونی به‌جای دعوت، <b>{req} امتیاز</b> خرج کنی و همین الان بازش کنی 👇")
                        kb.inline_keyboard.insert(0, [InlineKeyboardButton(
                            text=f"🏅 بازکردن با {req} امتیاز",
                            callback_data=f"gate:ptsgo:{btn.id}", style=ButtonStyle.SUCCESS)])
                    else:
                        body += f"\n(برای باز کردن با امتیاز {req} امتیاز لازم است.)"
        except Exception:
            logger.exception("نمایشِ امتیازِ رفرال در گیتِ لینکِ اختصاصی ناموفق بود")
        await msg.answer(body, reply_markup=kb)
        return
    # مصرف: فقط اگه شمارنده هنوز واقعاً کافیه صفرش کن (اتمیک، ضدِ ریس)
    async with Session() as s:
        result = await s.execute(
            update(GateReferralLink)
            .where(GateReferralLink.id == link.id, GateReferralLink.join_count >= req)
            .values(join_count=0))
        await s.commit()
        if result.rowcount == 0:
            # یکی دیگه (یا همین کاربر با دو تپِ سریع) زودتر مصرفش کرد؛
            # دوباره از اول با وضعیتِ به‌روزشده تصمیم بگیر.
            await _deliver_access_gatelink(gate, btn, msg, bot, row, u, bot_id, state, cb, req)
            return
    if cb is not None:
        try:
            await cb.answer()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _run_button_action(gate.get("keep_action"), btn, msg, bot, state,
                             row, u, bot_id, cb=cb)


# === PATCH_REFERRAL_POINTS_V1 ===

async def _deliver_access_points(gate, btn, msg, bot, row, u, bot_id, state, cb, req: int) -> None:
    """گیتِ دسترسی با ملاکِ «امتیازِ رفرال».

    برخلافِ حالتِ gatelink (که شمارنده را صفر می‌کند)، اینجا فقط به‌اندازهٔ
    آستانه از کیفِ امتیازِ کاربر کم می‌شود؛ پس امتیازِ اضافه هدر نمی‌رود:
    با آستانهٔ ۳، شش امتیاز یعنی دو بار استفاده.
    """
    from tenant import _run_button_action
    from tenant.shared.ref_points import (
        deliver_reward, get_points, points_enabled, spend_points,
    )

    if not await points_enabled(bot_id):
        note = ("⛔️ «امتیازگیری با رفرال» در این ربات فعال نیست؛ "
                "مالکِ ربات باید از «دکمه‌ها و تنظیمات → 🏅 امتیازگیری با رفرال» روشنش کند.")
        if cb is not None:
            try:
                await cb.answer(note, show_alert=True)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        else:
            await msg.answer(note)
        return

    have = await get_points(bot_id, u.id)
    if have < req:
        link = await _build_user_invite_link(bot, bot_id, u)
        alert_text = (f"⚠️ برای استفاده از این بخش {req} امتیاز لازم است.\n"
                      f"🏅 امتیازِ فعلی تو: {have} از {req}")
        if cb is not None:
            try:
                await cb.answer(alert_text, show_alert=True)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        body = alert_text + "\n\nهر نفری که با لینکِ دعوتت وارد ربات شود امتیاز می‌گیری."
        if link:
            body += ("\n\n🔗 لینکِ دعوتِ اختصاصیِ تو (برای کپی، روش لمس کن):\n"
                     f"<code>{link}</code>")
        await msg.answer(body, reply_markup=_invite_keyboard(link, None))
        return

    # === PATCH_GATE_STOCK_CHECK_V1 ===
    # اگر عملکردِ نگه‌داشته‌شده (keep_action) نمایشِ یک محصولِ مشخص است،
    # قبل از کسرِ امتیاز موجودیِ همان محصول را چک کن — وگرنه امکان دارد
    # امتیاز کسر شود ولی محصول ناموجود باشد و کاربر چیزی دریافت نکند.
    if gate.get("keep_action") == "buy_product":
        pid = gate.get("product_id")
        if pid:
            async with Session() as _s:
                _p = await _s.get(Product, int(pid))
            unavailable = (not _p) or (not _p.is_active) or (_p.stock == 0)
            if unavailable:
                note = "⚠️ این محصول فعلاً ناموجود است — امتیازی کسر نشد."
                if cb is not None:
                    try:
                        await cb.answer(note, show_alert=True)
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                else:
                    await msg.answer(note)
                return

    # === PATCH_GATE_TRIAL_CHECK_V1 ===
    # اگر عملکردِ نگه‌داشته‌شده یک «تست رایگانِ VPN» است، قبل از کسرِ
    # امتیاز مطمئن شو واقعاً قابلِ‌تحویل است — وگرنه امکان دارد امتیاز
    # کسر شود ولی تست رایگان (چون سراسری خاموش است یا سهمیهٔ کاربر تمام
    # شده) هیچ‌وقت ساخته نشود.
    if gate.get("keep_action") == "free_trial":
        per_button_trial = bool(gate.get("trial_mode"))
        if not per_button_trial:
            from tenant import get_setting as _get_setting_g
            _trial_on = await _get_setting_g(bot_id, "trial_enabled", "0")
            if _trial_on != "1":
                note = "⚠️ تست رایگان فعلاً برای این ربات فعال نیست — امتیازی کسر نشد."
                if cb is not None:
                    try:
                        await cb.answer(note, show_alert=True)
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                else:
                    await msg.answer(note)
                return
        else:
            from tenant import _check_button_trial_allowance
            _allowed, _err = await _check_button_trial_allowance(btn.id, u.telegram_id, gate)
            if not _allowed:
                note = _err + "\n\nامتیازی کسر نشد."
                if cb is not None:
                    try:
                        await cb.answer(note, show_alert=True)
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                else:
                    await msg.answer(note)
                return

    if not await spend_points(bot_id, u.id, req):
        # هم‌زمانی (دو تپِ سریع): با وضعیتِ به‌روزشده دوباره تصمیم بگیر.
        await _deliver_access_points(gate, btn, msg, bot, row, u, bot_id, state, cb, req)
        return

    if cb is not None:
        try:
            await cb.answer()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    left = await get_points(bot_id, u.id)
    keep = gate.get("keep_action")
    if keep:
        # === PATCH_REFPOINT_GATE_MISLEADING_MSG_FIX_V1 ===
        # باگِ گزارش‌شده: وقتی گیت رویِ یک دکمهٔ *دیگر* (نه جایزهٔ تعریف‌شده
        # در «🏅 امتیازگیری با رفرال») گذاشته شده — یعنی keep_action ست
        # است — این حالت یعنی «فقط قفلِ همین دکمه باز شود»، نه «جایزه‌ای
        # تحویل شود». اما پیامِ پایینِ تابع همیشه یکسان بود و کسر شدن
        # امتیاز را طوری اعلام می‌کرد که انگار جایزه تحویل شده — کاربر
        # (و حتی ادمین موقعِ بررسیِ تیکت) فکر می‌کرد باید یک کانفیگ/جایزهٔ
        # واقعی هم بیاید، در حالی که تنها اتفاقی که افتاده باز شدنِ قفلِ
        # همین دکمه است (مثلاً یک منویِ خرید) — چیزی که خودِ عملکردِ دکمه
        # (keep_action) پایین‌تر نشانش می‌دهد.
        await _run_button_action(keep, btn, msg, bot, state, row, u, bot_id, cb=cb)
        await msg.answer(f"🔓 {req} امتیاز برایِ بازکردنِ این بخش کسر شد. امتیازِ باقی‌مانده‌ات: <b>{left}</b>")
    else:
        # دکمه عملکردِ دیگری ندارد → جایزهٔ تعریف‌شده در «🏅 امتیازگیری با رفرال»
        ok = await deliver_reward(bot, bot_id, u, msg)
        if not ok:
            # تحویل نشد؛ امتیازِ کسرشده را برگردان تا حقِ کاربر ضایع نشود.
            from tenant.shared.ref_points import award_points
            await award_points(bot_id, u.id, req)
            return
        await msg.answer(f"🏅 {req} امتیاز کسر شد. امتیازِ باقی‌مانده‌ات: <b>{left}</b>")


async def _claimed_tiers(bot_id: int, button_id: int, user_id: int) -> set[int]:
    async with Session() as s:
        rows = (await s.scalars(
            select(GatedClaim.tier).where(
                GatedClaim.button_id == button_id,
                GatedClaim.user_id == user_id,
            )
        )).all()
    return {int(t or 0) for t in rows}


async def _deliver_reward(gate, btn, msg, bot, row, u, bot_id, cb) -> None:
    src = gate["src"]
    tiers = gate["tiers"]
    have = await count_referrals(bot_id, u.id, msg.from_user.id, src)
    if cb is not None:
        try:
            await cb.answer()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    claimed = await _claimed_tiers(bot_id, btn.id, u.id)
    pending = [(i, t) for i, t in enumerate(tiers, 1)
               if have >= t["req"] and i not in claimed]

    if not pending:
        eligible_done = [i for i, t in enumerate(tiers, 1)
                         if have >= t["req"] and i in claimed]
        next_tier = next((t for i, t in enumerate(tiers, 1) if have < t["req"]), None)
        link = await _build_user_invite_link(bot, bot_id, u)
        parts = [f"👤 تعداد زیرمجموعه‌های فعلی تو: <b>{have}</b> نفر"]
        if eligible_done:
            parts.append(f"✅ جایزهٔ {len(eligible_done)} سطح را قبلاً گرفته‌ای.")
        if next_tier:
            parts.append(f"🎯 تا جایزهٔ سطح بعدی <b>{next_tier['req'] - have}</b> نفر "
                         f"دیگر مانده (در {next_tier['req']} نفر).")
        else:
            parts.append("🏆 همهٔ سطوح را تکمیل کرده‌ای!")
        if link:
            parts.append("\n🔗 لینک دعوت اختصاصی تو:\n<code>%s</code>" % link)
        await msg.answer("\n".join(parts),
                         reply_markup=_invite_keyboard(link, None))
        return

    delivered_any = False
    for tier_no, t in pending:
        ok = await _grant_one_tier(t, tier_no, btn, msg, bot, u, bot_id)
        delivered_any = delivered_any or ok

    if delivered_any:
        link = await _build_user_invite_link(bot, bot_id, u)
        next_tier = next((tt for i, tt in enumerate(tiers, 1) if have < tt["req"]), None)
        if next_tier:
            await msg.answer(
                f"🎯 سطح بعدی در <b>{next_tier['req']}</b> نفر است "
                f"({next_tier['req'] - have} نفر دیگر).",
                reply_markup=_invite_keyboard(link, None))


async def _grant_one_tier(t, tier_no, btn, msg, bot, u, bot_id) -> bool:
    from tenant import send_config_to_user, inline_back_kb

    async with Session() as s:
        claim = GatedClaim(bot_id=bot_id, button_id=btn.id, user_id=u.id,
                           tier=tier_no, delivered=None)
        s.add(claim)
        try:
            await s.flush()
            await s.commit()
        except IntegrityError:
            await s.rollback()
            return False

    rew = t.get("rew", "text")

    if rew == "stock":
        vol = int(t.get("vol", 0))
        days = int(t.get("days", 0))
        assigned_payload = None
        cfg_obj_id = None
        async with Session() as s:
            # === PATCH_CONFIG_STOCK_ROW_LOCK_V1 ===
            cfg = await s.scalar(
                select(Config).where(
                    Config.bot_id == bot_id, Config.status == "in_stock",
                    Config.volume_gb == vol, Config.days == days,
                ).with_for_update().limit(1)
            )
            if cfg:
                cfg.status = "sold"
                cfg.owner_user_id = u.id
                assigned_payload = cfg.payload
                cfg_obj_id = cfg.id
                cl = await s.scalar(select(GatedClaim).where(
                    GatedClaim.button_id == btn.id, GatedClaim.user_id == u.id,
                    GatedClaim.tier == tier_no))
                if cl:
                    cl.delivered = assigned_payload
                await s.commit()
            else:
                cl = await s.scalar(select(GatedClaim).where(
                    GatedClaim.button_id == btn.id, GatedClaim.user_id == u.id,
                    GatedClaim.tier == tier_no))
                if cl:
                    await s.delete(cl)
                    await s.commit()

        if not assigned_payload:
            await msg.answer(
                f"🎉 شرط سطح <b>{t['req']}</b> نفره را برآورده کردی! اما جایزهٔ این "
                "سطح موقتاً ناموجود است؛ کمی بعد دوباره همین دکمه را بزن.",
                reply_markup=await inline_back_kb(bot_id))
            return False

        await msg.answer(
            f"🎉 تبریک! شرط <b>{t['req']}</b> نفره را برآورده کردی. جایزه‌ات آماده است 🎁")
        try:
            await send_config_to_user(bot, msg.from_user.id, await _get_config(cfg_obj_id))
        except Exception:
            await msg.answer(f"<code>{assigned_payload}</code>")
        return True

    # rew == "text"
    async with Session() as s:
        cl = await s.scalar(select(GatedClaim).where(
            GatedClaim.button_id == btn.id, GatedClaim.user_id == u.id,
            GatedClaim.tier == tier_no))
        if cl:
            cl.delivered = btn.message or ""
            await s.commit()
    await msg.answer(
        f"🎉 تبریک! شرط <b>{t['req']}</b> نفره را برآورده کردی 🎁\n\n"
        + (btn.message or "—"),
        reply_markup=await inline_back_kb(bot_id))
    return True


async def _get_config(cfg_id: int):
    async with Session() as s:
        return await s.get(Config, cfg_id)


# ═══════════════════════════════════════════
#  پنل ادمین
# ═══════════════════════════════════════════

class GateFSM(StatesGroup):
    req = State()
    vol = State()
    days = State()


def _fa_to_int(text: str) -> int | None:
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    digits = (text or "").translate(trans).replace(",", "").replace("،", "").strip()
    return int(digits) if digits.isdigit() else None


async def _owner_ok(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return None
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return None
    return row


async def _get_btn(mb_id: int):
    async with Session() as s:
        return await s.get(MainButton, mb_id)


async def _patch_config(mb_id: int, **patch) -> None:
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
        if not mb:
            return
        cur = {}
        if mb.config:
            try:
                cur = json.loads(mb.config) or {}
            except Exception:
                cur = {}
        cur.update(patch)
        mb.config = json.dumps(cur, ensure_ascii=False)
        mb.action = "referral_gate"
        await s.commit()


async def _strip_keys(mb_id: int, keys) -> None:
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
        if not mb or not mb.config:
            return
        try:
            cur = json.loads(mb.config) or {}
        except Exception:
            return
        for k in keys:
            cur.pop(k, None)
        mb.config = json.dumps(cur, ensure_ascii=False)
        await s.commit()


async def _append_tier(mb_id: int, tier: dict) -> None:
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
        if not mb:
            return
        try:
            cur = json.loads(mb.config) if mb.config else {}
        except Exception:
            cur = {}
        cur.setdefault("mode", "reward")
        cur.setdefault("src", "join")
        tiers = cur.get("tiers") or []
        tiers = [t for t in tiers if int(t.get("req", 0)) != int(tier["req"])]
        tiers.append(tier)
        tiers.sort(key=lambda x: int(x.get("req", 0)))
        cur["tiers"] = tiers
        cur.pop("req", None)  # حذف کلید قدیمیِ تک‌سطحی در صورت وجود
        mb.config = json.dumps(cur, ensure_ascii=False)
        mb.action = "referral_gate"
        await s.commit()


async def _back_to_panel(cb, row, mb_id: int):
    from tenant import _thb_text, _thb_kb, _disabled_btn_ids
    mb = await _get_btn(mb_id)
    if not mb or mb.bot_id != row.id:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    is_dis = mb_id in await _disabled_btn_ids(row.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(_thb_text(mb, is_dis), reply_markup=_thb_kb(mb, is_dis))


# ─────────── نقطهٔ ورود: انتخاب نوع شرط ───────────

@gate_router.callback_query(F.data.startswith("gate:start:"))
async def gate_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[2])
    mb = await _get_btn(mb_id)
    if not mb or mb.bot_id != row.id:
        await cb.answer("یافت نشد", show_alert=True)
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🎯 <b>شرطی‌سازی با زیرمجموعه</b>\n\n"
        "🎁 <b>جایزهٔ پلکانی</b>: کاربر با رسیدن به هر تعداد زیرمجموعه یک جایزه "
        "(کانفیگ از انبار یا متن) می‌گیرد. چند سطح قابل تعریف است.\n\n"
        "🔒 <b>گیتِ دسترسی</b>: عملکرد فعلیِ این دکمه فقط برای کسانی که حداقل "
        "تعداد مشخصی زیرمجموعه دارند باز می‌شود (عملکرد دکمه حفظ می‌شود). "
        "هر استفاده رفرال‌هاش مصرف می‌شود؛ برایِ استفادهٔ بعدی باید همون تعداد "
        "زیرمجموعهٔ تازه بیاورد (نامحدود، پلکانی).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 جایزهٔ پلکانی", callback_data=f"gate:mode:reward:{mb_id}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="🔒 گیتِ دسترسی روی این دکمه", callback_data=f"gate:mode:access:{mb_id}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"gate:cancel:{mb_id}", style=ButtonStyle.DANGER)],
        ])
    )
    await cb.answer()


# ─────────── گیتِ دسترسی ───────────

@gate_router.callback_query(F.data.startswith("gate:mode:access:"))
async def gate_mode_access(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[3])
    mb = await _get_btn(mb_id)
    if not mb or mb.bot_id != row.id:
        await cb.answer("یافت نشد", show_alert=True)
        return
    keep = mb.action if mb.action and mb.action != "referral_gate" else None
    if keep is None:
        prev = _parse_config(mb) or {}
        keep = prev.get("keep_action")
    await state.set_state(GateFSM.req)
    await state.update_data(gate_mb=mb_id, gate_mode="access", gate_keep=keep)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    ka_txt = f"<code>{keep}</code>" if keep else "متن/پیام دکمه"
    await cb.message.answer(
        "🔒 <b>گیتِ دسترسی</b>\n\n"
        f"عملکردی که پس از باز شدن اجرا می‌شود: {ka_txt}\n\n"
        "💡 هر بار که کاربر استفاده کند، همون تعداد زیرمجموعه‌ای که وارد می‌کنی مصرف "
        "می‌شود؛ برایِ استفادهٔ بعدی باید همون تعداد زیرمجموعهٔ <b>تازه</b> بیاورد "
        "(نامحدود، پلکانیِ فزاینده).\n\n"
        "حداقل چند زیرمجموعه لازم باشد؟ یک عدد بفرست (مثلاً <code>3</code>):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"gate:cancel:{mb_id}", style=ButtonStyle.DANGER),
        ]])
    )
    await cb.answer()


# ─────────── جایزهٔ پلکانی ───────────

@gate_router.callback_query(F.data.startswith("gate:mode:reward:"))
async def gate_mode_reward(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[3])
    mb = await _get_btn(mb_id)
    cur = _parse_config(mb) or {}
    if cur.get("mode") != "reward":
        await _patch_config(mb_id, mode="reward", src=cur.get("src", "join"),
                            tiers=cur.get("tiers", []))
        await _strip_keys(mb_id, ("keep_action", "req"))
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _show_reward_panel(cb.message, mb_id)
    await cb.answer()


async def _show_reward_panel(msg, mb_id: int):
    mb = await _get_btn(mb_id)
    gate = load_gate(mb) if mb else None
    src = (gate or {}).get("src", "join")
    lines = ["🎁 <b>جایزهٔ پلکانی</b>\n",
             f"📊 ملاک شمارش: <b>{_src_label(src)}</b>"]
    tiers = (gate or {}).get("tiers", []) if gate else []
    if tiers:
        lines.append("\n<b>سطوح تعریف‌شده:</b>")
        for i, t in enumerate(tiers, 1):
            rw = (f"کانفیگ {t.get('vol', 0)}گیگ/{t.get('days', 0)}روز"
                  if t.get("rew") == "stock" else "متن/کانفیگ ثابت")
            lines.append(f"  {i}) <b>{t['req']}</b> نفر → {rw}")
    else:
        lines.append("\n<i>هنوز سطحی تعریف نشده.</i>")
    if (gate or {}).get("link"):
        lines.append("\n🔗 لینک دعوتِ دلخواه: ✅")
    rows = [[InlineKeyboardButton(text="➕ افزودن سطح جدید", callback_data=f"gate:tier:add:{mb_id}", style=ButtonStyle.SUCCESS)]]
    if tiers:
        rows.append([InlineKeyboardButton(text="🗑 حذف آخرین سطح", callback_data=f"gate:tier:pop:{mb_id}", style=ButtonStyle.DANGER)])
    rows += [
        [InlineKeyboardButton(text="👥 دعوت‌شدگانِ لینک", callback_data=f"gate:src:join:{mb_id}", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="✅ زیرمجموعهٔ واقعی", callback_data=f"gate:src:real:{mb_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✅ پایان", callback_data=f"gate:done:{mb_id}", style=ButtonStyle.SUCCESS)],
    ]
    await msg.answer("\n".join(lines),
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@gate_router.callback_query(F.data.startswith("gate:src:"))
async def gate_set_src(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, src, mb_id = cb.data.split(":")
    await _patch_config(int(mb_id), src=src)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _show_reward_panel(cb.message, int(mb_id))
    await cb.answer("ملاک ذخیره شد")


@gate_router.callback_query(F.data.startswith("gate:tier:add:"))
async def gate_tier_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[3])
    await state.set_state(GateFSM.req)
    await state.update_data(gate_mb=mb_id, gate_mode="reward")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🎯 حداقل تعداد زیرمجموعهٔ این سطح را بفرست (مثلاً <code>3</code>):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"gate:cancel:{mb_id}", style=ButtonStyle.DANGER),
        ]])
    )
    await cb.answer()


@gate_router.callback_query(F.data.startswith("gate:tier:pop:"))
async def gate_tier_pop(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[3])
    mb = await _get_btn(mb_id)
    cur = _parse_config(mb) or {}
    tiers = cur.get("tiers") or []
    if tiers:
        tiers.sort(key=lambda x: int(x.get("req", 0)))
        tiers.pop()
        await _patch_config(mb_id, tiers=tiers)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _show_reward_panel(cb.message, mb_id)
    await cb.answer("آخرین سطح حذف شد")


# ─────────── دریافت عدد req ───────────

@gate_router.message(GateFSM.req, F.text)
async def gate_set_req(msg: Message, state: FSMContext):
    n = _fa_to_int(msg.text)
    if not n or n <= 0 or n > 100000:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 3).")
        return
    data = await state.get_data()
    mb_id = data.get("gate_mb")
    mode = data.get("gate_mode", "reward")

    if mode == "access":
        # === PATCH_GATE_DEDICATED_LINK_V1 ===
        # قبلاً اینجا مستقیم finalize می‌شد (با src پیش‌فرضِ «join»، بدونِ
        # اینکه ادمین اصلاً می‌دیدش). حالا قبل از نهایی‌سازی، ملاکِ شمارش رو
        # می‌پرسیم — از جمله گزینهٔ جدیدِ «لینکِ اختصاصیِ همین دکمه».
        await state.update_data(gate_access_req=n)
        await msg.answer(
            f"✅ عدد <b>{n}</b> ثبت شد. ملاکِ شمارش رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 لینکِ اختصاصیِ همین دکمه (سوزاننده)",
                                      callback_data=f"gate:asrc:gatelink:{mb_id}", style=ButtonStyle.PRIMARY)],
                # === PATCH_REFERRAL_POINTS_V1 ===
                [InlineKeyboardButton(text="🏅 امتیازِ رفرال (کسرِ امتیاز)",
                                      callback_data=f"gate:asrc:points:{mb_id}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton(text="👥 دعوت‌شدگانِ لینکِ عمومی",
                                      callback_data=f"gate:asrc:join:{mb_id}", style=ButtonStyle.PRIMARY),
                 InlineKeyboardButton(text="✅ زیرمجموعهٔ واقعی",
                                      callback_data=f"gate:asrc:real:{mb_id}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text="❌ لغو", callback_data=f"gate:cancel:{mb_id}", style=ButtonStyle.DANGER)],
            ]))
        return

    await state.update_data(gate_tier_req=n)
    await msg.answer(
        f"✅ سطح <b>{n}</b> نفره. نوع جایزهٔ این سطح چیست؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 متن/کانفیگ ثابت", callback_data=f"gate:trew:text:{mb_id}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="📦 کانفیگ از انبار", callback_data=f"gate:trew:stock:{mb_id}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"gate:cancel:{mb_id}", style=ButtonStyle.DANGER)],
        ]))


# === PATCH_GATELINK_POINTS_ALT_V1 ===
@gate_router.callback_query(F.data.startswith("gate:ptsgo:"))
async def gate_use_points(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بازکردنِ گیتِ «لینکِ اختصاصی» با کسرِ امتیازِ رفرال (راهِ دومِ همان گیت)."""
    from tenant import get_bot_row
    from models import MainButton
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    btn_id = int(cb.data.split(":")[2])
    async with Session() as s:
        btn = await s.get(MainButton, btn_id)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
    gate = load_gate(btn) if (btn and btn.bot_id == row.id) else None
    if not gate or gate.get("mode") != "access" or gate.get("src") != "gatelink" or not u:
        await cb.answer("این گزینه دیگر در دسترس نیست.", show_alert=True)
        return
    fake = cb.message.model_copy(update={"from_user": cb.from_user}).as_(bot)
    await _deliver_access_points(gate, btn, fake, bot, row, u, row.id, state, cb, int(gate["req"]))


# === PATCH_GATE_DEDICATED_LINK_V1 ===

@gate_router.callback_query(F.data.startswith("gate:asrc:"))
async def gate_set_access_src(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, src, mb_id_s = cb.data.split(":")
    mb_id = int(mb_id_s)
    data = await state.get_data()
    n = data.get("gate_access_req")
    keep = data.get("gate_keep")
    if not n:
        await cb.answer("خطا؛ دوباره تلاش کن", show_alert=True)
        return
    await _patch_config(mb_id, mode="access", req=int(n), keep_action=keep, src=src)
    await _strip_keys(mb_id, ("tiers",))
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    ka_txt = f"<code>{keep}</code>" if keep else "متن/پیام دکمه"
    src_note = {
        "gatelink": ("🔗 لینکِ اختصاصیِ همین دکمه — مستقل از بقیهٔ دکمه‌ها و لینکِ عمومی؛ "
                    "بعد از هر استفاده صفر می‌شه و باید از نو رفقایِ تازه بیاره."),
        "points": ("🏅 امتیازِ رفرال — هر رفرال امتیاز می‌دهد و هر استفاده همین تعداد "
                   "امتیاز کم می‌کند (دو برابرِ آستانه = دو بار استفاده). "
                   "قبلش از «دکمه‌ها و تنظیمات → 🏅 امتیازگیری با رفرال» روشنش کن."),
        "join": "👥 دعوت‌شدگانِ لینکِ عمومیِ کاربر (پلکانیِ فزاینده).",
        "real": "✅ زیرمجموعهٔ واقعیِ ثبت‌شده (پلکانیِ فزاینده).",
    }.get(src, src)
    await cb.message.answer(
        f"✅ گیتِ دسترسی روی <b>{n}</b> زیرمجموعه تنظیم شد.\n"
        f"عملکرد دکمه پس از باز شدن: {ka_txt}\n"
        f"📊 ملاکِ شمارش: {src_note}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ پایان", callback_data=f"gate:done:{mb_id}", style=ButtonStyle.SUCCESS)],
        ]))
    await cb.answer()


@gate_router.callback_query(F.data.startswith("gate:trew:text:"))
async def gate_tier_rew_text(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[3])
    data = await state.get_data()
    req = int(data.get("gate_tier_req", 0))
    if req <= 0:
        await cb.answer("خطا؛ دوباره تلاش کن", show_alert=True)
        return
    await _append_tier(mb_id, {"req": req, "rew": "text"})
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"✅ سطح <b>{req}</b> نفره با جایزهٔ «متن/کانفیگ ثابت» اضافه شد.\n"
        "متن این دکمه را با «✏️ تنظیم متنِ دکمه» تنظیم کن؛ همان به کاربرِ واجدِ "
        "شرایط داده می‌شود.")
    await _show_reward_panel(cb.message, mb_id)
    await cb.answer()


@gate_router.callback_query(F.data.startswith("gate:trew:stock:"))
async def gate_tier_rew_stock(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[3])
    await state.set_state(GateFSM.vol)
    await state.update_data(gate_mb=mb_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "📦 حجمِ بستهٔ جایزهٔ این سطح چند گیگ باشد؟ (مثلاً <code>2</code>):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"gate:cancel:{mb_id}", style=ButtonStyle.DANGER),
        ]])
    )
    await cb.answer()


@gate_router.message(GateFSM.vol, F.text)
async def gate_set_vol(msg: Message, state: FSMContext):
    n = _fa_to_int(msg.text)
    if n is None or n < 0:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 2).")
        return
    await state.update_data(gate_vol=n)
    await state.set_state(GateFSM.days)
    data2 = await state.get_data()
    _mid = data2.get("gate_mb", 0)
    await msg.answer(
        "📅 مدتِ بستهٔ جایزه چند روز باشد؟ (مثلاً <code>30</code>):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"gate:cancel:{_mid}", style=ButtonStyle.DANGER),
        ]])
    )


@gate_router.message(GateFSM.days, F.text)
async def gate_set_days(msg: Message, state: FSMContext):
    n = _fa_to_int(msg.text)
    if n is None or n < 0:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 30).")
        return
    data = await state.get_data()
    mb_id = data.get("gate_mb")
    req = int(data.get("gate_tier_req", 0))
    vol = int(data.get("gate_vol", 0))
    await state.clear()
    if req <= 0:
        await msg.answer("خطا در ثبت سطح؛ دوباره از «➕ افزودن سطح جدید» تلاش کن.")
        return
    await _append_tier(mb_id, {"req": req, "rew": "stock", "vol": vol, "days": n})
    await msg.answer(
        f"✅ سطح <b>{req}</b> نفره → کانفیگ <b>{_fmt_vol(vol)} / {n} روز</b> اضافه شد.\n"
        "⚠️ به‌اندازهٔ کافی کانفیگ با این حجم/مدت در انبار داشته باش.")
    await _show_reward_panel(msg, mb_id)


# === PATCH_GATE_DEDICATED_LINK_V1 ===
# «🔗 تنظیم لینک دعوت دلخواه» (ورودیِ دستیِ یه URL توسطِ ادمین) حذف شد —
# جایگزینش همون کدِ اختصاصیِ خودکارِ هر دکمه‌ست (src=«gatelink» تویِ گیتِ
# دسترسی) که دیگه نیازی به تایپِ دستیِ لینک نداره.


# ─────────── پایان / لغو / حذف ───────────

@gate_router.callback_query(F.data.startswith("gate:done:"))
async def gate_done(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[2])
    await state.clear()
    await _back_to_panel(cb, row, mb_id)
    await cb.answer("ذخیره شد")


@gate_router.callback_query(F.data.startswith("gate:cancel:"))
async def gate_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[2])
    await state.clear()
    await _back_to_panel(cb, row, mb_id)
    await cb.answer("لغو شد")


@gate_router.callback_query(F.data.startswith("gate:off:"))
async def gate_off(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mb_id = int(cb.data.split(":")[2])
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
        if mb:
            keep = None
            cur = {}
            if mb.config:
                try:
                    cur = json.loads(mb.config) or {}
                except Exception:
                    cur = {}
                keep = cur.get("keep_action")
            # کلیدهای مخصوص گیت را حذف کن ولی بقیه (مثل بازهٔ buy_config) را نگه دار
            for k in ("mode", "src", "req", "tiers", "link", "keep_action"):
                cur.pop(k, None)
            mb.action = keep
            mb.config = json.dumps(cur, ensure_ascii=False) if cur else None
            await s.commit()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "✅ شرطِ زیرمجموعه از این دکمه برداشته شد؛ دکمه به حالت قبلی برگشت.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات این دکمه",
                                 callback_data=f"thb:cat:action:{mb_id}", style=ButtonStyle.DANGER),
        ]]))
    await cb.answer("انجام شد")
