"""
smm_ai.py — دستیارِ هوشِ مصنوعیِ خدماتِ مجازی (PATCH_SMM_AI_ASSISTANT_V1).

ایده: مشتری سؤال می‌پرسه (چطور سفارش بدم؟ سفارشم چرا انجام نشده؟ وضعیتِ
سفارشم چیه؟ و...)، ربات با هوشِ مصنوعی جواب می‌ده — با یه سیستم‌پرامپتِ
ثابت دربارهٔ نحوهٔ کارِ ربات، به‌علاوهٔ خلاصهٔ آخرین سفارش‌هایِ خودِ همون
مشتری (تا جواب‌هایِ مرتبط‌تری بده، نه فقط عمومی).

=== PATCH_SMM_AI_MULTI_PROVIDER_V1 ===
سه ارائه‌دهنده پشتیبانی می‌شه: Anthropic (Claude)، Google (Gemini)، و
OpenAI (ChatGPT). ادمین اول ارائه‌دهنده رو انتخاب می‌کنه، بعد کلیدِ همون
رو وارد می‌کنه. تغییرِ ارائه‌دهنده یعنی باید کلیدِ جدید هم بده (چون
کلیدهاشون فرق داره)، پس فقط یه کلید ذخیره می‌شه — مالِ همون ارائه‌دهندهٔ
فعلی.

هزینه: هر ادمین کلیدِ API خودشو وارد می‌کنه — هزینهٔ مصرف مستقیم رویِ
حسابِ خودشه، نه رویِ پلتفرم. اگه کلید تنظیم نشده باشه، این قابلیت
اصلاً به مشتری نشون داده نمی‌شه.
"""
from __future__ import annotations

import html
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle, ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

try:
    import aiohttp
except ImportError:
    aiohttp = None

from config import Session
from models import SmmOrder, SmmService, SmmCategory, VpnPlan, MainButton, PanelAccount
from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)


def _group_ai_allowed(row) -> bool:
    return bool(row and (row.field == "virtual_service" or (row.field == "vpn_sales" and row.plan == "pro")))

smm_ai_router = Router()

_TIMEOUT = 25

# === PATCH_SMM_AI_MULTI_PROVIDER_V1 ===
# (کلیدِ داخلی: برچسبِ فارسی, مدلی که استفاده می‌کنیم, جایی که کلید ازش گرفته می‌شه)
_PROVIDERS = {
    "anthropic": ("🟣 Claude (Anthropic)", "claude-haiku-4-5-20251001", "console.anthropic.com"),
    # === PATCH_GEMINI_MODEL_3_6_V1 ===
    # «gemini-2.5-flash» برایِ کلیدهایِ جدید (کاربرهایِ جدید) دیگه در
    # دسترس نیست؛ گوگل خطایِ 404 با پیشنهادِ صریحِ «gemini-3.6-flash» می‌ده.
    "gemini":    ("🔵 Gemini (Google)", "gemini-3.6-flash", "aistudio.google.com/apikey"),
    "openai":    ("🟢 ChatGPT (OpenAI)", "gpt-4o-mini", "platform.openai.com/api-keys"),
    # === PATCH_SMM_AI_GROQ_V1 ===
    # درخواستِ ادمین: Groq — رایگان تا ۱۰۰۰ درخواست در روز، برایِ یه
    # دستیارِ پشتیبانیِ ساده کاملاً کافیه.
    "groq":      ("🟠 Groq (رایگان)", "openai/gpt-oss-120b", "console.groq.com/keys"),
}
_DEFAULT_PROVIDER = "anthropic"


class SmmAiFSM(StatesGroup):
    api_key = State()
    question = State()
    group_name = State()  # === PATCH_SMM_AI_GROUP_V1 ===
    group_question = State()  # مکالمهٔ دومرحله‌ایِ نام‌محور در گروه


async def _get_setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set_setting(bot_id: int, key: str, value: str):
    from tenant import set_setting
    await set_setting(bot_id, key, value)


async def _get_provider(bot_id: int) -> str:
    p = await _get_setting(bot_id, "smm_ai_provider", _DEFAULT_PROVIDER)
    return p if p in _PROVIDERS else _DEFAULT_PROVIDER


_SYSTEM_PROMPT = """تو دستیار فارسیِ همین ربات تلگرام هستی، نه یک نمایش‌دهندهٔ اطلاعات فنی.
فقط فارسی، روان، محترمانه و کوتاه جواب بده؛ معمولاً ۲ تا ۵ جمله کافی است.
بر اساس اطلاعات واقعی و اختصاصی همین ربات پاسخ بده و برای سؤال‌های «چطور بخرم؟» مسیر را مرحله‌به‌مرحله با نام فارسی دکمه‌ها توضیح بده.
هیچ‌وقت متن خام context، فهرست داخلی، action، callback، شناسهٔ فنی، نام جدول، نام مدل، JSON، کد، کلید API یا عبارت‌هایی مثل action=... و callback_data=... را به کاربر نشان نده.
اطلاعات محصولات، قیمت‌ها و دکمه‌ها را فقط وقتی ذکر کن که به سؤال مربوط باشند؛ همهٔ منو را بی‌دلیل فهرست نکن.
اگر پاسخ دقیق در اطلاعات همین ربات نیست، صادقانه بگو «این مورد در اطلاعات فعلی ربات پیدا نشد؛ لطفاً از پشتیبانی راهنمایی بگیر.» و حدس نزن.
از ارجاع به پشتیبانی فقط به‌عنوان آخرین راه استفاده کن؛ اگر جواب در اطلاعات هست (قیمت، شرایط، مسیر خرید) مستقیم و با عدد جواب بده.
اگر سؤال مبهم است، یک سؤال کوتاه برای روشن‌شدن منظور بپرس. زمان قطعی انجام سفارش یا ادعای خارج از اطلاعات ربات را وعده نده.
اطلاعات کاربران دیگر و داده‌های محرمانه را هرگز افشا نکن."""


async def _ask_anthropic(api_key: str, user_content: str, system_prompt: str = _SYSTEM_PROMPT) -> str | None:
    _, model, _ = _PROVIDERS["anthropic"]
    payload = {
        "model": model,
        "max_tokens": 400,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    t = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with aiohttp.ClientSession(timeout=t) as s:
        async with s.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                logger.info(f"[SMM_AI] Claude جوابِ غیرِ۲۰۰ داد: status={r.status} body={data!r}")
                return None
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return text.strip() or None


async def _ask_openai(api_key: str, user_content: str, system_prompt: str = _SYSTEM_PROMPT) -> str | None:
    _, model, _ = _PROVIDERS["openai"]
    payload = {
        "model": model,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    t = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with aiohttp.ClientSession(timeout=t) as s:
        async with s.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                logger.info(f"[SMM_AI] ChatGPT جوابِ غیرِ۲۰۰ داد: status={r.status} body={data!r}")
                return None
            choices = data.get("choices") or []
            if not choices:
                return None
            text = (choices[0].get("message") or {}).get("content", "")
            return text.strip() or None


async def _ask_gemini(api_key: str, user_content: str, system_prompt: str = _SYSTEM_PROMPT) -> str | None:
    """PATCH_GEMINI_AUTH_KEY_HEADER_V1 — گوگل از اواسطِ ۲۰۲۶ داره کلیدهایِ
    «AQ.» (Auth key) رو به‌جایِ فرمتِ قدیمیِ «AIza» (Standard key) صادر
    می‌کنه؛ از سپتامبرِ ۲۰۲۶ کلیدهایِ Standard رو کلاً رد می‌کنه. کلیدهایِ
    AQ. وقتی از طریقِ کوئری‌پارامترِ ?key= فرستاده بشن (روشِ قبلیِ اینجا)،
    با 401 UNAUTHENTICATED رد می‌شن — طبقِ مستنداتِ رسمی و مثال‌هایِ REST
    فعلیِ گوگل، باید از هدرِ x-goog-api-key استفاده بشه، هم برایِ کلیدهایِ
    جدید هم قدیمی. برایِ همینه که «amir» با هر کلیدی که از AI Studio
    می‌گرفت (که الان همه‌شون AQ. هستن) با «این کلید کار نکرد» مواجه می‌شد."""
    _, model, _ = _PROVIDERS["gemini"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"maxOutputTokens": 400},
    }
    headers = {"content-type": "application/json", "x-goog-api-key": api_key}
    t = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with aiohttp.ClientSession(timeout=t) as s:
        async with s.post(url, json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                logger.info(f"[SMM_AI] Gemini جوابِ غیرِ۲۰۰ داد: status={r.status} body={data!r}")
                return None
            cands = data.get("candidates") or []
            if not cands:
                return None
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            return text.strip() or None


async def _ask_groq(api_key: str, user_content: str, system_prompt: str = _SYSTEM_PROMPT) -> str | None:
    """Groq از همون قالبِ OpenAI Chat Completions پیروی می‌کنه، فقط
    آدرس/مدلش فرق داره — برایِ همین تقریباً عینِ _ask_openai است."""
    _, model, _ = _PROVIDERS["groq"]
    payload = {
        "model": model,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    t = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with aiohttp.ClientSession(timeout=t) as s:
        async with s.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                # === PATCH_SMM_AI_GROUP_V1 ===
                logger.info(f"[SMM_AI] Groq جوابِ غیرِ۲۰۰ داد: status={r.status} body={data!r}")
                return None
            choices = data.get("choices") or []
            if not choices:
                logger.info(f"[SMM_AI] Groq پاسخ داد ولی choices خالی بود: {data!r}")
                return None
            text = (choices[0].get("message") or {}).get("content", "")
            return text.strip() or None


_PROVIDER_FUNCS = {
    "anthropic": _ask_anthropic,
    "openai": _ask_openai,
    "gemini": _ask_gemini,
    "groq": _ask_groq,
}


def _clean_ai_answer(answer: str | None) -> str | None:
    """هرگونه نشت شناسه‌های فنی از پاسخ مدل را قبل از ارسال حذف می‌کند."""
    if not answer:
        return None
    import re
    value = str(answer).strip()
    value = re.sub(r"\s*\(?\s*(?:action|callback(?:_data)?)\s*=\s*[^)\n]+\)?", "", value, flags=re.I)
    value = re.sub(r"\s*\(?\s*(?:tadm|smm|usr|smmai|group|bet|wallet):[A-Za-z0-9_:-]+\)?", "", value, flags=re.I)
    value = re.sub(r"\n\s*\([^()\n]*=[^()\n]*\)", "", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()[:2000] or None


# === PATCH_AI_GROUP_HTML_V1 ===
def md_to_html(text: str) -> str:
    """پاسخِ مدل (مارک‌داونِ ساده) را به HTMLِ تلگرام تبدیل می‌کند: **متن** → <b>، `کد` → <code>.
    باگِ گزارش‌شده: ستاره‌ها خام نشان داده می‌شدند («**پشتیبانی**»)."""
    import re
    t = html.escape(text or "", quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(?m)^\s*#{1,6}\s*(.+)$", r"<b>\1</b>", t)
    return t.replace("**", "")


async def ask_ai(provider: str, api_key: str, question: str, context: str = "",
                system_prompt: str | None = None) -> str | None:
    """سؤال را با context اختصاصی همان ربات به ارائه‌دهنده می‌فرستد."""
    if not aiohttp or not api_key:
        return None
    fn = _PROVIDER_FUNCS.get(provider)
    if not fn:
        return None
    user_content = question.strip()
    if context:
        user_content = f"{context}\n\nسؤالِ مشتری: {question.strip()}"
    system_prompt = system_prompt or _SYSTEM_PROMPT
    try:
        return _clean_ai_answer(await fn(api_key, user_content, system_prompt))
    except Exception:
        logger.exception(f"[SMM_AI] خطایِ غیرمنتظره در فراخوانیِ {provider}")
        return None


def _bot_system_prompt(row) -> str:
    if getattr(row, "field", "") == "vpn_sales":
        return """تو دستیار اختصاصی همین ربات فروش VPN در تلگرام هستی.
فقط فارسی، روان و کوتاه، حداکثر ۵ جمله جواب بده. فقط از اطلاعات زنده‌ای که در «اطلاعات اختصاصی این ربات» آمده استفاده کن و مسیر یا قیمت پلنی را حدس نزن.
اطلاعات خام context، action، callback، شناسهٔ فنی، JSON، کد و نام داخلی منو را هرگز به کاربر نشان نده؛ فقط نام فارسی دکمه‌ها و مسیر سادهٔ خرید را بگو.
برای خرید VPN، کاربر را به دکمه/مسیر واقعی درج‌شده در context راهنمایی کن؛ اگر مسیر یا محصولی در context نیست، صادقانه بگو اطلاعاتش در این ربات ثبت نشده و کاربر را به پشتیبانی ارجاع بده.
در مورد سرویس‌های کاربر فقط از اطلاعات همان کاربر استفاده کن. هرگز کلید API، اطلاعات کاربران دیگر یا دادهٔ داخلی را افشا نکن. زمان قطعی تحویل را وعده نده."""
    return """تو دستیار اختصاصی همین ربات خدمات مجازی در تلگرام هستی.
فقط فارسی، روان و کوتاه، حداکثر ۵ جمله جواب بده. فقط از اطلاعات زنده‌ای که در «اطلاعات اختصاصی این ربات» آمده استفاده کن و مسیر یا قیمت سرویس را حدس نزن.
اطلاعات خام context، action، callback، شناسهٔ فنی، JSON، کد و نام داخلی منو را هرگز به کاربر نشان نده؛ فقط نام فارسی دکمه‌ها و مسیر سادهٔ خرید را بگو.
برای خرید خدمات، کاربر را به دکمه/مسیر واقعی درج‌شده در context راهنمایی کن. اگر قیمت یا شرایط در context هست (مثلاً قیمت استارز یا تون) همان را با عدد بگو و بعد مسیرِ خرید را بگو؛ فقط وقتی واقعاً اطلاعاتی نیست کاربر را به پشتیبانی ارجاع بده.
برای تأکید می‌توانی از **متنِ پررنگ** استفاده کنی (ربات آن را درست نمایش می‌دهد) ولی زیاد استفاده نکن.
در مورد سفارش‌ها فقط از اطلاعات همان کاربر استفاده کن. هرگز کلید API، اطلاعات کاربران دیگر یا دادهٔ داخلی را افشا نکن. زمان قطعی تکمیل سفارش را وعده نده."""


async def _live_bot_context(bot_id: int, row, telegram_id: int | None = None) -> str:
    """کاتالوگ زندهٔ همان bot؛ هیچ داده‌ای از ربات دیگر وارد prompt نمی‌شود."""
    sections = ["اطلاعات اختصاصی این ربات — منبع حقیقت فقط همین بخش است:"]
    field = getattr(row, "field", "")
    async with Session() as s:
        buttons = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id).order_by(MainButton.row, MainButton.col).limit(80))).all()
        if buttons:
            sections.append("دکمه‌ها و مسیرهای منو:")
            sections.append("\n".join(
                f"- {b.label}" for b in buttons if b.label))
        if field == "vpn_sales":
            plans = (await s.scalars(select(VpnPlan).where(
                VpnPlan.bot_id == bot_id, VpnPlan.active.is_(True)).order_by(VpnPlan.id).limit(80))).all()
            sections.append("پلن‌های فعال VPN:")
            if plans:
                sections.append("\n".join(
                    f"- {p.title}: {int(p.volume_gb or 0)}GB، {int(p.days or 0)} روز، "
                    f"قیمت {int(p.price or 0):,} تومان، IP هم‌زمان {int(p.limit_ip or 0)}"
                    for p in plans))
            else:
                sections.append("- پلن فعال VPN در حال حاضر ثبت نشده است.")
            if telegram_id is not None:
                accounts = (await s.scalars(select(PanelAccount).where(
                    PanelAccount.bot_id == bot_id,
                    PanelAccount.user_telegram_id == telegram_id,
                    PanelAccount.reseller_made.is_not(True)).order_by(PanelAccount.id.desc()).limit(5))).all()
                if accounts:
                    sections.append("سرویس‌های همین کاربر:")
                    sections.append("\n".join(
                        f"- {a.plan_title or 'سرویس VPN'}، {int(a.volume_gb or 0)}GB، "
                        f"{int(a.days or 0)} روز، وضعیت {a.status}"
                        for a in accounts))
        else:
            cats = (await s.scalars(select(SmmCategory).where(
                SmmCategory.bot_id == bot_id, SmmCategory.is_active.is_(True)).order_by(SmmCategory.id).limit(60))).all()
            services = (await s.scalars(select(SmmService).where(
                SmmService.bot_id == bot_id, SmmService.is_active.is_(True)).order_by(SmmService.id).limit(80))).all()
            if cats:
                sections.append("دسته‌های فعال خدمات:")
                sections.append("\n".join(f"- {c.platform}: {c.name}" for c in cats))
            # === PATCH_AI_STARS_TON_CONTEXT_V1 === قیمتِ استارز/تون تا مدل به «پشتیبانی» ارجاع ندهد
            try:
                from tenant.bots.virtual_service.smm import _stars_sell_settings, _ton_sell_settings
                _sr, _smin, _smax = await _stars_sell_settings(bot_id)
                if _sr > 0 and await _get_setting(bot_id, "stars_sell_enabled", "1") != "0":
                    sections.append(f"فروش استارز تلگرام: هر ۱ استارز {int(_sr):,} تومان؛ حداقل {_smin:,} و حداکثر {_smax:,} استارز؛ "
                                    f"مثلاً ۱۰۰ استارز = {int(_sr * 100):,} تومان. مسیر خرید: دکمهٔ «استارز و تون» در منوی اصلی.")
                _tr, _tmin, _tmax = await _ton_sell_settings(bot_id)
                if _tr > 0:
                    sections.append(f"فروش تون (TON): هر ۱ تون {int(_tr):,} تومان؛ حداقل {_tmin:g} و حداکثر {_tmax:g} تون.")
            except Exception:
                logger.exception("[SMM_AI] افزودنِ قیمتِ استارز/تون به context ناموفق بود")
            sections.append("سرویس‌های فعال:")
            if services:
                sections.append("\n".join(
                    f"- {s.name}؛ دسته {s.category or 'عمومی'}؛ حداقل {int(s.min_qty or 0)}؛ "
                    f"حداکثر {int(s.max_qty or 0)}؛ قیمت هر ۱۰۰۰ {int(s.sell_rate or 0):,} تومان"
                    + (f"؛ توضیح: {(s.description or '')[:180]}" if s.description else "")
                    for s in services))
            else:
                sections.append("- سرویس فعال ثبت نشده است.")
    return "\n\n".join(sections)[:12000]


async def _recent_orders_context(bot_id: int, telegram_id: int) -> str:
    async with Session() as s:
        orders = (await s.scalars(select(SmmOrder).where(
            SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == telegram_id)
            .order_by(SmmOrder.id.desc()).limit(5))).all()
    if not orders:
        return "این مشتری هنوز هیچ سفارشی ثبت نکرده."
    lines = ["آخرین سفارش‌هایِ همین مشتری:"]
    for o in orders:
        lines.append(f"- #{o.tracking_code or o.id}: {o.service_name or 'سرویس'} × {o.quantity}, "
                     f"وضعیت: {o.status}, مبلغ: {int(o.price_toman or 0):,} تومان")
    return "\n".join(lines)


# ═══════════════════ تنظیماتِ ادمین ═══════════════════

# === PATCH_SMMAI_ORIGIN_TRACKING_V1 ===
# این صفحه از دو جا صدا زده می‌شه: «⚙️ گیفت/پرمیوم/... و تنظیماتِ فروش»
# (smm:menu) و «⚙️ تنظیماتِ گروه» (tadm:groupsettings). دکمه‌ی بازگشت باید
# برگرده همونجایی که کاربر ازش اومده، نه یه مقصدِ ثابت.
_SMMAI_ORIGIN_TARGETS = {
    "smm": "smm:menu",
    "group": "tadm:groupsettings",
    # === PATCH_GROUP_AI_COMMUNITY_MGMT_V1 ===
    # ورودی از بخشِ «سرویس‌ها و محصولات» ربات‌هایِ مدیریتِ گروه؛
    # بازگشت باید به همان صفحه باشد، نه به «تنظیماتِ گروه»یِ
    # مخصوصِ خدماتِ مجازی (که برایِ این نوعِ ربات معنا ندارد).
    "cmty": "tadm:sec:products",
}


def _smmai_back_target(origin: str) -> str:
    return _SMMAI_ORIGIN_TARGETS.get(origin, "smm:menu")


def _smmai_origin_from_cb(data: str) -> str:
    tail = data.rsplit(":", 1)[-1]
    return tail if tail in _SMMAI_ORIGIN_TARGETS else "smm"


async def smm_ai_settings_content(bot_id: int, origin: str = "smm") -> tuple[str, InlineKeyboardMarkup]:
    enabled = (await _get_setting(bot_id, "smm_ai_enabled", "0")) == "1"
    has_key = bool((await _get_setting(bot_id, "smm_ai_api_key", "")).strip())
    provider = await _get_provider(bot_id)
    provider_label, _, _ = _PROVIDERS[provider]
    text = (
        "🤖 <b>دستیارِ هوشِ مصنوعی</b>\n\n"
        "به سؤال‌هایِ مشتری دربارهٔ نحوهٔ کار با ربات، ثبت/پیگیریِ سفارش، "
        "وضعیتِ سفارش و... خودکار جواب می‌ده.\n\n"
        f"وضعیت: {'✅ فعال' if enabled else '❌ غیرفعال'}\n"
        f"ارائه‌دهنده: <b>{provider_label}</b>\n"
        f"کلیدِ API: {'✅ تنظیم شده' if has_key else '⚠️ تنظیم نشده'}\n\n"
        "⚠️ هزینهٔ استفاده از این کلید مستقیم رویِ حسابِ خودتونه؛ پلتفرم "
        "هزینه‌ای بابتش نمی‌گیره یا پرداخت نمی‌کنه.\n\n"
        "📌 <b>راه‌اندازی در گروه:</b> ابتدا ارائه‌دهنده و کلید را تنظیم کن، "
        "دستیار را روشن کن، سپس از «تنظیمات گروه» اسم دستیار و گروه مقصد را مشخص کن."
    )
    rows = [
        [InlineKeyboardButton(text="🔀 تغییرِ ارائه‌دهنده", callback_data=f"smmai:setprovider:{origin}")],
        [InlineKeyboardButton(text="🔑 تنظیمِ کلیدِ API", callback_data=f"smmai:setkey:{origin}")],
        [InlineKeyboardButton(text=("🔴 غیرفعال کن" if enabled else "🟢 فعال کن"),
                              callback_data=f"smmai:toggle:{origin}")],
        [InlineKeyboardButton(text="📚 راهنمای فعال‌سازی در گروه", callback_data=f"smmai:grouphelp:{origin}")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_smmai_back_target(origin), style=ButtonStyle.DANGER)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)




@smm_ai_router.callback_query(F.data.startswith("smmai:menu"))
async def smmai_menu(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    origin = _smmai_origin_from_cb(cb.data)
    text, kb = await smm_ai_settings_content(row.id, origin)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_ai_router.callback_query(F.data.startswith("smmai:grouphelp:"))
async def smmai_group_help(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    name = "بات ساز"
    await cb.message.answer(
        "📚 <b>راهنمای فعال‌سازی هوش مصنوعی در گروه</b>\n\n"
        "۱) از همین صفحه ارائه‌دهنده را انتخاب کنید.\n"
        "۲) کلید دسترسی را ثبت کنید؛ پس از تأیید کلید، دستیار فعال می‌شود.\n"
        "۳) از «تنظیمات گروه»، نامی برای دستیار تعیین کنید.\n"
        "۴) دکمه «اتصال هوش مصنوعی به گروه» را بزنید و گروه را انتخاب کنید؛ یا داخل گروه بفرستید:\n\n"
        "<code>/فعال_کردن_دستیار</code>\n\n"
        "۵) برای آزمایش، ابتدا فقط اسم دستیار را بفرستید؛ سپس سؤال را ارسال کنید.\n"
        "نمونه: <code>بات ساز</code> ← «جانم، سؤالت رو بپرس» ← <code>چطور خرید کنم؟</code>",
        parse_mode="HTML")
    await cb.answer("راهنما ارسال شد")


@smm_ai_router.callback_query(F.data.startswith("smmai:setprovider:"))
async def smmai_setprovider_menu(cb: CallbackQuery, bot: Bot):
    """لیستِ ۳ ارائه‌دهنده — انتخاب یکی از اونا کلیدِ فعلی رو پاک می‌کنه
    (چون کلیدهایِ ارائه‌دهنده‌هایِ مختلف با هم فرق دارن) و می‌خواد
    دوباره کلیدِ همون یکی رو وارد کنی."""
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    origin = _smmai_origin_from_cb(cb.data)
    cur = await _get_provider(row.id)
    rows = []
    for key, (label, _model, _src) in _PROVIDERS.items():
        mark = "✅ " if key == cur else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"smmai:provider:{key}:{origin}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smmai:menu:{origin}", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "🔀 <b>ارائه‌دهندهٔ هوشِ مصنوعی رو انتخاب کن</b>\n\n"
        "توجه: با تغییرِ ارائه‌دهنده، باید کلیدِ API همون سرویس رو دوباره وارد کنی.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@smm_ai_router.callback_query(F.data.startswith("smmai:provider:"))
async def smmai_setprovider_pick(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    provider, origin = parts[2], (parts[3] if len(parts) > 3 else "smm")
    if provider not in _PROVIDERS:
        await cb.answer("نامعتبر", show_alert=True)
        return
    cur = await _get_provider(row.id)
    if provider != cur:
        # === PATCH_SMM_AI_MULTI_PROVIDER_V1 ===
        # کلیدِ ارائه‌دهندهٔ قبلی برایِ این یکی معتبر نیست؛ خالی می‌کنیم و
        # غیرفعال می‌مونه تا کلیدِ درست وارد بشه (نه اینکه با کلیدِ غلط
        # بی‌صدا شکست بخوره).
        await _set_setting(row.id, "smm_ai_provider", provider)
        await _set_setting(row.id, "smm_ai_api_key", "")
        await _set_setting(row.id, "smm_ai_enabled", "0")
    await cb.answer("✅ ذخیره شد")
    label, _, src = _PROVIDERS[provider]
    text, kb = await smm_ai_settings_content(row.id, origin)
    await cb.message.edit_text(
        f"✅ ارائه‌دهنده رویِ «{label}» تنظیم شد.\n"
        f"حالا از «🔑 تنظیمِ کلیدِ API» کلیدت رو (از {src}) وارد کن.\n\n" + text,
        reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_AI_GROUP_V1 ===
@smm_ai_router.callback_query(F.data == "smmai:setgroupname")
async def smmai_setgroupname_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = (await _get_setting(row.id, "smm_ai_group_name", "")).strip()
    await state.set_state(SmmAiFSM.group_name)
    cur_line = f"الان: «{cur}»\n\n" if cur else ""
    await cb.message.answer(
        "✏️ اسمی که تویِ گروه باید صداش بزنن رو بفرست (مثلاً «بات ساز» یا «دستیار»).\n"
        f"{cur_line}"
        "بعدِ تنظیم، تویِ گروه‌هایی که این ربات عضوشونه، هر پیامی که با همین اسم "
        "شروع بشه (مثلاً «بات ساز چطور سفارش بدم؟») جواب داده می‌شه.\n\n"
        "❌ برایِ لغو /cancel را بفرست.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data="tadm:groupsettings", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_ai_router.message(SmmAiFSM.group_name, F.text)
async def smmai_setgroupname_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    await state.clear()
    if raw in ("لغو", "/cancel"):
        from tenant.shared.group_wallet import _group_settings_content
        text, kb = await _group_settings_content(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    name = raw[:30]
    if not name:
        await msg.answer("❌ اسمِ خالی قابلِ‌قبول نیست.")
        return
    await _set_setting(row.id, "smm_ai_group_name", name)
    # === PATCH_SMMAI_ORIGIN_TRACKING_V1 ===
    from tenant.shared.group_wallet import _group_settings_content
    text, kb = await _group_settings_content(row.id)
    await msg.answer(f"✅ از این به بعد تویِ گروه با اسمِ «{name}» جواب می‌ده.\n\n" + text,
                     reply_markup=kb, parse_mode="HTML")


@smm_ai_router.callback_query(F.data.startswith("smmai:toggle:"))
async def smmai_toggle(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    origin = _smmai_origin_from_cb(cb.data)
    if (await _get_setting(row.id, "smm_ai_enabled", "0")) != "1":
        if not (await _get_setting(row.id, "smm_ai_api_key", "")).strip():
            await cb.answer("❌ اول یه کلیدِ API ثبت کن.", show_alert=True)
            return
        await _set_setting(row.id, "smm_ai_enabled", "1")
    else:
        await _set_setting(row.id, "smm_ai_enabled", "0")
    await cb.answer("✅ ذخیره شد")
    text, kb = await smm_ai_settings_content(row.id, origin)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_ai_router.callback_query(F.data.startswith("smmai:setkey:"))
async def smmai_setkey_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    origin = _smmai_origin_from_cb(cb.data)
    await state.update_data(smmai_key_origin=origin)
    provider = await _get_provider(row.id)
    label, _model, src = _PROVIDERS[provider]
    await state.set_state(SmmAiFSM.api_key)
    await cb.message.answer(
        f"🔑 کلیدِ API برایِ «{label}» را بفرست.\n"
        f"از <code>{src}</code> می‌تونی بسازیش.\n\n"
        "(اگه ارائه‌دهنده اشتباهه، اول از «🔀 تغییرِ ارائه‌دهنده» عوضش کن.)\n\n"
        "❌ برایِ لغو /cancel را بفرست.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smmai:menu:{origin}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_ai_router.message(SmmAiFSM.api_key, F.text)
async def smmai_setkey_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    origin = data.get("smmai_key_origin", "smm")
    key = msg.text.strip()
    if key in ("لغو", "/cancel"):
        await state.clear()
        text, kb = await smm_ai_settings_content(row.id, origin)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    provider = await _get_provider(row.id)
    await msg.answer("⏳ در حالِ بررسیِ کلید...")
    reply = await ask_ai(provider, key, "سلام")
    if not reply:
        await msg.answer("❌ این کلید کار نکرد. دوباره بررسی کن و بفرست، یا /cancel بزن.")
        return
    await _set_setting(row.id, "smm_ai_api_key", key)
    await _set_setting(row.id, "smm_ai_enabled", "1")
    await state.clear()
    text, kb = await smm_ai_settings_content(row.id, origin)
    await msg.answer("✅ کلید تأیید و ذخیره شد؛ دستیار فعال شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════ سمتِ مشتری ═══════════════════

@smm_ai_router.callback_query(F.data == "smmai:ask")
async def smmai_ask_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if (await _get_setting(row.id, "smm_ai_enabled", "0")) != "1":
        await cb.answer("این قابلیت الان فعال نیست.", show_alert=True)
        return
    await state.set_state(SmmAiFSM.question)
    await cb.message.answer(
        "🤖 سؤالت رو بپرس (مثلاً: چرا سفارشم انجام نشده؟ چطور سفارش بدم؟):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data="smmu:home", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_ai_router.message(SmmAiFSM.question, F.text)
async def smmai_ask_answer(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    await state.clear()
    api_key = await _get_setting(row.id, "smm_ai_api_key", "")
    if not api_key:
        await msg.answer("❌ این قابلیت الان در دسترس نیست.")
        return
    provider = await _get_provider(row.id)
    await msg.answer("⏳ در حالِ فکر کردن...")
    context = (await _live_bot_context(row.id, row, msg.from_user.id)
               + "\n\n" + await _recent_orders_context(row.id, msg.from_user.id))
    answer = await ask_ai(provider, api_key, msg.text, context,
                          _bot_system_prompt(row))
    if not answer:
        await msg.answer("❌ الان نتونستم جواب بدم. بعداً دوباره امتحان کن یا با پشتیبانی تماس بگیر.")
        return
    _kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 سؤالِ دیگه", callback_data="smmai:ask"),
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smmu:home"),
    ]])
    try:   # PATCH_AI_GROUP_HTML_V1
        await msg.answer(f"🤖 {md_to_html(answer)}", reply_markup=_kb, parse_mode="HTML")
    except Exception:
        logger.exception("[SMM_AI] ارسالِ HTML ناموفق؛ متنِ ساده")
        await msg.answer(f"🤖 {answer.replace('**', '')}", reply_markup=_kb)


# ═══════════════════ فراخوانی داخلِ گروه با اسمِ دلخواه ═══════════════════
# === PATCH_SMM_AI_GROUP_V1 ===

# === PATCH_SMM_AI_GROUP_V1 ===
@smm_ai_router.message(Command("فعال_کردن_دستیار"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def smmai_connect_group(msg: Message, bot: Bot):
    """دستورِ صریح که فقط داخلِ خودِ گروه قابلِ‌اجراست — تنها راهیه که
    chat_id همون گروه رو می‌فهمیم. فقط صاحب/ادمینِ ربات (نه هر عضوِ
    گروهی) می‌تونه این کارو بکنه."""
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not _group_ai_allowed(row):
        return
    if not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await msg.reply("❌ فقط ادمینِ ربات می‌تونه این دستور رو بزنه.")
        return
    await _set_setting(row.id, "smm_ai_group_chat_id", str(msg.chat.id))
    await _set_setting(row.id, "smm_ai_group_chat_title", msg.chat.title or "این گروه")
    name = (await _get_setting(row.id, "smm_ai_group_name", "")).strip()
    test_text = (f"برای آزمایش، ابتدا فقط «{name}» را بفرست و بعد سؤالت را ارسال کن."
                 if name else "برای استفاده، ابتدا از تنظیمات گروه یک اسم برای دستیار تعیین کن.")
    await msg.reply(
        "✅ دستیار هوش مصنوعی با موفقیت به همین گروه متصل شد.\n\n"
        f"{test_text}")


@smm_ai_router.callback_query(F.data == "smmai:disconnectgroup")
async def smmai_disconnect_group(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await _set_setting(row.id, "smm_ai_group_chat_id", "")
    await _set_setting(row.id, "smm_ai_group_chat_title", "")
    await cb.answer("✅ اتصال قطع شد")
    # === PATCH_SMMAI_ORIGIN_TRACKING_V1 ===
    # این دکمه فقط تویِ «⚙️ تنظیماتِ گروه» هست، نه صفحه‌ی ارائه‌دهنده/کلید —
    # پس باید همونجا برگرده، نه smm_ai_settings_content.
    from tenant.shared.group_wallet import _group_settings_content
    text, kb = await _group_settings_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _smm_ai_group_match(msg: Message, bot: Bot) -> bool | dict:
    """فیلترِ async — عمداً همه‌ی چک‌ها (نوعِ ربات، فعال‌بودنِ دستیار، تنظیم‌بودنِ
    اسم، تطبیقِ متن) این‌جاست، نه تویِ بدنه‌ی هندلر. چون اگه فیلتر False
    برگردونه، aiogram اصلاً این هندلر رو صدا نمی‌زنه و می‌ره سراغِ روترِ
    بعدی (community_router و بقیه) — یعنی رباتِ مدیریتِ گروه یا هر رباتِ
    دیگه‌ای که تو همون گروهه، دست‌نخورده کار می‌کنه. اگه به‌جاش یه فیلترِ
    همیشه-true (مثلِ F.text خام) می‌ذاشتیم و چک‌ها رو تو بدنه انجام
    می‌دادیم، حتی وقتی این ربات کاری نداشت، پیام رو «مصرف‌شده» حساب
    می‌کرد و جلویِ روترهایِ بعدی رو می‌گرفت — دقیقاً همون باگی که تو
    گروه_wallet قبلاً دیدیم."""
    if not msg.chat or msg.chat.type not in ("group", "supergroup"):
        return False
    if not msg.text:
        return False
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        logger.info("[SMM_AI_GROUP] get_bot_row چیزی برنگردوند")
        return False
    if not _group_ai_allowed(row):
        logger.info(f"[SMM_AI_GROUP] نوع/پلن ربات برای دستیار گروه مجاز نیست ({row.field!r}/{row.plan!r})")
        return False
    # === PATCH_SMM_AI_GROUP_V1 ===
    connected_id = (await _get_setting(row.id, "smm_ai_group_chat_id", "")).strip()
    if not connected_id:
        logger.info(f"[SMM_AI_GROUP] bot_id={row.id} — هنوز به هیچ گروهی وصل نشده")
        return False
    if str(msg.chat.id) != connected_id:
        logger.info(f"[SMM_AI_GROUP] bot_id={row.id} — این گروه ({msg.chat.id}) وصل‌شده نیست ({connected_id})")
        return False
    if (await _get_setting(row.id, "smm_ai_enabled", "0")) != "1":
        logger.info(f"[SMM_AI_GROUP] bot_id={row.id} — دستیار غیرفعاله")
        return False
    group_name = (await _get_setting(row.id, "smm_ai_group_name", "")).strip()
    if not group_name:
        logger.info(f"[SMM_AI_GROUP] bot_id={row.id} — اسمِ گروه تنظیم نشده")
        return False
    text = msg.text.strip()
    logger.info(f"[SMM_AI_GROUP] bot_id={row.id} name={group_name!r} incoming={text!r}")
    if not text.lower().startswith(group_name.lower()):
        logger.info(f"[SMM_AI_GROUP] تطبیق نخورد — پیام با اسمِ تنظیم‌شده شروع نمی‌شه")
        return False
    remainder = text[len(group_name):]
    if remainder and remainder[0].isalnum():
        logger.info("[SMM_AI_GROUP] تطبیق نخورد — نام بخشی از یک کلمهٔ دیگر است")
        return False
    question = remainder.strip(" ،,:؛-؟?!")
    logger.info(f"[SMM_AI_GROUP] تطبیق موفق — سؤال={question!r}")
    return {"smm_ai_question": question, "smm_ai_row": row}


async def _smm_ai_answer_in_group(msg: Message, question: str, row) -> None:
    if not question.strip():
        await msg.reply("جانم، سؤالت رو بپرس.")
        return
    api_key = (await _get_setting(row.id, "smm_ai_api_key", "")).strip()
    if not api_key:
        logger.info(f"[SMM_AI_GROUP] bot_id={row.id} — کلیدِ API تنظیم نشده")
        return
    provider = await _get_provider(row.id)
    logger.info(f"[SMM_AI_GROUP] bot_id={row.id} provider={provider} — در حالِ فراخوانیِ AI برایِ سؤال={question!r}")
    context = (await _live_bot_context(row.id, row, msg.from_user.id)
               + "\n\n" + await _recent_orders_context(row.id, msg.from_user.id))
    answer = await ask_ai(provider, api_key, question, context,
                          _bot_system_prompt(row))
    if not answer:
        logger.info(f"[SMM_AI_GROUP] bot_id={row.id} — ask_ai چیزی برنگردوند (احتمالاً کلید/ارائه‌دهنده مشکل داره)")
        # === PATCH_AI_GROUP_FRIENDLY_ERR_V1 === در گروه پیامِ مخصوصِ ادمین (کلید/ارائه‌دهنده) نشان داده نمی‌شود
        try:
            from tenant import _notify_admins
            await _notify_admins(msg.bot, row, "⚠️ دستیارِ هوشِ مصنوعی در گروه جواب نداد؛ کلیدِ API یا ارائه‌دهنده را از «تنظیماتِ دستیار» چک کن.")
        except Exception:
            logger.exception("[SMM_AI_GROUP] اطلاع به ادمین ممکن نشد")
        await msg.reply("🙏 الان نتونستم جواب بدم؛ لطفاً چند دقیقهٔ دیگه دوباره امتحان کن.")
        return
    logger.info(f"[SMM_AI_GROUP] bot_id={row.id} — جواب دریافت شد، در حالِ ارسال")
    try:
        await msg.reply(f"🤖 {md_to_html(answer)}", parse_mode="HTML")
    except Exception:
        logger.exception("[SMM_AI_GROUP] ارسالِ HTML ناموفق؛ متنِ ساده")
        await msg.reply(f"🤖 {answer.replace('**', '')}")


@smm_ai_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), _smm_ai_group_match)
async def smmai_group_trigger(msg: Message, bot: Bot, state: FSMContext,
                              smm_ai_question: str, smm_ai_row) -> None:
    """نام تنها، مکالمهٔ دومرحله‌ای را برای همان کاربر و گروه باز می‌کند."""
    if not smm_ai_question.strip():
        await state.set_state(SmmAiFSM.group_question)
        await state.update_data(group_ai_bot_id=smm_ai_row.id,
                                group_ai_chat_id=str(msg.chat.id))
        await _smm_ai_answer_in_group(msg, "", smm_ai_row)
        return
    await state.clear()
    await _smm_ai_answer_in_group(msg, smm_ai_question, smm_ai_row)


@smm_ai_router.message(SmmAiFSM.group_question,
                       F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def smmai_group_followup(msg: Message, bot: Bot, state: FSMContext) -> None:
    """سؤال بعدیِ همان کاربر، بدون نیاز به تکرار نام دستیار."""
    data = await state.get_data()
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if (not row or data.get("group_ai_bot_id") != row.id
            or data.get("group_ai_chat_id") != str(msg.chat.id)
            or not _group_ai_allowed(row)
            or (await _get_setting(row.id, "smm_ai_enabled", "0")) != "1"):
        await state.clear()
        return
    await state.clear()
    await _smm_ai_answer_in_group(msg, msg.text.strip(), row)


# === PATCH_SMM_AI_GROUP_FALLBACK_FIX_V1 ===
async def maybe_handle_group_message(msg: Message, bot: Bot) -> bool:
    """نسخه‌ی مستقیم/غیرفیلتریِ همون منطق — از fallback_router صدا زده
    می‌شه، جایی که ثابت شد پیام‌هایِ گروه واقعاً بهش می‌رسن (برخلافِ
    ثبتِ smm_ai_router به‌عنوانِ فیلتر که به دلیلِ نامعلومی صدا زده
    نمی‌شد). خروجی True یعنی جواب داده شد (fallback نباید کارِ دیگه‌ای
    بکنه)، False یعنی ربطی نداشت."""
    result = await _smm_ai_group_match(msg, bot)
    if not result:
        return False
    await _smm_ai_answer_in_group(msg, result["smm_ai_question"], result["smm_ai_row"])
    return True
